"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v15
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — linear drift +0.001/tick, buy+hold
    2. ASH_COATED_OSMIUM     — mean-reverts ~10,000, take+passive MM

V15:
    Start from v11 and test one hidden-alpha idea only:

    OSMIUM imbalance is used as a PASSIVE QUOTING filter, not a fair-value
    model. Strong positive imbalance predicts short-horizon upward pressure;
    strong negative imbalance predicts downward pressure. So:

      - if the book is strongly bid-heavy, do not lean into it with a passive ask
      - if the book is strongly ask-heavy, do not lean into it with a passive bid

    Strong takes remain unchanged. Fair remains fixed at 10,000. Pepper remains
    unchanged. This keeps the bot robust while testing whether the main leak in
    v11 is adverse passive fills on the wrong side of an imbalanced book.
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
    # PEPPER — buy fast, hold forever
    # =========================================================================
    PEPPER_SLOPE = 0.001
    PEPPER_FAIR_BIAS = 2
    PEPPER_PASSIVE_BID_PREMIUM = 2
    PEPPER_ACCUMULATION_CUTOFF = 90000

    # =========================================================================
    # OSMIUM — take + passive MM
    # =========================================================================
    OSMIUM_FAIR = 10000

    # v11 CHANGE: Two separate position limits.
    #
    # TAKE_LIMIT: used when sweeping mispriced orders (asks < 10000, bids > 10000).
    #   Set to the FULL exchange limit (80). These trades have 4+ pts of known edge.
    #   Even at position +60, buying an ask at 9996 gives 4 pts of guaranteed profit
    #   once the price mean-reverts to 10000. The edge is large enough to justify
    #   the position risk.
    #
    # PASSIVE_LIMIT: used for resting quotes (bid at 9997, ask at 10003).
    #   Set to 50. Passive fills might be adverse selection — the counterparty
    #   knows something we don't. Keep exposure limited for these speculative fills.
    #
    # v10 used soft_limit=50 for BOTH. This blocked 12 profitable takes worth ~225.
    OSMIUM_TAKE_LIMIT = 80
    OSMIUM_PASSIVE_LIMIT = 50

    OSMIUM_QUOTE_OFFSET = 3   # passive quotes at fair ± 3 (9997/10003)
    OSMIUM_SKEW_FACTOR = 4    # inventory skew (v6 proven)
    OSMIUM_IMBALANCE_SUPPRESS = 0.25

    # v11 CHANGE: Panic threshold 40 → 30.
    # Unwind earlier to free up take capacity. At |pos|=30:
    #   - 3pt adverse move = 90 loss (acceptable)
    #   - Trading at fair to get back to 30 costs 0 profit per unit
    #   - But frees up 50 units of take capacity (80-30=50 room for takes)
    # At |pos|=40 (v10): only 40 units of take capacity remained.
    OSMIUM_PANIC_THRESHOLD = 30

    # =========================================================================
    # RUN
    # =========================================================================
    def run(self, state: TradingState):

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

                # --- Accumulate to +80, then hold ---
                if position < limit and state.timestamp < self.PEPPER_ACCUMULATION_CUTOFF:
                    for ask_price, ask_vol in bot_asks:
                        if buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                # At +80 or past cutoff: do nothing. Mark-to-market at EOD.

            # =================================================================
            # ASH_COATED_OSMIUM — TAKE + PASSIVE MM
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # v11 CHANGE: Handle 1-sided book.
                # v10 skipped these ticks entirely (84 ticks, 8.4% of day).
                # Now we still take mispriced orders on the visible side and
                # post a passive quote. This recovers ~76 in missed PnL.
                #
                # If BOTH sides empty: skip (nothing to do).
                # If only one side: take if mispriced, post passive on that side.
                if best_bid is None and best_ask is None:
                    result[product] = orders
                    continue

                fair_int = self.OSMIUM_FAIR

                if best_bid is not None and best_ask is not None:
                    best_bid_vol = bot_bids[0][1]
                    best_ask_vol = -bot_asks[0][1]
                    denom = best_bid_vol + best_ask_vol
                    imbalance = (best_bid_vol - best_ask_vol) / denom if denom > 0 else 0.0
                elif best_bid is not None:
                    imbalance = 1.0
                elif best_ask is not None:
                    imbalance = -1.0
                else:
                    imbalance = 0.0

                # --- Compute room for TAKES (full exchange limit) ---
                # v11: takes use the full ±80 limit. If someone sells at 9996,
                # that's 4 points of guaranteed profit. Buy it even at position +60.
                take_buy_room = limit - position   # up to +80
                take_sell_room = limit + position   # down to -80

                # --- Compute room for PASSIVE quotes (soft limit) ---
                # Passive fills are speculative — cap at ±50.
                passive_limit = self.OSMIUM_PASSIVE_LIMIT
                passive_buy_room = min(passive_limit - position, buy_room)
                passive_sell_room = min(passive_limit + position, sell_room)

                # Clamp passive room to non-negative
                passive_buy_room = max(0, passive_buy_room)
                passive_sell_room = max(0, passive_sell_room)

                # --- Panic flags (v11: threshold lowered to 30) ---
                is_panic_long = position >= self.OSMIUM_PANIC_THRESHOLD
                is_panic_short = position <= -self.OSMIUM_PANIC_THRESHOLD

                # --- Layer 1: TAKE mispriced orders (full ±80 limit) ---
                # Buy any ask < 10000. Sell any bid > 10000.
                # At panic: also trade at exactly 10000 to reduce position.

                if best_ask is not None:
                    for ask_price, ask_vol in bot_asks:
                        if ask_price < fair_int and take_buy_room > 0:
                            take_qty = min(-ask_vol, take_buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            take_buy_room -= take_qty
                            position += take_qty
                        elif ask_price == fair_int and take_buy_room > 0 and is_panic_short:
                            take_qty = min(-ask_vol, take_buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            take_buy_room -= take_qty
                            position += take_qty
                        else:
                            break

                if best_bid is not None:
                    for bid_price, bid_vol in bot_bids:
                        if bid_price > fair_int and take_sell_room > 0:
                            take_qty = min(bid_vol, take_sell_room)
                            orders.append(Order(product, bid_price, -take_qty))
                            take_sell_room -= take_qty
                            position -= take_qty
                        elif bid_price == fair_int and take_sell_room > 0 and is_panic_long:
                            take_qty = min(bid_vol, take_sell_room)
                            orders.append(Order(product, bid_price, -take_qty))
                            take_sell_room -= take_qty
                            position -= take_qty
                        else:
                            break

                # --- Layer 2: PASSIVE quotes (soft ±50 limit) ---
                # Recalculate passive room after takes (position changed).
                passive_buy_room = min(passive_limit - position, limit - position)
                passive_sell_room = min(passive_limit + position, limit + position)
                passive_buy_room = max(0, passive_buy_room)
                passive_sell_room = max(0, passive_sell_room)

                # Inventory skew: push position toward zero.
                # Uses position relative to passive_limit for ratio calculation.
                position_ratio = position / passive_limit if passive_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

                my_bid = min(my_bid, fair_int - 1)
                my_ask = max(my_ask, fair_int + 1)

                # V15: suppress the passive side that fights strong short-term pressure.
                suppress_bid = imbalance <= -self.OSMIUM_IMBALANCE_SUPPRESS
                suppress_ask = imbalance >= self.OSMIUM_IMBALANCE_SUPPRESS

                # Post passive quotes (even on 1-sided book ticks)
                if passive_buy_room > 0 and not suppress_bid:
                    orders.append(Order(product, my_bid, passive_buy_room))
                if passive_sell_room > 0 and not suppress_ask:
                    orders.append(Order(product, my_ask, -passive_sell_room))

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
