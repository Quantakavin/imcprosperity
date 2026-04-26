"""
=============================================================================
PROSPERITY 4 — ROUND 1 — OPTIMIZED COMBINED TRADER (V2)
=============================================================================
Data-driven improvements over trader.py (V1) for both products:

ASH_COATED_OSMIUM — mean-reverting around fair = 10,000
  Data findings:
    - Autocorrelation 0.93: deviations are very persistent (median 104-step reversion)
    - Std dev 5.5 from 10k, range [-18, +18]
    - Spread 16 (63% of time), bimodal
    - Book imbalance predicts next return: bid_heavy → +2.0, ask_heavy → -1.9
    - One-sided books: 8% of ticks (missed by V1)
  Improvements:
    1. Imbalance-adjusted fair value (data-backed short-term signal)
    2. One-sided book handling (trade available side using 10k as anchor)
    3. Relaxed at-fair taking threshold (take at <= fair when |position| < 30)

INTARIAN_PEPPER_ROOT — linear drift +1/1000 ts
  Data findings:
    - Residuals have ZERO autocorrelation → no mean reversion to exploit
    - Book imbalance predicts next return: bid_heavy → +1.7 to +2.0 (3 days)
    - Spread 12-14, one-sided 7.5%
    - PnL is dominated by drift capture (stay max long)
  Improvements:
    1. Imbalance-adjusted fair value
    2. One-sided book handling during position building
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# =============================================================================
# SHARED CONFIG
# =============================================================================
POSITION_LIMIT = 80

# Book imbalance coefficient — applied to both products.
# Measured: bid_heavy (imb > 0.3) → +2.0 avg return, ask_heavy → -1.9.
# We shift fair by IMBALANCE_K * imbalance to lean into the predicted move.
# Conservative at 1.5 (slightly below observed magnitude) to avoid overfitting.
IMBALANCE_K = 1.5


# ---- ASH_COATED_OSMIUM -------------------------------------------------------
OSMIUM = "ASH_COATED_OSMIUM"
OSMIUM_FAIR = 10_000
# V1 takes at fair only when unwinding (position on wrong side of 0).
# Relaxed: take at fair when |position| < this threshold.
# Allows building moderate positions during persistent deviations.
OSMIUM_AT_FAIR_THRESHOLD = 25


# ---- INTARIAN_PEPPER_ROOT ---------------------------------------------------
PEPPER = "INTARIAN_PEPPER_ROOT"
PEPPER_LONG_TARGET = 80
PEPPER_RES_K = 0.25
PEPPER_MIN_EDGE = 3


# =============================================================================
# LOGGER (identical to V1)
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
# HELPER: book imbalance
# =============================================================================
def compute_imbalance(order_depth: OrderDepth) -> float:
    """Compute L1 volume imbalance in [-1, +1]. Positive = bid-heavy (bullish)."""
    bids = order_depth.buy_orders
    asks = order_depth.sell_orders
    if not bids or not asks:
        return 0.0
    # Volume at best bid (highest price) and best ask (lowest price)
    best_bid_vol = bids[max(bids.keys())]          # positive
    best_ask_vol = -asks[min(asks.keys())]          # flip sign: sell_orders are negative
    total = best_bid_vol + best_ask_vol
    if total == 0:
        return 0.0
    return (best_bid_vol - best_ask_vol) / total


# =============================================================================
# PER-PRODUCT STRATEGIES
# =============================================================================
def trade_osmium(state: TradingState) -> List[Order]:
    """
    Mean-reversion MM around fair=10,000 with imbalance signal.

    Changes from V1:
    1. fair += IMBALANCE_K * imbalance (short-term directional signal)
    2. One-sided book: trade available side using 10k as anchor
    3. Relaxed at-fair taking: take at <= fair when |position| < 25
       (V1 only takes at fair when unwinding, i.e. position on wrong side of 0)
    """
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

    # --- Improvement: handle one-sided books ---
    # V1 returns empty here. We can still trade the available side
    # using 10k as the anchor fair value.
    if best_bid is None and best_ask is None:
        return orders

    # --- Imbalance-adjusted fair ---
    imbalance = compute_imbalance(order_depth)
    fair = OSMIUM_FAIR + IMBALANCE_K * imbalance

    # PHASE 1 — TAKE
    # Improvement: take at <= fair when |position| < threshold (not just when unwinding).
    # V1 logic: buy <= fair ONLY when position <= 0 (unwind), else buy < fair.
    # New logic: buy <= fair when position < THRESHOLD (build moderate positions).
    for ask_price, ask_vol in bot_asks:
        if buy_room <= 0:
            break
        if ask_price <= fair and position < OSMIUM_AT_FAIR_THRESHOLD:
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
        if bid_price >= fair and position > -OSMIUM_AT_FAIR_THRESHOLD:
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
    # Use integer fair bounds for price placement
    fair_bid_clamp = int(fair) if fair != int(fair) else int(fair) - 1  # strictly below fair
    fair_ask_clamp = int(fair) + 1 if fair == int(fair) else int(fair) + 1  # strictly above fair

    if best_bid is not None and best_ask is not None:
        my_bid = min(best_bid + 1, fair_bid_clamp)
        my_ask = max(best_ask - 1, fair_ask_clamp)
    elif best_ask is not None:
        # Only asks: post a bid to buy (we know fair is ~10000)
        my_bid = fair_bid_clamp
        my_ask = best_ask + 5  # wide ask, unlikely to fill (we want to buy)
    elif best_bid is not None:
        # Only bids: post an ask to sell
        my_bid = best_bid - 5  # wide bid
        my_ask = fair_ask_clamp
    else:
        return orders

    if buy_room > 0 and my_bid > 0:
        orders.append(Order(OSMIUM, my_bid, buy_room))
    if sell_room > 0 and my_ask > 0:
        orders.append(Order(OSMIUM, my_ask, -sell_room))

    return orders


def trade_pepper(state: TradingState, persisted: dict) -> List[Order]:
    """
    Drift-capturing MM targeting position 80 with imbalance signal.

    Changes from V1:
    1. fair += IMBALANCE_K * imbalance (short-term signal, +2 avg return for bid-heavy)
    2. One-sided book: use last known mid for fair estimate during position building
    """
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

    # --- Compute mid price ---
    if best_bid is not None and best_ask is not None:
        raw_mid = (best_bid + best_ask) / 2.0
        persisted["pm"] = raw_mid  # save last known pepper mid
        persisted["pt"] = state.timestamp
    elif best_ask is not None and position < PEPPER_LONG_TARGET and "pm" in persisted:
        # Only asks visible AND we need to buy: project mid from last known + drift
        raw_mid = persisted["pm"] + (state.timestamp - persisted.get("pt", state.timestamp)) * 0.001
    else:
        # No usable data — skip (same as V1)
        # Includes: bid-only books (we don't want to sell on incomplete info),
        # ask-only when at target, and no book at all.
        return orders

    # --- Imbalance-adjusted fair ---
    imbalance = compute_imbalance(order_depth)
    raw_fair = raw_mid + IMBALANCE_K * imbalance
    fair = raw_fair - (position - PEPPER_LONG_TARGET) * PEPPER_RES_K
    fair_floor = int(fair)
    fair_ceil = int(fair + 0.999999)

    # PHASE 1 — TAKE (identical structure to V1, but with imbalance in fair)
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

    # PHASE 2 — MAKE (identical structure to V1, but with imbalance in fair)
    if best_bid is not None and best_ask is not None:
        my_bid = min(best_bid + 1, fair_floor - PEPPER_MIN_EDGE)
        my_ask = max(best_ask - 1, fair_ceil + PEPPER_MIN_EDGE)
    elif best_ask is not None and buy_room > 0:
        # Only asks visible, still building position: post a buy
        my_bid = fair_floor - PEPPER_MIN_EDGE
        my_ask = fair_ceil + PEPPER_MIN_EDGE  # no best_ask ref for clamping
    else:
        return orders

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
        # Deserialize persistent state (used by pepper for one-sided book handling)
        try:
            persisted = json.loads(state.traderData) if state.traderData else {}
        except (json.JSONDecodeError, TypeError):
            persisted = {}

        result: Dict[Symbol, List[Order]] = {
            OSMIUM: trade_osmium(state),
            PEPPER: trade_pepper(state, persisted),
        }

        trader_data = json.dumps(persisted)
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass
        return result, conversions, trader_data
