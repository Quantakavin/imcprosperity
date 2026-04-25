"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v7 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000

HISTORY OF RESULTS:
    v3: 6,294  — PEPPER 5,089 + OSMIUM 1,205
    v4: 2,900  — REGRESSION (asks during accumulation, tightened OSMIUM)
    v5: ~6,500 — v3 + faster accumulation
    v6: 6,876  — PEPPER 5,396 + OSMIUM 1,480
        PEPPER: bias correction helped (reached +80 by ts=41300 vs v3's 65700)
        OSMIUM: wave-riding FAILED (take logic overrode target, passive lost money)

V7 DIAGNOSIS & FIXES:

    PEPPER LEAKS FOUND:
        1. MM overlay at +80 earned +8 total from 12 round-trips but cost
           ~5.6 in drift by dropping below 80 for 70 ticks. Net: ~zero.
           FIX: Remove MM entirely. Just hold 80 and ride the drift.
        2. 90 ticks had empty book (one side missing) → algo skipped them.
           FIX: Use stored fair value when book is one-sided. Still sweep
           any available asks and still post passive bids.

    OSMIUM LEAKS FOUND:
        1. Wave-riding target was ignored — position deviated by 30-50 units
           from target routinely. EMA (alpha=0.05) only tracked 7 of 25 pts.
        2. Takes earned ~1,572 but total PnL was 1,480 → passive quotes
           and wave-riding LOST ~92. The overlay was net negative.
        3. Position sat at ±40-47 for long stretches → unrealized losses
           from mean reversion wiped out take profits.

        FIX: Simplify radically.
           - Remove wave-riding (EMA, target position) entirely
           - Keep aggressive taking (the REAL alpha: 4+ pts per trade)
           - MUCH stronger inventory skew to unwind FAST after takes
           - New skew factor: 6 (was 4). At position ±50, the unwind
             side posts at fair±1 (maximum aggression to flatten)
           - Add a "panic unwind" at ±40: take any order that reduces
             position, even at fair (0 profit), to avoid sitting on
             large positions that get crushed by mean reversion

EXPECTED PnL:
    PEPPER: ~5,500 (hold at +80, no MM leakage, handle empty book)
    OSMIUM: ~1,800-2,200 (same takes, faster unwinding, less position bleed)
    TOTAL:  ~7,300-7,700

Position limits: 80 for both products.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# =============================================================================
# POSITION LIMITS — both products capped at 80
# =============================================================================
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS — standard IMC Prosperity visualizer boilerplate (unchanged)
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]],
              conversions: int, trader_data: str) -> None:
        base_length = len(
            self.to_json([
                self.compress_state(state, ""),
                self.compress_orders(orders),
                conversions, "", "",
            ])
        )
        max_item_length = (self.max_log_length - base_length) // 3
        print(
            self.to_json([
                self.compress_state(state, self.truncate(state.traderData, max_item_length)),
                self.compress_orders(orders),
                conversions,
                self.truncate(trader_data, max_item_length),
                self.truncate(self.logs, max_item_length),
            ])
        )
        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp, trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for t in arr:
                compressed.append([t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp])
        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        try:
            for product, obs in observations.conversionObservations.items():
                conversion_observations[product] = [
                    obs.bidPrice, obs.askPrice, obs.transportFees,
                    obs.exportTariff, obs.importTariff,
                    getattr(obs, "sugarPrice", 0),
                    getattr(obs, "sunlightIndex", 0),
                ]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for o in arr:
                compressed.append([o.symbol, o.price, o.quantity])
        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        lo, hi = 0, min(len(value), max_length)
        out = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = value[:mid]
            if len(candidate) < len(value):
                candidate += "..."
            if len(json.dumps(candidate)) <= max_length:
                out = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return out


logger = Logger()


# =============================================================================
# TRADER CLASS
# =============================================================================
class Trader:
    """
    Round 1 v7 — simplified and data-driven:
        PEPPER  → Accumulate to +80, HOLD (no MM overlay)
        OSMIUM  → Aggressive taking + fast unwinding (no wave-riding)
    """

    # =========================================================================
    # PEPPER CONFIG
    # =========================================================================

    # Linear drift: +0.001 per timestamp = +1,000 per day.
    PEPPER_SLOPE = 0.001

    # Fair value bias correction (NEW in v6, kept in v7).
    # Our linear model underestimates by ~1.5. We add +2 (rounded up).
    # This was PROVEN to help in v6: reached +80 by ts=41300 (vs v3's 65700).
    PEPPER_FAIR_BIAS = 2

    # Sweep threshold: buy any ask at fair + OVERPAY or below.
    # At +4 above bias-corrected fair, we're paying ~true_fair+6.
    # Aggressive, but drift pays it back in ~2000 ticks (2% of day).
    PEPPER_MAX_OVERPAY = 4

    # Passive bid during accumulation: post at fair + PREMIUM.
    # With bias, this is ~true_fair+4. Sits high in queue for fast fills.
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # v7 CHANGE: NO MM CONFIG AT ALL.
    #
    # v6 had PEPPER_MM_OFFSET, PEPPER_MM_SIZE, PEPPER_TAKE_PROFIT_THRESHOLD.
    # All removed. The v6 data showed:
    #   - 12 MM round-trips earned a total of +8 seashells
    #   - Those round-trips dropped us below 80 for 70 ticks
    #   - 70 ticks * 80 units * 0.001 = 5.6 in lost drift
    #   - Net: approximately zero, with added complexity and risk
    #
    # The correct strategy at +80 is simply: DO NOTHING.
    # Hold 80 units. Let the drift compound. Don't trade.
    # Any trade at +80 has negative expected value because:
    #   1. Selling reduces our drift exposure (cost: 0.001/tick/unit)
    #   2. Rebuying costs spread (~6-8 per unit)
    #   3. Round-trip profit is ~0-2 per unit (v6 data: avg 0.67)
    #   4. 0.67 profit < 0.001 * ticks_to_rebuy * units sold

    # =========================================================================
    # OSMIUM CONFIG — RADICALLY SIMPLIFIED IN V7
    # =========================================================================

    # Long-term fair value. Rock-solid at 10,000 across all days.
    # v6 tried a dynamic EMA fair — it was too slow (7pt range vs 25pt actual)
    # and the wave-riding target was completely overridden by takes anyway.
    # Back to basics: fair = 10,000. Period.
    OSMIUM_FAIR = 10000

    # Soft position cap: 50 (kept from v6).
    # v6 data showed positions >30 earned +334 PnL with no blowups.
    # The extra capacity lets us take more mispriced orders before capping out.
    OSMIUM_SOFT_LIMIT = 50

    # Quote offset: 3 ticks from fair (unchanged since v3).
    # v4 tried 2 → worse. v3/v5/v6 all used 3 → works.
    # Passive quotes at 9997 bid / 10003 ask.
    OSMIUM_QUOTE_OFFSET = 3

    # v7 CHANGE: Stronger inventory skew factor: 6 (was 4).
    #
    # WHY: The #1 OSMIUM problem in v6 was position sitting at ±40-47
    # for long stretches. Takes earned 4+ pts each, but the accumulated
    # position bled unrealized losses from mean reversion.
    #
    # The fix: unwind faster. With skew=6:
    #   At position +50 (ratio=1.0):
    #     bid_offset = 3 + 6*1.0 = 9 (barely bidding, at 9991)
    #     ask_offset = 3 - 6*1.0 = -3 → clamped to 1 (asking at 10001!)
    #   vs v6 skew=4:
    #     bid_offset = 3 + 4*1.0 = 7 (at 9993)
    #     ask_offset = 3 - 4*1.0 = -1 → clamped to 1 (same)
    #
    # The big difference is the bid side: at 9991 vs 9993. Two ticks
    # further out means we STOP buying sooner when we're already long.
    # The ask side is the same (clamped to 1) — we sell aggressively.
    #
    # At position +25 (ratio=0.5):
    #     bid_offset = 3 + 6*0.5 = 6 (at 9994 — wide, slow buying)
    #     ask_offset = 3 - 6*0.5 = 0 → clamped to 1 (asking at 10001)
    #   vs v6:
    #     bid_offset = 3 + 4*0.5 = 5 (at 9995)
    #     ask_offset = 3 - 4*0.5 = 1 (same)
    #
    # The skew kicks in EARLIER and HARDER, preventing position from
    # drifting to ±40 in the first place.
    OSMIUM_SKEW_FACTOR = 6

    # v7 NEW: Panic unwind threshold.
    #
    # When |position| exceeds this, we take ANY order that reduces our
    # position, even at exactly fair (10000). Normally we only take
    # orders ABOVE fair (sells) or BELOW fair (buys). But at high
    # inventory, the risk of holding outweighs waiting for a better price.
    #
    # At ±40 with mean-reversion, a 3-point adverse move = 120 loss.
    # Taking at fair (0 profit per unit) to reduce position by 10 avoids
    # 10 * 3 = 30 of potential loss. Worth it.
    OSMIUM_PANIC_THRESHOLD = 40

    # =========================================================================
    # RUN — called once per tick
    # =========================================================================
    def run(self, state: TradingState):

        # =====================================================================
        # STEP 0: RESTORE PERSISTED STATE
        # =====================================================================
        # Only two variables now (removed osmium_ema from v6):
        #   pepper_day_start_price — intercept for PEPPER fair value
        #   pepper_last_timestamp  — for day boundary detection
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)
        # v7 NEW: store last known fair for PEPPER in case book is one-sided
        pepper_last_fair = stored.get("pepper_last_fair", None)

        # =====================================================================
        # STEP 1: PROCESS EACH PRODUCT
        # =====================================================================
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []

            # ----- Position bookkeeping -----
            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position
            sell_room = limit + position

            # ----- Read the order book -----
            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            # =================================================================
            # INTARIAN_PEPPER_ROOT — ACCUMULATE + HOLD
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                # v7 CHANGE: Handle one-sided book.
                #
                # In v6, we skipped the tick entirely if either side was empty.
                # This happened 90 times! On those ticks, there might still be
                # asks we can sweep or we should post a passive bid.
                #
                # New logic:
                #   - If BOTH sides empty: skip (truly nothing to do)
                #   - If only bids empty but asks exist: use last known fair
                #     to decide whether to sweep asks. Can't compute mid, but
                #     the linear model + stored fair is good enough.
                #   - If only asks empty but bids exist: still post a passive
                #     bid using stored fair (in case someone sells into us).
                #   - Normal case: compute mid, update fair, proceed as before.

                if best_bid is None and best_ask is None:
                    # Truly empty book. Nothing to do.
                    result[product] = orders
                    continue

                # --- Day boundary detection ---
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True

                pepper_last_timestamp = state.timestamp

                # --- Compute fair value ---
                if best_bid is not None and best_ask is not None:
                    # Normal case: both sides present → compute mid
                    mid_price = (best_bid + best_ask) / 2
                    if is_new_day:
                        pepper_day_start_price = mid_price
                    fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp + self.PEPPER_FAIR_BIAS
                    fair_int = round(fair)
                    pepper_last_fair = fair_int  # store for next tick
                elif pepper_last_fair is not None:
                    # One-sided book: use last known fair + slope increment.
                    # The slope adds 0.001 per timestamp. Since ticks are 100
                    # apart, that's +0.1 per tick. We approximate with last_fair.
                    # The error is at most 0.1 — negligible.
                    fair_int = pepper_last_fair
                    # If it's a new day and we can't compute start price, we
                    # have to wait for a tick with both sides.
                    if is_new_day and best_bid is not None and best_ask is not None:
                        pepper_day_start_price = (best_bid + best_ask) / 2
                else:
                    # Very first tick AND book is one-sided. Extremely rare.
                    # Use the one price we have as a rough estimate.
                    fair_int = best_bid if best_bid is not None else best_ask
                    if is_new_day:
                        pepper_day_start_price = fair_int

                # =============================================================
                # ACCUMULATION (position < 80)
                # =============================================================
                # Same as v6: sweep asks up to fair+OVERPAY, post passive bid.
                # NEVER post an ask during accumulation.

                if position < limit:
                    max_buy_price = fair_int + self.PEPPER_MAX_OVERPAY

                    # Sweep available asks (if any)
                    for ask_price, ask_vol in bot_asks:
                        if ask_price <= max_buy_price and buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break

                    # Post passive bid (even if ask side is empty — someone
                    # might sell into us before the book refills)
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                # =============================================================
                # HOLD (position = 80) — v7: DO NOTHING
                # =============================================================
                # v6 had an MM overlay here (sell 10, buy back). Removed in v7.
                #
                # Why do nothing? The drift earns 80 * 0.001 = 0.08 per tick
                # automatically. Any trade risks:
                #   - Losing drift exposure (if sell fills but rebuy doesn't)
                #   - Paying spread on round-trip (~6-8 per unit)
                #   - v6 data: 12 round-trips, total profit +8 (breakeven)
                #
                # The optimal play at +80 is to sit on our hands and let the
                # linear drift print money. No orders needed.
                #
                # We don't even post a passive bid here — we're at +80, we
                # CAN'T buy more. And we don't want to sell (see above).
                # The else clause is intentionally empty.
                else:
                    pass  # Hold. Do nothing. Let drift work.

            # =================================================================
            # ASH_COATED_OSMIUM — TAKE + FAST UNWIND
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # v7: Both sides needed for OSMIUM (no one-sided handling needed,
                # OSMIUM book is always two-sided in historical data)
                if best_bid is None or best_ask is None:
                    result[product] = orders
                    continue

                fair_int = self.OSMIUM_FAIR  # 10000, hardcoded, proven
                soft_limit = self.OSMIUM_SOFT_LIMIT

                # --- Soft position room ---
                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                # --- Check if we're in "panic" territory ---
                # When |position| exceeds PANIC_THRESHOLD (40), we need to
                # unwind aggressively. This means:
                #   1. Taking orders AT fair (not just above/below)
                #   2. The passive quotes will be extremely skewed (from the
                #      high skew factor of 6)
                #
                # Why 40? At 40 units, a 3-point adverse move = 120 loss.
                # That's 8% of our total OSMIUM budget. Too much risk.
                is_panic_long = position >= self.OSMIUM_PANIC_THRESHOLD   # need to sell
                is_panic_short = position <= -self.OSMIUM_PANIC_THRESHOLD  # need to buy

                # =============================================================
                # STEP A: TAKE MISPRICED ORDERS
                # =============================================================
                # This is the CORE alpha for OSMIUM. v6 data showed:
                #   - 60 take fills, avg profit 4.2 pts/unit, total ~1,572
                #   - This ALONE exceeded total OSMIUM PnL (1,480)
                #
                # Logic:
                #   Normal mode: take asks < 10000 (cheap buys), bids > 10000 (expensive sells)
                #   Panic long:  also take bids AT 10000 (get out at any cost)
                #   Panic short: also take asks AT 10000 (get out at any cost)

                # --- Sweep cheap asks (buy below fair) ---
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        # Below 10000 = always take. Guaranteed profit on mean reversion.
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and is_panic_short:
                        # AT 10000: only take if we're in panic short mode.
                        # We're desperate to reduce our short — buying at fair
                        # earns 0 profit but reduces position risk.
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                # --- Sweep expensive bids (sell above fair) ---
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and soft_sell_room > 0:
                        # Above 10000 = always take. Guaranteed profit on mean reversion.
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and soft_sell_room > 0 and is_panic_long:
                        # AT 10000: only take if we're in panic long mode.
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # =============================================================
                # STEP B: INVENTORY-SKEWED PASSIVE QUOTES
                # =============================================================
                # Post bids and asks around 10000 with aggressive inventory skew.
                #
                # The skew's job: UNWIND position as fast as possible after takes.
                # Takes earn 4+ pts. We don't need the passive quotes to be
                # profitable — they just need to flatten our position before
                # mean reversion eats our unrealized PnL.
                #
                # With skew_factor=6 (up from 4):
                #
                # Position +50 (max long, ratio=1.0):
                #   bid_offset = 3 + 6*1.0 = 9 → bid at 9991 (almost never fills)
                #   ask_offset = 3 - 6*1.0 = -3 → clamped to 1 → ask at 10001
                #   Result: aggressively offering to sell at 10001 while barely buying
                #
                # Position +25 (ratio=0.5):
                #   bid_offset = 3 + 6*0.5 = 6 → bid at 9994
                #   ask_offset = 3 - 6*0.5 = 0 → clamped to 1 → ask at 10001
                #   Result: still very eager to sell, reluctant to buy
                #
                # Position +10 (ratio=0.2):
                #   bid_offset = 3 + 6*0.2 = 4.2 → 4 → bid at 9996
                #   ask_offset = 3 - 6*0.2 = 1.8 → 2 → ask at 10002
                #   Result: slightly skewed, mostly symmetric
                #
                # Position 0 (neutral):
                #   bid_offset = 3, ask_offset = 3 → symmetric 9997/10003

                # Recalculate soft room after takes (position may have changed)
                soft_buy_room = min(soft_limit - position, limit - position)
                soft_sell_room = min(soft_limit + position, limit + position)

                position_ratio = position / soft_limit if soft_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                # Minimum 1: never cross the spread
                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

                # Safety clamps
                my_bid = min(my_bid, fair_int - 1)
                my_ask = max(my_ask, fair_int + 1)

                if soft_buy_room > 0:
                    orders.append(Order(product, my_bid, soft_buy_room))
                if soft_sell_room > 0:
                    orders.append(Order(product, my_ask, -soft_sell_room))

            # =================================================================
            # UNKNOWN PRODUCT — safe fallback
            # =================================================================
            else:
                if best_bid is None or best_ask is None:
                    result[product] = orders
                    continue
                mid_price = (best_bid + best_ask) / 2
                fair_int = round(mid_price)
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and buy_room > 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                    else:
                        break
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                    else:
                        break
                my_bid = min(best_bid + 1, fair_int - 1)
                my_ask = max(best_ask - 1, fair_int + 1)
                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            result[product] = orders

        # =====================================================================
        # STEP 2: PERSIST STATE
        # =====================================================================
        trader_data = json.dumps({
            "pepper_day_start_price": pepper_day_start_price,
            "pepper_last_timestamp": pepper_last_timestamp,
            "pepper_last_fair": pepper_last_fair,  # v7 NEW: for one-sided book handling
            # v7: removed osmium_ema (wave-riding removed)
        })

        conversions = 0

        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
