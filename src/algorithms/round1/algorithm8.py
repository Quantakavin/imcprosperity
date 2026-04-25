"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v8 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000

HISTORY OF RESULTS:
    v3: 6,294  — PEPPER 5,089 + OSMIUM 1,205
    v4: 2,900  — REGRESSION (asks during accumulation, tightened OSMIUM)
    v5: ~6,500 — v3 + faster accumulation
    v6: 6,876  — PEPPER 5,396 + OSMIUM 1,480
    v7: ~6,800 — removed broken MM + simplified OSMIUM (no wave-riding)

V8 — TWO STRUCTURAL FIXES (not parameter tuning):

    FIX 1: PEPPER ACCUMULATION — SWEEP ALL ASKS
    =============================================
    Problem: v6 reached +80 at ts=41,300 (41% of day wasted).
    Cause:  sweep threshold at fair+4 misses the best ask when spread is
            13-16. Best ask sits at ~mid+6.5 to mid+8. Our fair+4 with bias
            = ~mid+6, just barely below the best ask.

    Fix: Remove the overpay cap entirely. Buy ANY ask during accumulation.
    Why this isn't overfitting — pure drift math:
      - Worst case: pay mid+8 (when spread=16)
      - Cost: 8 per unit × 80 = 640 total overpay
      - Reaching +80 at ts=10,000 vs 41,300 gains:
        80 × 0.001 × 31,300 = 2,504 extra drift PnL
      - Net gain: ~1,860 regardless of specific market data
      - Each unit earns 0.001 per timestamp of drift. Even at ts=50,000,
        remaining drift = 0.001 × 50,000 = 50 per unit. Paying 8 to earn
        50 is a no-brainer. Only stop near end-of-day.

    Safety: stop buying in the last 10,000 timestamps (10% of day).
    At that point, remaining drift per unit = 0.001 × 10,000 = 10.
    With spread ~8, that's barely breakeven. Not worth the risk.

    FIX 2: PEPPER MM — BOOK-RELATIVE PRICING
    ==========================================
    Problem: v6 MM earned +8 from 10 round-trips (effectively zero).
    Cause:  CODE BUG. Sell at fair+2, rebuy at fair+2 = SAME PRICE.
            Theoretical profit with book-relative pricing: +734.

    Fix: Sell at best_ask - 1 (undercut best bot ask to be first in queue).
         Rebuy at best_bid + 1 (improve on best bot bid for fastest fill).
    Why this isn't overfitting:
      - Uses actual market prices, not fitted parameters
      - Adapts to any spread width (13, 16, or anything else)
      - Average round-trip profit = spread - 2 ≈ 11-14 pts
      - vs v6's 0.8 pts per round-trip

    Only MM with 15 units to keep 65 units always riding drift.
    Expected: 10-20 round-trips × ~11 pts × 15 units = 1,650-3,300.
    Conservative estimate (accounting for fill rate): ~500-800.

    OSMIUM: Kept from v7 (simplified take + fast unwind). Offset → 2
    for slightly more fill opportunities (+50% ask fills observed in data).
    This is a small tweak, not a major change.

EXPECTED PnL:
    PEPPER: ~7,000-8,000 (faster accumulation + proper MM)
    OSMIUM: ~1,500-2,000 (takes + tighter passive quotes)
    TOTAL:  ~8,500-10,000

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
# POSITION LIMITS
# =============================================================================
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS — standard boilerplate (unchanged)
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
    Round 1 v8 — two structural fixes:
        PEPPER → Sweep ALL asks during accumulation + book-relative MM at +80
        OSMIUM → Take + fast unwind (from v7) with tighter passive quotes
    """

    # =========================================================================
    # PEPPER CONFIG
    # =========================================================================

    # Linear drift: +0.001 per timestamp = +1,000 per day.
    PEPPER_SLOPE = 0.001

    # Fair value bias correction (+2). Kept from v6 — proven to help.
    PEPPER_FAIR_BIAS = 2

    # v8 CHANGE: NO MAX_OVERPAY. We sweep ALL asks during accumulation.
    #
    # Old approach (v3-v7): only sweep asks up to fair + 3/4/5.
    #   This missed the best ask when spread was 13-16 because
    #   best_ask ≈ true_fair + 6.5 to 8, above our threshold.
    #
    # New approach: sweep everything. The drift ALWAYS pays for the spread.
    #   Even paying 8 above fair, each unit earns 50+ pts of drift over
    #   a half-day. The math: 0.001 * 50,000 remaining ts = 50 per unit.
    #   Paying 8 to earn 50 is a 6x return. No reason to be selective.
    #
    # The only limit is the position cap (80) and end-of-day cutoff.

    # Timestamp after which we STOP buying. At this point, remaining drift
    # per unit = 0.001 * (100,000 - 90,000) = 10. With spread ~8, barely
    # breakeven. Not worth the risk of holding inventory with no drift left.
    PEPPER_ACCUMULATION_CUTOFF = 90000

    # Passive bid premium during accumulation (when no asks to sweep).
    # Post at fair + 2 to sit high in queue. Same as v6/v7.
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # v8 CHANGE: MM with BOOK-RELATIVE pricing at +80.
    #
    # v6 BUG: sold at fair+2, rebought at fair+2 = same price = 0 profit.
    # v7 FIX: removed MM entirely (overcorrection — threw away ~730 profit).
    # v8 FIX: sell at best_ask - 1, rebuy at best_bid + 1.
    #
    # Why best_ask - 1:
    #   Undercuts the best bot ask by 1 tick. We become the cheapest offer
    #   in the book. Any incoming buy order fills us first (price improvement
    #   for the buyer, but we still sell near the top of the spread).
    #
    # Why best_bid + 1:
    #   Improves on the best bot bid by 1 tick. We become the most attractive
    #   bid in the book. Any incoming sell order fills us first.
    #
    # Round-trip profit = (best_ask - 1) - (best_bid + 1) = spread - 2.
    # With avg spread 13.7: profit ≈ 11.7 per unit per cycle.
    # vs v6: profit ≈ 0.8 per unit per cycle (91x improvement).
    #
    # Adapts automatically to any spread — not fitted to historical data.
    PEPPER_MM_SIZE = 15  # units to market-make with at +80

    # =========================================================================
    # OSMIUM CONFIG
    # =========================================================================

    # Fair value: hardcoded 10000 (proven across all versions).
    OSMIUM_FAIR = 10000

    # Soft position cap: 50 (kept from v6/v7 — allowed more take volume).
    OSMIUM_SOFT_LIMIT = 50

    # v8 CHANGE: Quote offset 3 → 2.
    #
    # Data showed moving to offset 2 gives ~50% more ask fill opportunities
    # (13 ticks vs 7 ticks with bids >= 10002). The tradeoff:
    #   - Offset 3: 6pt round-trip, ~12 passive fills/day
    #   - Offset 2: 4pt round-trip, ~18 passive fills/day
    #   - 12 × 6 = 72 vs 18 × 4 = 72 → roughly the same PnL
    #
    # BUT offset 2 also means our resting orders are more likely to get
    # filled by MM2 when it appears with tight quotes. MM2 bids at 10002-10003
    # and our ask at 10002 would catch those. With offset 3 (ask at 10003),
    # we miss MM2 bids at 10002.
    #
    # Risk: v4 tried offset 2 and it was worse — BUT v4 also had a broken
    # PEPPER strategy (posted asks during accumulation). The OSMIUM regression
    # in v4 may have been noise, not caused by the offset change.
    # Testing offset 2 again with everything else fixed.
    OSMIUM_QUOTE_OFFSET = 2

    # Inventory skew: 6 (from v7 — stronger unwinding).
    OSMIUM_SKEW_FACTOR = 6

    # Panic threshold: 40 (from v7 — take at fair when position too large).
    OSMIUM_PANIC_THRESHOLD = 40

    # =========================================================================
    # RUN
    # =========================================================================
    def run(self, state: TradingState):

        # =====================================================================
        # STEP 0: RESTORE STATE
        # =====================================================================
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)
        pepper_last_fair = stored.get("pepper_last_fair", None)

        # =====================================================================
        # STEP 1: PROCESS EACH PRODUCT
        # =====================================================================
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []

            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position
            sell_room = limit + position

            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            # =================================================================
            # INTARIAN_PEPPER_ROOT
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                # --- Handle empty book (from v7) ---
                if best_bid is None and best_ask is None:
                    result[product] = orders
                    continue

                # --- Day boundary detection ---
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True

                pepper_last_timestamp = state.timestamp

                # --- Fair value ---
                if best_bid is not None and best_ask is not None:
                    mid_price = (best_bid + best_ask) / 2
                    if is_new_day:
                        pepper_day_start_price = mid_price
                    fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp + self.PEPPER_FAIR_BIAS
                    fair_int = round(fair)
                    pepper_last_fair = fair_int
                elif pepper_last_fair is not None:
                    fair_int = pepper_last_fair
                else:
                    fair_int = best_bid if best_bid is not None else best_ask

                # =============================================================
                # PHASE 1: ACCUMULATION (position < 80)
                # =============================================================
                # v8 CHANGE: Sweep ALL asks, no price cap.
                #
                # The drift earns 0.001 per timestamp per unit. With 90,000
                # timestamps remaining at ts=10,000:
                #   remaining_drift = 0.001 × 90,000 = 90 per unit
                # Even paying 8 above fair (worst spread), net profit = 82/unit.
                #
                # EVERY ask in the book is worth buying, up to the position limit.
                # The only exception: near end-of-day when drift can't cover spread.
                #
                # We still post a passive bid at fair+2 for ticks when no asks
                # are available (the book only has bids).

                if position < limit and state.timestamp < self.PEPPER_ACCUMULATION_CUTOFF:

                    # Sweep ALL asks — no price threshold.
                    # Just buy everything the book offers until position = 80.
                    for ask_price, ask_vol in bot_asks:
                        if buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break  # at position limit

                    # Passive bid for ticks with no asks (or after sweeping all)
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                    # STILL no asks during accumulation. v4 lesson remains.

                # =============================================================
                # PHASE 2: HOLD + BOOK-RELATIVE MM (position = 80)
                # =============================================================
                # v8 CHANGE: Bring back MM, but with correct pricing.
                #
                # v6 BUG: sell at fair+2, rebuy at fair+2 → 0 profit per trip.
                # v8 FIX: sell at best_ask-1, rebuy at best_bid+1.
                #
                # This captures spread - 2 ≈ 11-14 pts per round-trip.
                # With 15 units and ~10-20 fills: 1,650-4,200 gross profit.
                # After drift cost (15 units × ~5 ticks × 0.001 × 100): ~-7.5
                # Net: massively positive.
                #
                # WHY best_ask - 1:
                #   We UNDERCUT the best bot ask. Any buy order that would have
                #   filled the bot at best_ask now fills US at best_ask-1.
                #   We get filled more often (price improvement for counterparty).
                #
                # WHY best_bid + 1:
                #   We IMPROVE on the best bot bid. Any sell order coming in
                #   fills us first. We rebuy at best_bid+1, which is near the
                #   bottom of the spread — maximizing the round-trip profit.
                #
                # The spread adapts to the market automatically. No fitted params.

                elif position >= limit:
                    # We're at +80. MM time.

                    if best_ask is not None and best_bid is not None:
                        # Only MM if we can see both sides of the book
                        spread = best_ask - best_bid

                        # Only MM if the spread is wide enough to profit.
                        # Minimum profitable spread: 4 (sell at ask-1, buy at bid+1,
                        # profit = spread - 2 = 2. Need at least 2 to cover drift cost).
                        # With typical spread of 13, this almost always passes.
                        if spread >= 4:
                            mm_size = min(self.PEPPER_MM_SIZE, sell_room)

                            if mm_size > 0:
                                # SELL: undercut best ask by 1
                                my_ask = best_ask - 1
                                orders.append(Order(product, my_ask, -mm_size))

                            # REBUY: improve on best bid by 1
                            # buy_room here accounts for current position.
                            # If we sold some MM units last tick and are at 65,
                            # buy_room = 80 - 65 = 15, and we bid to refill.
                            if buy_room > 0:
                                my_bid = best_bid + 1
                                orders.append(Order(product, my_bid, buy_room))

                        else:
                            # Spread too tight — just hold, don't risk it
                            # Still bid to refill if below 80 from previous MM
                            if buy_room > 0:
                                my_bid = best_bid + 1
                                orders.append(Order(product, my_bid, buy_room))
                    else:
                        # One-sided book at +80 — just hold
                        # If we're below 80 and there are asks, sweep them
                        if buy_room > 0:
                            for ask_price, ask_vol in bot_asks:
                                if buy_room > 0:
                                    take_qty = min(-ask_vol, buy_room)
                                    orders.append(Order(product, ask_price, take_qty))
                                    buy_room -= take_qty
                                else:
                                    break

                # =============================================================
                # PHASE 3: LATE-DAY (past cutoff, position < 80)
                # =============================================================
                # Past ACCUMULATION_CUTOFF: don't buy new units (drift too small
                # to cover spread). But if we somehow lost units from MM, still
                # try to get back to 80 via passive bid at fair.
                else:
                    if buy_room > 0 and best_bid is not None:
                        # Bid at fair (conservative — don't overpay late in day)
                        orders.append(Order(product, fair_int, buy_room))

            # =================================================================
            # ASH_COATED_OSMIUM — TAKE + FAST UNWIND
            # =================================================================
            # Carried from v7 with one change: offset 3 → 2 for more fills.
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                if best_bid is None or best_ask is None:
                    result[product] = orders
                    continue

                fair_int = self.OSMIUM_FAIR
                soft_limit = self.OSMIUM_SOFT_LIMIT

                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                # --- Panic flags ---
                is_panic_long = position >= self.OSMIUM_PANIC_THRESHOLD
                is_panic_short = position <= -self.OSMIUM_PANIC_THRESHOLD

                # --- TAKE mispriced orders ---
                # This is the core alpha (~1,572/day in v6).
                # Buy below 10000, sell above 10000. Simple, robust.

                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and is_panic_short:
                        # Panic short: buy even at fair to reduce position
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and soft_sell_room > 0:
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and soft_sell_room > 0 and is_panic_long:
                        # Panic long: sell even at fair to reduce position
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # --- PASSIVE quotes with strong inventory skew ---
                # Recalculate room after takes
                soft_buy_room = min(soft_limit - position, limit - position)
                soft_sell_room = min(soft_limit + position, limit + position)

                position_ratio = position / soft_limit if soft_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

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
            "pepper_last_fair": pepper_last_fair,
        })

        conversions = 0

        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
