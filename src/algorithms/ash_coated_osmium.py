"""
=============================================================================
PROSPERITY 4 — ROUND 1 — ASH_COATED_OSMIUM
=============================================================================
Product profile (derived from prices_round_1_day_{-2,-1,0}.csv):
    - Fair value is anchored at 10,000
        mean   = 10000.20   median = 10000.5   std ≈ 5.35
        1%/99% percentiles = 9987 / 10013
    - Dominant bot market maker quotes 9992 / 10008 (spread 16).
    - The outer book regularly crosses fair — there are ~1,400 asks
      below 10,000 and ~1,300 bids above 10,000 across 3 days. Free
      "take" opportunities.
    - Position limit: 80 (long or short).

Strategy (emerald-style two-phase — simple beats clever in production):

  PHASE 1 — TAKE
      ask < fair                 → buy
      ask == fair AND pos <= 0   → buy (unwinds short; neutral EV otherwise)
      bid > fair                 → sell
      bid == fair AND pos >= 0   → sell (unwinds long)

  PHASE 2 — MAKE
      Penny: best_bid+1 / best_ask-1, clamped so bid < fair and ask > fair.
      One-shot size = all remaining buy_room / sell_room. Single tier —
      multi-tier + skew had higher backtest PnL but underperformed live.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


PRODUCT = "ASH_COATED_OSMIUM"
POSITION_LIMIT = 80
FAIR_VALUE = 10_000


class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state, orders, conversions, trader_data):
        base_length = len(self.to_json([
            self.compress_state(state, ""),
            self.compress_orders(orders),
            conversions, "", "",
        ]))
        max_item_length = (self.max_log_length - base_length) // 3
        print(self.to_json([
            self.compress_state(state, self.truncate(state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))
        self.logs = ""

    def compress_state(self, state, trader_data):
        return [
            state.timestamp, trader_data,
            [[l.symbol, l.product, l.denomination] for l in state.listings.values()],
            {s: [od.buy_orders, od.sell_orders] for s, od in state.order_depths.items()},
            [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
             for arr in state.own_trades.values() for t in arr],
            [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
             for arr in state.market_trades.values() for t in arr],
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_observations(self, observations):
        conv = {}
        try:
            for p, o in observations.conversionObservations.items():
                conv[p] = [o.bidPrice, o.askPrice, o.transportFees,
                           o.exportTariff, o.importTariff,
                           getattr(o, "sugarPrice", 0),
                           getattr(o, "sunlightIndex", 0)]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conv]

    def compress_orders(self, orders):
        return [[o.symbol, o.price, o.quantity]
                for arr in orders.values() for o in arr]

    def to_json(self, v):
        return json.dumps(v, cls=ProsperityEncoder, separators=(",", ":"))

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


class Trader:
    def run(self, state: TradingState):
        result: Dict[Symbol, List[Order]] = {}

        if PRODUCT not in state.order_depths:
            return result, 0, ""

        order_depth: OrderDepth = state.order_depths[PRODUCT]
        orders: List[Order] = []

        position = state.position.get(PRODUCT, 0)
        buy_room = POSITION_LIMIT - position
        sell_room = POSITION_LIMIT + position

        bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
        bot_asks = sorted(order_depth.sell_orders.items())
        best_bid = bot_bids[0][0] if bot_bids else None
        best_ask = bot_asks[0][0] if bot_asks else None
        if best_bid is None or best_ask is None:
            result[PRODUCT] = orders
            return result, 0, ""

        fair = FAIR_VALUE

        # PHASE 1 — TAKE
        for ask_price, ask_vol in bot_asks:
            if buy_room <= 0:
                break
            if ask_price <= fair and position <= 0:
                take = min(-ask_vol, buy_room)
                orders.append(Order(PRODUCT, ask_price, take))
                buy_room -= take
                position += take
            elif ask_price < fair:
                take = min(-ask_vol, buy_room)
                orders.append(Order(PRODUCT, ask_price, take))
                buy_room -= take
                position += take
            else:
                break

        for bid_price, bid_vol in bot_bids:
            if sell_room <= 0:
                break
            if bid_price >= fair and position >= 0:
                take = min(bid_vol, sell_room)
                orders.append(Order(PRODUCT, bid_price, -take))
                sell_room -= take
                position -= take
            elif bid_price > fair:
                take = min(bid_vol, sell_room)
                orders.append(Order(PRODUCT, bid_price, -take))
                sell_room -= take
                position -= take
            else:
                break

        # PHASE 2 — MAKE: penny inside top-of-book, clamped strictly past fair
        my_bid = min(best_bid + 1, fair - 1)
        my_ask = max(best_ask - 1, fair + 1)

        if buy_room > 0:
            orders.append(Order(PRODUCT, my_bid, buy_room))
        if sell_room > 0:
            orders.append(Order(PRODUCT, my_ask, -sell_room))

        result[PRODUCT] = orders

        trader_data = ""
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass
        return result, conversions, trader_data
