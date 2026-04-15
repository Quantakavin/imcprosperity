"""
=============================================================================
PROSPERITY 4 — ROUND 1 — INTARIAN_PEPPER_ROOT
=============================================================================
Product profile (derived from 3 days of R1 data):

    Fair value is a LINE, not a constant.
        Slope confirmed at +1.000 / 1000 ts on all three days (day -2, -1, 0).
        Day-end mid = day-start mid of next day, so the line is continuous.
        Detrended residual:
            std = 2.36, autocorr lag-1 = 0.003 (pure noise around trend)

    Typical book shape (day 0):
        bid1  ≈ trend - 5.6
        ask1  ≈ trend + 8.5
        spread ≈ 13-14
        volumes 10-12 at each side

Strategy ("trend-biased market maker"):

    1. DYNAMIC FAIR = midpoint of top-of-book. Since the detrended residual
       is nearly white noise, the instantaneous mid is an unbiased estimator
       of the trend line at that moment. No need to fit the line explicitly.

    2. LONG BIAS for trend capture. The known +1/1000 drift means every
       unit of average long inventory produces +1 PnL per 1000 ts. At a
       target of +40 across a 10k-ts day that's ~+400 of trend carry alone.
       We encode this by targeting position = LONG_TARGET rather than 0
       in the reservation-price formula.

    3. RESERVATION PRICE: fair is shifted opposite to (position - LONG_TARGET):
           fair_adj = mid - round((position - LONG_TARGET) * RES_K)
       Below target → fair_adj > mid → willing to buy higher, sells reluctant.
       Above target → fair_adj < mid → willing to sell lower, buys reluctant.

    4. TAKE + MAKE mechanics mirror the osmium strategy. Positions capped
       at ±80 (exchange limit). The soft band around LONG_TARGET is what
       keeps us trend-captured without drifting to the hard cap.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
PRODUCT = "INTARIAN_PEPPER_ROOT"
POSITION_LIMIT = 80

# Long-inventory target: pinned at the position cap. The trend is
# +1.000/1000 ts across all 3 days of data, so every unit of long inventory
# earns +1 PnL per 1000 ts deterministically. At 80 long we harvest ~800
# of free trend carry per 10k-ts day. Sweep (LT × K × ME over 3 days of
# R1 data) showed LT=80 beats LT=40 by +75% (243k vs 138k), with the
# plateau flat across K∈[0.15, 0.50] and ME∈[2, 5].
LONG_TARGET = 80

# Reservation coefficient. At LT=80 we're always at or below target, so
# the shift only widens the ask and never restrains the bid — K controls
# how much premium we demand when partly unwound.
RES_K = 0.25

# Minimum edge (ticks) we require vs fair_adj before placing a quote.
# The bot spread is ~13, so a 3-tick minimum still leaves us well inside
# the bot quotes (4+ ticks from best_bid/best_ask).
MIN_EDGE = 3


# =============================================================================
# LOGGER (standard visualizer helper — same as other algos)
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
# TRADER
# =============================================================================
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

        # DYNAMIC FAIR = current book mid (float; rounded only when
        # compared to integer prices). Residual autocorr is ~0 so the
        # instantaneous mid is as good as any smoothed estimate.
        raw_fair = (best_bid + best_ask) / 2.0

        # Trend-biased reservation price: target LONG_TARGET rather than 0.
        fair = raw_fair - (position - LONG_TARGET) * RES_K

        # ====================================================================
        # PHASE 1 — TAKE any ask below fair or bid above fair.
        # ====================================================================
        # Use > / < strictly; "==" would require fractional fair comparison
        # which is messy. Floor/ceil to ints for comparisons.
        fair_floor = int(fair)       # used for "price < fair" comparisons
        fair_ceil = int(fair + 0.999999)

        for ask_price, ask_vol in bot_asks:
            if buy_room <= 0:
                break
            if ask_price < fair:
                take = min(-ask_vol, buy_room)
                orders.append(Order(PRODUCT, ask_price, take))
                buy_room -= take
                position += take
            else:
                break

        for bid_price, bid_vol in bot_bids:
            if sell_room <= 0:
                break
            if bid_price > fair:
                take = min(bid_vol, sell_room)
                orders.append(Order(PRODUCT, bid_price, -take))
                sell_room -= take
                position -= take
            else:
                break

        # ====================================================================
        # PHASE 2 — MAKE at fair ± MIN_EDGE, pennied inside best bot quote.
        # ====================================================================
        my_bid = min(best_bid + 1, fair_floor - MIN_EDGE)
        my_ask = max(best_ask - 1, fair_ceil + MIN_EDGE)

        if buy_room > 0 and my_bid > 0:
            orders.append(Order(PRODUCT, my_bid, buy_room))
        if sell_room > 0 and my_ask > 0:
            orders.append(Order(PRODUCT, my_ask, -sell_room))

        result[PRODUCT] = orders

        trader_data = ""
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass
        return result, conversions, trader_data
