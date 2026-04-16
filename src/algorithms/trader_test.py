"""
=============================================================================
PROSPERITY 4 — ROUND 1 — TRADER_TEST
=============================================================================
Goal: push past trader.py's ~10k live result. Known-good top scores are
~14k, so there's ~40% of edge left on the table.

Observations about the "pin-at-80" approach in trader.py that likely hurt
live:
    1. Live has competing traders. Posting enormous resting bids fills
       worse than in the solo backtester.
    2. Pinning at +80 means every unfavorable tick is fully MTM-visible —
       a single adverse move of ~5 ticks wipes 400 of PnL.
    3. Our "fair" was raw mid, which is noisy. One wide-spread tick can
       move it ±5 ticks and trigger bad trades.

Techniques adopted here (each well-known in prior Prosperity rounds):

    A. MM-level fair price (`mm_fair`)
       Filter the book to LARGE-volume levels (≥ MM_SIZE_THRESHOLD) —
       these are the persistent bot market-makers. Use their midpoint
       as the stable reference. Falls back to mid if no large levels.

    B. Slope forecast
       Keep the last N mm_fairs in trader_data. Fit a simple linear
       slope (Σ(i·x) / Σ(i²)). Projected fair = mm_fair + slope * HORIZON.
       This lets us bias quotes in the direction the trend is actually
       moving RIGHT NOW, rather than assuming a fixed +1/1000 drift.

    C. Moderate LONG_TARGET with Avellaneda-Stoikov reservation
       Target +30 (not +80). This captures most of the trend carry while
       leaving room to participate on both sides and skew naturally.
       `fair_res = projected_fair - (pos - LONG_TARGET) * RES_K`

    D. Two-tier MAKE
       Tight quote (penny best, capped size) for queue priority, deep
       quote (at fair ± EDGE) for base flow. Same approach that worked
       well historically for RAINFOREST_RESIN.

    E. Signal-gated TAKE
       Only hit the book when ask < fair_res - EDGE_TAKE (or bid >
       fair_res + EDGE_TAKE). Single-tick edge isn't enough — live
       flow eats it.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# -----------------------------------------------------------------------------
# SHARED
# -----------------------------------------------------------------------------
POSITION_LIMIT = 80


# ---- OSMIUM (unchanged — simple emerald-style works live) --------------------
OSMIUM = "ASH_COATED_OSMIUM"
OSMIUM_FAIR = 10_000


# ---- PEPPER (new approach) ---------------------------------------------------
PEPPER = "INTARIAN_PEPPER_ROOT"

# Volume threshold for "this level is the true MM" — true MM tends to quote
# 10-30 lots per side in R1 data, small outliers are 1-5.
MM_SIZE_THRESHOLD = 10

# Rolling window of mm_fairs used to estimate trend slope. Short enough to
# react to regime changes, long enough to filter noise.
TREND_WINDOW = 30

# How many ticks ahead we project fair. Higher = more aggressive trend
# capture, more whiplash risk.
FORECAST_HORIZON = 5

# Soft inventory target. +50 earns most of the trend carry (if present live)
# while leaving ~30 units of headroom to participate on both sides. Sweep
# on 3 days of R1 data showed LT ∈ [50, 70] is on the same backtest plateau;
# picked the lower end to stay robust if live trend is weaker than training.
LONG_TARGET = 50

# Reservation coefficient on inventory deviation from LONG_TARGET.
RES_K = 0.15

# Quoting edges (ticks vs fair_res).
EDGE_TAKE = 1       # require > 1 tick of edge before crossing
TIGHT_EDGE = 2      # tight MAKE quote offset
DEEP_EDGE = 6       # deep MAKE quote offset

# Size cap on the tight-tier quote (queue priority; overflow goes deep).
TIGHT_CAP = 25


# =============================================================================
# LOGGER (unchanged)
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
# HELPERS
# =============================================================================
def mm_fair(order_depth: OrderDepth) -> float | None:
    """Midpoint between the highest-volume bid and ask (MM-level fair).
    Falls back to best-of-book mid if no large-volume levels exist."""
    big_bids = [p for p, v in order_depth.buy_orders.items() if v >= MM_SIZE_THRESHOLD]
    big_asks = [p for p, v in order_depth.sell_orders.items() if -v >= MM_SIZE_THRESHOLD]
    if big_bids and big_asks:
        return (max(big_bids) + min(big_asks)) / 2.0
    if order_depth.buy_orders and order_depth.sell_orders:
        return (max(order_depth.buy_orders) + min(order_depth.sell_orders)) / 2.0
    return None


def slope_of(seq: list[float]) -> float:
    """Least-squares slope of seq vs index (x = 0..n-1). 0 if < 3 points."""
    n = len(seq)
    if n < 3:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(seq) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(seq))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


# =============================================================================
# OSMIUM (unchanged from trader.py — emerald-style)
# =============================================================================
def trade_osmium(state: TradingState) -> List[Order]:
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

    my_bid = min(best_bid + 1, fair - 1)
    my_ask = max(best_ask - 1, fair + 1)

    if buy_room > 0:
        orders.append(Order(OSMIUM, my_bid, buy_room))
    if sell_room > 0:
        orders.append(Order(OSMIUM, my_ask, -sell_room))

    return orders


# =============================================================================
# PEPPER (new: mm_fair + slope forecast + moderate long target + two-tier)
# =============================================================================
def trade_pepper(state: TradingState, mm_history: list[float]) -> List[Order]:
    orders: List[Order] = []
    if PEPPER not in state.order_depths:
        return orders

    order_depth = state.order_depths[PEPPER]
    position = state.position.get(PEPPER, 0)
    buy_room = POSITION_LIMIT - position
    sell_room = POSITION_LIMIT + position

    bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
    bot_asks = sorted(order_depth.sell_orders.items())
    best_bid = bot_bids[0][0] if bot_bids else None
    best_ask = bot_asks[0][0] if bot_asks else None
    if best_bid is None or best_ask is None:
        return orders

    # A. MM-fair — filtered reference price.
    mmf = mm_fair(order_depth)
    if mmf is None:
        return orders

    # B. Slope forecast — project fair `FORECAST_HORIZON` ticks ahead.
    mm_history.append(mmf)
    if len(mm_history) > TREND_WINDOW:
        del mm_history[:len(mm_history) - TREND_WINDOW]
    slope = slope_of(mm_history)
    projected = mmf + slope * FORECAST_HORIZON

    # C. Reservation price on inventory.
    fair_res = projected - (position - LONG_TARGET) * RES_K

    fair_floor = int(fair_res)
    fair_ceil = int(fair_res + 0.999999)

    # E. Signal-gated TAKE — only hit clearly mispriced orders.
    for ask_price, ask_vol in bot_asks:
        if buy_room <= 0:
            break
        if ask_price <= fair_res - EDGE_TAKE:
            take = min(-ask_vol, buy_room)
            orders.append(Order(PEPPER, ask_price, take))
            buy_room -= take
            position += take
        else:
            break

    for bid_price, bid_vol in bot_bids:
        if sell_room <= 0:
            break
        if bid_price >= fair_res + EDGE_TAKE:
            take = min(bid_vol, sell_room)
            orders.append(Order(PEPPER, bid_price, -take))
            sell_room -= take
            position -= take
        else:
            break

    # D. Two-tier MAKE.
    tight_bid = min(best_bid + 1, fair_floor - TIGHT_EDGE)
    tight_ask = max(best_ask - 1, fair_ceil + TIGHT_EDGE)
    deep_bid = min(tight_bid - 1, fair_floor - DEEP_EDGE)
    deep_ask = max(tight_ask + 1, fair_ceil + DEEP_EDGE)

    tight_buy = min(buy_room, TIGHT_CAP)
    deep_buy = buy_room - tight_buy
    tight_sell = min(sell_room, TIGHT_CAP)
    deep_sell = sell_room - tight_sell

    if tight_buy > 0 and tight_bid > 0:
        orders.append(Order(PEPPER, tight_bid, tight_buy))
    if deep_buy > 0 and deep_bid > 0 and deep_bid < tight_bid:
        orders.append(Order(PEPPER, deep_bid, deep_buy))
    elif deep_buy > 0 and tight_bid > 0:
        orders.append(Order(PEPPER, tight_bid, deep_buy))

    if tight_sell > 0 and tight_ask > 0:
        orders.append(Order(PEPPER, tight_ask, -tight_sell))
    if deep_sell > 0 and deep_ask > tight_ask:
        orders.append(Order(PEPPER, deep_ask, -deep_sell))
    elif deep_sell > 0 and tight_ask > 0:
        orders.append(Order(PEPPER, tight_ask, -deep_sell))

    return orders


# =============================================================================
# TRADER
# =============================================================================
class Trader:
    def run(self, state: TradingState):
        # Restore mm_fair history from trader_data.
        mm_history: list[float] = []
        if state.traderData:
            try:
                mm_history = json.loads(state.traderData).get("mm_hist", [])
            except Exception:
                mm_history = []

        result: Dict[Symbol, List[Order]] = {
            OSMIUM: trade_osmium(state),
            PEPPER: trade_pepper(state, mm_history),
        }

        trader_data = json.dumps({"mm_hist": mm_history})
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass
        return result, conversions, trader_data
