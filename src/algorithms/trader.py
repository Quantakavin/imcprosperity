"""
=============================================================================
PROSPERITY 4 — ROUND 1 — COMBINED TRADER
=============================================================================
Trades both Round 1 products in a single run() pass:

    ASH_COATED_OSMIUM       — mean-reverting around fair = 10,000
    INTARIAN_PEPPER_ROOT    — dynamic mid, linear +1/1000 ts upward drift

The two product strategies are independent — they don't share inventory
signals, so each block just reads its own order depth and emits its own
orders. See ash_coated_osmium.py and intarian_pepper_root.py for the
detailed rationale behind each.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# -----------------------------------------------------------------------------
# SHARED CONFIG
# -----------------------------------------------------------------------------
POSITION_LIMIT = 80


# ---- ASH_COATED_OSMIUM -------------------------------------------------------
OSMIUM = "ASH_COATED_OSMIUM"
OSMIUM_FAIR = 10_000


# ---- INTARIAN_PEPPER_ROOT ---------------------------------------------------
# LT=80 pins inventory at the cap to harvest the +1/1000 ts trend drift.
# Sweep on 3 days of R1 data: LT=80 beat LT=40 by +75% (243k vs 138k).
PEPPER = "INTARIAN_PEPPER_ROOT"
PEPPER_LONG_TARGET = 80
PEPPER_RES_K = 0.25
PEPPER_MIN_EDGE = 3


# =============================================================================
# LOGGER
# =============================================================================
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


# =============================================================================
# PER-PRODUCT STRATEGIES
# =============================================================================
def trade_osmium(state: TradingState) -> List[Order]:
    """Simple two-phase MM around fixed fair 10,000 (emerald-style)."""
    orders: List[Order] = []
    if OSMIUM not in state.order_depths:
        return orders

    order_depth: OrderDepth = state.order_depths[OSMIUM]
    position = state.position.get(OSMIUM, 0)
    buy_room = POSITION_LIMIT - position
    sell_room = POSITION_LIMIT + position

    bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
    bot_asks = sorted(order_depth.sell_orders.items())
    best_bid = bot_bids[0][0] if bot_bids else None
    best_ask = bot_asks[0][0] if bot_asks else None
    if best_bid is None or best_ask is None:
        return orders

    fair = OSMIUM_FAIR

    # PHASE 1 — TAKE (at fair only when it unwinds inventory)
    for ask_price, ask_vol in bot_asks:
        if buy_room <= 0:
            break
        if ask_price <= fair and position <= 0:
            take = min(-ask_vol, buy_room)
            orders.append(Order(OSMIUM, ask_price, take))
            buy_room -= take
            position += take
        elif ask_price < fair:
            take = min(-ask_vol, buy_room)
            orders.append(Order(OSMIUM, ask_price, take))
            buy_room -= take
            position += take
        else:
            break

    for bid_price, bid_vol in bot_bids:
        if sell_room <= 0:
            break
        if bid_price >= fair and position >= 0:
            take = min(bid_vol, sell_room)
            orders.append(Order(OSMIUM, bid_price, -take))
            sell_room -= take
            position -= take
        elif bid_price > fair:
            take = min(bid_vol, sell_room)
            orders.append(Order(OSMIUM, bid_price, -take))
            sell_room -= take
            position -= take
        else:
            break

    # PHASE 2 — MAKE: penny inside top-of-book, clamped past fair
    my_bid = min(best_bid + 1, fair - 1)
    my_ask = max(best_ask - 1, fair + 1)

    if buy_room > 0:
        orders.append(Order(OSMIUM, my_bid, buy_room))
    if sell_room > 0:
        orders.append(Order(OSMIUM, my_ask, -sell_room))

    return orders


def trade_pepper(state: TradingState) -> List[Order]:
    """Trend-biased MM targeting LONG_TARGET to harvest +1/1000 drift."""
    orders: List[Order] = []
    if PEPPER not in state.order_depths:
        return orders

    order_depth: OrderDepth = state.order_depths[PEPPER]
    position = state.position.get(PEPPER, 0)
    buy_room = POSITION_LIMIT - position
    sell_room = POSITION_LIMIT + position

    bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
    bot_asks = sorted(order_depth.sell_orders.items())
    best_bid = bot_bids[0][0] if bot_bids else None
    best_ask = bot_asks[0][0] if bot_asks else None
    if best_bid is None or best_ask is None:
        return orders

    raw_fair = (best_bid + best_ask) / 2.0
    fair = raw_fair - (position - PEPPER_LONG_TARGET) * PEPPER_RES_K
    fair_floor = int(fair)
    fair_ceil = int(fair + 0.999999)

    # PHASE 1 — TAKE
    for ask_price, ask_vol in bot_asks:
        if buy_room <= 0:
            break
        if ask_price < fair:
            take = min(-ask_vol, buy_room)
            orders.append(Order(PEPPER, ask_price, take))
            buy_room -= take
            position += take
        else:
            break

    for bid_price, bid_vol in bot_bids:
        if sell_room <= 0:
            break
        if bid_price > fair:
            take = min(bid_vol, sell_room)
            orders.append(Order(PEPPER, bid_price, -take))
            sell_room -= take
            position -= take
        else:
            break

    # PHASE 2 — MAKE
    my_bid = min(best_bid + 1, fair_floor - PEPPER_MIN_EDGE)
    my_ask = max(best_ask - 1, fair_ceil + PEPPER_MIN_EDGE)

    if buy_room > 0 and my_bid > 0:
        orders.append(Order(PEPPER, my_bid, buy_room))
    if sell_room > 0 and my_ask > 0:
        orders.append(Order(PEPPER, my_ask, -sell_room))

    return orders


# =============================================================================
# TRADER
# =============================================================================
class Trader:
    def run(self, state: TradingState):
        result: Dict[Symbol, List[Order]] = {
            OSMIUM: trade_osmium(state),
            PEPPER: trade_pepper(state),
        }

        trader_data = ""
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass
        return result, conversions, trader_data
