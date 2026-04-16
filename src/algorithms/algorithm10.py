"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v10 (final, clean)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — linear price ramp (+0.001/tick), HOLD at +80
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000, market-make

RESULTS HISTORY:
    v3:  6,294  (PEPPER 5,089 + OSMIUM 1,205) — slow accumulation
    v6:  6,876  (PEPPER 5,396 + OSMIUM 1,480) — bias fix helped
    v8:  8,400  (PEPPER 7,219 + OSMIUM 1,266) — sweep-all + broken MM
    v9:  8,555  (PEPPER 7,162 + OSMIUM 1,393) — passive rebuy too slow

    MM ON PEPPER IS ALWAYS NET NEGATIVE:
      v6: sell fair+2, rebuy fair+2 → 0 profit, fast refill → -8 net
      v8: sell ask-1, rebuy sweeps ask → -1/unit, fast refill → -67 net
      v9: sell ask-1, rebuy passive bid+1 → +11/unit, slow refill → -415 net
    In an uptrending asset, any sell reduces drift exposure. The round-trip
    profit never exceeds the lost drift. Proven across 3 versions.

V10 — CLEAN FINAL VERSION:
    PEPPER: Sweep all asks to reach +80 ASAP. Then HOLD. No MM. No sells. Ever.
    OSMIUM: Take mispriced orders + passive quotes. v6 proven settings.

    This is the simplest possible strategy. No clever tricks, no overlays.
    Just buy fast and hold on PEPPER, take free money on OSMIUM.

EXPECTED PnL:
    PEPPER: ~7,500 (80 units × ~95 pts drift - 750 entry cost)
    OSMIUM: ~1,400 (takes at 4+ pts + passive spread capture)
    TOTAL:  ~8,900

Position limits: 80 for both products.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER — standard boilerplate
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]],
              conversions: int, trader_data: str) -> None:
        base_length = len(self.to_json([
            self.compress_state(state, ""), self.compress_orders(orders),
            conversions, "", "",
        ]))
        max_item_length = (self.max_log_length - base_length) // 3
        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders), conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))
        self.logs = ""

    def compress_state(self, state, trader_data):
        return [state.timestamp, trader_data,
                self.compress_listings(state.listings),
                self.compress_order_depths(state.order_depths),
                self.compress_trades(state.own_trades),
                self.compress_trades(state.market_trades),
                state.position,
                self.compress_observations(state.observations)]

    def compress_listings(self, listings):
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths):
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades):
        compressed = []
        for arr in trades.values():
            for t in arr:
                compressed.append([t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp])
        return compressed

    def compress_observations(self, observations):
        conversion_observations = {}
        try:
            for product, obs in observations.conversionObservations.items():
                conversion_observations[product] = [
                    obs.bidPrice, obs.askPrice, obs.transportFees,
                    obs.exportTariff, obs.importTariff,
                    getattr(obs, "sugarPrice", 0), getattr(obs, "sunlightIndex", 0)]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conversion_observations]

    def compress_orders(self, orders):
        compressed = []
        for arr in orders.values():
            for o in arr:
                compressed.append([o.symbol, o.price, o.quantity])
        return compressed

    def to_json(self, value):
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value, max_length):
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
# TRADER
# =============================================================================
class Trader:

    # =========================================================================
    # PEPPER CONFIG — buy fast, hold forever
    # =========================================================================

    # Drift rate: +0.001 per timestamp = +100 per day on the mid-price.
    PEPPER_SLOPE = 0.001

    # Bias correction: our linear model underestimates mid by ~1.5.
    # +2 (rounded up) makes our passive bids competitive during accumulation.
    PEPPER_FAIR_BIAS = 2

    # Passive bid premium when no asks available to sweep.
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # Stop accumulating near end of day. Remaining drift < spread cost.
    PEPPER_ACCUMULATION_CUTOFF = 90000

    # NO MM CONFIG. Proven net negative across v6/v8/v9. Just hold.

    # =========================================================================
    # OSMIUM CONFIG — v6 proven settings (scored 1,480)
    # =========================================================================

    OSMIUM_FAIR = 10000       # hardcoded fair, stable across all days
    OSMIUM_SOFT_LIMIT = 50    # self-imposed cap (exchange allows 80)
    OSMIUM_QUOTE_OFFSET = 3   # passive quotes at 9997/10003
    OSMIUM_SKEW_FACTOR = 4    # inventory skew (gentler than v8's 6)
    OSMIUM_PANIC_THRESHOLD = 40  # take at fair when |position| >= 40

    # =========================================================================
    # RUN
    # =========================================================================
    def run(self, state: TradingState):

        # --- Restore state ---
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)
        pepper_last_fair = stored.get("pepper_last_fair", None)

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
            # INTARIAN_PEPPER_ROOT — BUY FAST, HOLD FOREVER
            # =================================================================
            # Strategy is dead simple:
            #   1. Sweep every ask in the book until position = 80
            #   2. Post aggressive passive bid to catch incoming sells
            #   3. Once at 80: do absolutely nothing
            #   4. PnL = 80 units × price drift, marked-to-market at EOD
            #
            # No selling. No market-making. No clever tricks.
            # The drift is the alpha. Our only job is to be at +80 ASAP.
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                if best_bid is None and best_ask is None:
                    result[product] = orders
                    continue

                # --- Day boundary ---
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True
                pepper_last_timestamp = state.timestamp

                # --- Fair value (only needed for passive bid pricing) ---
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

                # --- Accumulate to +80 ---
                if position < limit and state.timestamp < self.PEPPER_ACCUMULATION_CUTOFF:

                    # Sweep ALL asks. No price cap.
                    # The drift always pays for the spread. Even at the worst
                    # ask price (mid+8 when spread=16), each unit earns 50+ pts
                    # of drift over a half-day. Paying 8 to earn 50 is obvious.
                    for ask_price, ask_vol in bot_asks:
                        if buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break

                    # Passive bid at fair+2 for ticks with no asks.
                    # Catches incoming sells while we wait for more supply.
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                # --- At +80: HOLD. Do nothing. ---
                # The drift earns 80 × 0.001 = 0.08 per timestamp automatically.
                # Over 100,000 timestamps: 80 × 100 = 8,000 gross.
                # Minus entry cost (~750): net ~7,250.
                # No orders needed. Mark-to-market at EOD captures all gains.
                #
                # elif position >= limit:
                #     pass  (literally nothing)

            # =================================================================
            # ASH_COATED_OSMIUM — TAKE + PASSIVE (v6 proven settings)
            # =================================================================
            # Two-layer strategy:
            #   Layer 1 (TAKE): Sweep any ask < 10000 or bid > 10000.
            #     These are guaranteed-profit trades against mean reversion.
            #     Core alpha: ~1,572/day at 4+ pts per trade.
            #   Layer 2 (PASSIVE): Post quotes at 9997/10003 with inventory skew.
            #     Earns small spread when filled. Skew pushes toward flat.
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

                # --- Layer 1: TAKE mispriced orders ---
                # Buy any ask below 10000 (cheap). Sell any bid above 10000 (expensive).
                # At panic levels, also take at exactly 10000 to reduce risk.
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

                # --- Layer 2: PASSIVE quotes with inventory skew ---
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

        # --- Persist state ---
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
