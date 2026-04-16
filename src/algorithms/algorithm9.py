"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v9 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000

HISTORY OF RESULTS:
    v3: 6,294  — PEPPER 5,089 + OSMIUM 1,205
    v6: 6,876  — PEPPER 5,396 + OSMIUM 1,480
    v8: 8,400  — PEPPER 7,219 + OSMIUM 1,266
        PEPPER: sweep-all accumulation was a home run (+80 in 3 ticks)
        PEPPER: MM net negative (-67) because rebuy swept asks at full price
        OSMIUM: offset 2 and skew 6 both backfired (persistent -65 short bias)

V9 — TWO TARGETED FIXES:

    FIX 1: PEPPER MM REBUY BUG
    ============================
    Problem: After MM sell drops position below 80, the ACCUMULATION branch
    triggers (position < limit) and sweeps ALL asks. This rebuys at full
    ask price (top of spread), making each round-trip lose ~1/unit.

    Data: 9 round-trips, total PnL = -67. Sell at best_ask-1 works fine.
    The rebuy at full ask is the bug.

    Fix: Track "reached_max" flag in trader_data. Once we've been at +80:
      - NEVER use aggressive accumulation again
      - Rebuy PASSIVELY at best_bid + 1 (bottom of spread)
      - This changes round-trip profit from -1/unit to +11/unit

    Math: sell at best_ask-1 (mid+5.5), rebuy at best_bid+1 (mid-5.5)
    Round-trip = 11/unit. On 15 units: +165 per cycle.
    Even if passive rebuy takes 50 ticks (5000 ts), drift cost is
    15 * 0.001 * 5000 = 75. Net: +90 per cycle. Still very positive.

    FIX 2: OSMIUM — REVERT TO V6 SETTINGS
    ========================================
    Problem: v8 changed offset 3→2 and skew 4→6. Both backfired.
      - Offset 2: 55 passive fills (up from 12) but persistent short bias
        to -65. More fills built bad inventory faster than unwinding.
      - Skew 6: too aggressive, made the problem worse not better.

    Fix: Revert to v6 proven settings:
      - Offset 3 (scored 1,480 vs offset 2's 1,266)
      - Skew 4 (never exceeded ±47 vs skew 6's -65)
      - Soft limit 50 (kept — more take volume was net positive)
      - Panic at 40 (kept — safety net)

    These settings scored 1,480 in v6. Not overfitting — just using
    the configuration that's been proven across multiple runs.

EXPECTED PnL:
    PEPPER: ~7,500 (v8's 7,219 + fixed MM ~+300)
    OSMIUM: ~1,480 (reverting to v6 proven settings)
    TOTAL:  ~9,000

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
    Round 1 v9:
        PEPPER → Sweep-all accumulation + fixed book-relative MM
        OSMIUM → Take + passive with v6 proven settings
    """

    # =========================================================================
    # PEPPER CONFIG
    # =========================================================================

    PEPPER_SLOPE = 0.001
    PEPPER_FAIR_BIAS = 2

    # End-of-day cutoff: stop accumulating new units after this timestamp.
    # Remaining drift = 0.001 * (100,000 - 90,000) = 10 per unit.
    # With spread ~8, that's barely breakeven. Not worth accumulating.
    PEPPER_ACCUMULATION_CUTOFF = 90000

    # Passive bid premium during INITIAL accumulation (before reaching +80).
    # Posted at fair + 2 to sit high in queue for fast fills.
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # MM size: how many units to cycle at +80.
    # 15 units = 19% of position. The other 65 units always ride drift.
    # If rebuy takes 50 ticks: drift cost = 15 * 0.001 * 5000 = 75.
    # Round-trip profit: 15 * 11 = 165. Net: +90. Positive.
    # If rebuy takes 100 ticks: drift cost = 150. Net: +15. Still positive.
    # If rebuy takes 150+ ticks: net negative. But v8 data showed average
    # rebuy time of ~1-20 ticks. 150 is extremely unlikely.
    PEPPER_MM_SIZE = 15

    # =========================================================================
    # OSMIUM CONFIG — REVERTED TO V6 PROVEN SETTINGS
    # =========================================================================
    # v6 scored 1,480 on OSMIUM. v8 tried offset 2 + skew 6 → scored 1,266.
    # Reverting. These settings are proven, not fitted.

    OSMIUM_FAIR = 10000

    # Soft limit 50 (kept from v6 — more take volume was net positive).
    OSMIUM_SOFT_LIMIT = 50

    # Offset 3 (v6 value). v8's offset 2 gave 55 passive fills but built
    # a persistent -65 short position that bled unrealized PnL.
    # Offset 3: fewer fills (12) but each earns 6pts and inventory stays manageable.
    OSMIUM_QUOTE_OFFSET = 3

    # Skew 4 (v6 value). v8's skew 6 was too aggressive — at -40 position
    # the ask went to 10007, making it impossible to sell (unwind). The quotes
    # became so asymmetric that they trapped the position instead of unwinding it.
    # Skew 4: gentler, position never exceeded ±47 in v6 vs -65 in v8.
    OSMIUM_SKEW_FACTOR = 4

    # Panic threshold: take at fair when |position| >= 40.
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

        # v9 NEW: Track whether we've completed initial accumulation.
        # Once True, we NEVER use aggressive sweeps again — only passive rebuys.
        # This prevents the v8 bug where MM sells trigger accumulation sweeps
        # that rebuy at full ask price (top of spread).
        pepper_reached_max = stored.get("pepper_reached_max", False)

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

                if best_bid is None and best_ask is None:
                    result[product] = orders
                    continue

                # --- Day boundary detection ---
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True
                    # Reset reached_max on new day — position resets to 0
                    pepper_reached_max = False

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

                # --- Update reached_max flag ---
                # Once position hits 80, we flip this flag FOREVER (until new day).
                # This is the critical fix: it separates "initial accumulation"
                # (aggressive, sweep everything) from "post-max refill" (passive only).
                if position >= limit:
                    pepper_reached_max = True

                # =============================================================
                # BRANCH A: INITIAL ACCUMULATION (never reached +80 yet)
                # =============================================================
                # Sweep ALL asks. No price cap. The drift always pays.
                # Post aggressive passive bid at fair+2 for unfilled ticks.
                # NEVER post an ask (v4 lesson).
                #
                # This is ONLY used during the first accumulation from 0 to 80.
                # Once we've been at 80, we never enter this branch again.
                # This prevents the v8 bug where MM sells triggered accumulation
                # sweeps that rebought at full ask price.

                if not pepper_reached_max and state.timestamp < self.PEPPER_ACCUMULATION_CUTOFF:

                    # Sweep every ask in the book
                    for ask_price, ask_vol in bot_asks:
                        if buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break

                    # Passive bid for ticks with no asks
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                    # Check if we just hit max this tick
                    if position >= limit:
                        pepper_reached_max = True

                # =============================================================
                # BRANCH B: POST-MAX — HOLD + BOOK-RELATIVE MM
                # =============================================================
                # We've been at +80 before. Now we either:
                #   a) ARE at +80 → do MM (sell best_ask-1, passive bid best_bid+1)
                #   b) Are BELOW +80 from a previous MM sell → passive rebuy only
                #
                # CRITICAL: We NEVER sweep asks in this branch. All rebuys are
                # passive (best_bid+1). This is the v9 fix for the v8 bug.
                #
                # Why passive rebuy works:
                #   - Sell at best_ask-1 ≈ mid + 5.5 (top of spread)
                #   - Rebuy at best_bid+1 ≈ mid - 5.5 (bottom of spread)
                #   - Round-trip profit: ~11 per unit
                #   - v8 bug: rebuy swept at best_ask ≈ mid + 6.5, profit: -1/unit

                elif pepper_reached_max:

                    if best_bid is not None and best_ask is not None:
                        spread = best_ask - best_bid

                        if position >= limit and spread >= 4:
                            # AT +80: Post MM sell + passive rebuy bid
                            #
                            # SELL: undercut best ask by 1.
                            # We become the cheapest offer. Any incoming buy fills us.
                            mm_size = min(self.PEPPER_MM_SIZE, sell_room)
                            if mm_size > 0:
                                my_ask = best_ask - 1
                                orders.append(Order(product, my_ask, -mm_size))

                            # Also post a passive bid in case someone sells into us
                            # (rare but free money — we buy below fair in an uptrend)
                            # This bid is ONLY for bonus fills, not for MM rebuy.
                            # The MM rebuy bid is posted in the else branch below.

                        elif position < limit:
                            # BELOW +80 (from a previous MM sell): PASSIVE REBUY ONLY
                            #
                            # Post bid at best_bid + 1. This improves on the best bot
                            # bid by 1 tick, making us first in queue. Any incoming sell
                            # fills us at best_bid+1 (bottom of spread).
                            #
                            # We do NOT sweep asks. That was the v8 bug.
                            #
                            # If the rebuy takes too long (>100 ticks), we lose drift:
                            #   15 units * 0.001 * 10,000 ts = 150 lost drift
                            # But with round-trip profit of 15 * 11 = 165, we're still net +15.
                            # And v8 data showed rebuys take 1-20 ticks, not 100.
                            if buy_room > 0:
                                my_bid = best_bid + 1
                                orders.append(Order(product, my_bid, buy_room))

                            # ALSO still post MM sell if spread is wide enough.
                            # This lets us cycle continuously: sell → rebuy → sell → rebuy.
                            # While waiting for the rebuy, we might as well have a sell
                            # out there too. If both fill on the same tick = instant profit.
                            if spread >= 4 and sell_room > 0:
                                mm_size = min(self.PEPPER_MM_SIZE, sell_room)
                                if mm_size > 0:
                                    my_ask = best_ask - 1
                                    orders.append(Order(product, my_ask, -mm_size))

                        else:
                            # Spread too tight (< 4) but at +80: just hold.
                            # Don't risk MM in a tight spread.
                            pass

                    else:
                        # One-sided book: just post passive bid if below max
                        if buy_room > 0 and best_bid is not None:
                            my_bid = best_bid + 1
                            orders.append(Order(product, my_bid, buy_room))

                # =============================================================
                # BRANCH C: LATE-DAY (past cutoff, haven't reached max)
                # =============================================================
                # Unlikely scenario: past ts=90,000 and still not at +80.
                # Post a conservative bid at fair. Don't overpay this late.
                else:
                    if buy_room > 0:
                        orders.append(Order(product, fair_int, buy_room))

            # =================================================================
            # ASH_COATED_OSMIUM — TAKE + PASSIVE (v6 proven settings)
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                if best_bid is None or best_ask is None:
                    result[product] = orders
                    continue

                fair_int = self.OSMIUM_FAIR
                soft_limit = self.OSMIUM_SOFT_LIMIT

                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                is_panic_long = position >= self.OSMIUM_PANIC_THRESHOLD
                is_panic_short = position <= -self.OSMIUM_PANIC_THRESHOLD

                # --- TAKE mispriced orders (core alpha: ~1,572/day) ---
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and is_panic_short:
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
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # --- PASSIVE quotes with inventory skew (v6 settings) ---
                soft_buy_room = min(soft_limit - position, limit - position)
                soft_sell_room = min(soft_limit + position, limit + position)

                position_ratio = position / soft_limit if soft_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                # Skew factor 4 (v6 value, not v8's 6).
                # At +50: bid_offset = 3+4*1 = 7 (wide), ask_offset = 3-4*1 = -1→1 (tight)
                # At -50: bid_offset = 3+4*(-1) = -1→1 (tight), ask_offset = 3-4*(-1) = 7 (wide)
                # Gentler than skew 6 — allows position to unwind naturally without
                # making quotes so extreme that they trap the position.
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
            "pepper_reached_max": pepper_reached_max,  # v9 NEW: prevents accumulation sweeps after MM
        })

        conversions = 0

        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
