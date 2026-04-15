"""
=============================================================================
PROSPERITY 4 — ROUND 1 — ASH_COATED_OSMIUM
=============================================================================
Product profile (derived from prices_round_1_day_{-2,-1,0}.csv):
    - Fair value is anchored at 10,000
        mean   = 10000.20   median = 10000.5   std ≈ 5.35
        1%/99% percentiles = 9987 / 10013
    - Dominant "true" bot market maker quotes 9992 / 10008 (spread 16)
        When mid == 10000, 409/618 ticks have bid=9992 & ask=10008.
    - The outer book regularly crosses fair: across 3 days there are
        ~1,400 asks priced strictly below 10,000 and ~1,300 bids strictly
        above 10,000 — these are free-money "take" opportunities.
    - Position limit: 80 (long or short).

Strategy (two-phase: TAKE then MAKE):

  PHASE 1 — TAKE mispriced orders already in the book
      Fair is fixed at 10,000, so any ask < 10,000 is an instant profit buy,
      and any bid > 10,000 is an instant profit sell. We also hit orders
      AT fair (10,000) only when doing so unwinds inventory (soft inventory
      control — keeps us from loading up further in our current direction).

  PHASE 2 — MAKE: post passive quotes one tick inside the best bot quote
      With bot spread typically ~16, pennying to (best_bid+1, best_ask-1)
      still leaves us a wide edge vs the 10,000 fair. We clamp so we never
      quote a bid >= 10000 or an ask <= 10000.

      Inventory skew: when we're heavily long, we make it easier to sell
      (more aggressive ask, less aggressive bid) and vice versa. This keeps
      inventory from running away and getting stuck at the position limit.
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
PRODUCT = "ASH_COATED_OSMIUM"
POSITION_LIMIT = 80
FAIR_VALUE = 10_000

# Inventory skew thresholds — when |position| exceeds this fraction of the
# limit, we widen the quote on the "loading" side and tighten the unwind side.
SKEW_THRESHOLD = 40          # start skewing when |pos| > 40 (half the limit)
HARD_SKEW_THRESHOLD = 65     # aggressive skew when |pos| > 65


# =============================================================================
# LOGGER (standard Prosperity visualizer helper — unchanged from template)
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

    def compress_listings(self, listings):
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths):
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades):
        return [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
                for arr in trades.values() for t in arr]

    def compress_observations(self, observations):
        conv_obs = {}
        try:
            for p, o in observations.conversionObservations.items():
                conv_obs[p] = [o.bidPrice, o.askPrice, o.transportFees,
                               o.exportTariff, o.importTariff,
                               getattr(o, "sugarPrice", 0),
                               getattr(o, "sunlightIndex", 0)]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conv_obs]

    def compress_orders(self, orders):
        return [[o.symbol, o.price, o.quantity]
                for arr in orders.values() for o in arr]

    def to_json(self, value):
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
# TRADER
# =============================================================================
class Trader:
    def run(self, state: TradingState):
        result: Dict[Symbol, List[Order]] = {}

        if PRODUCT not in state.order_depths:
            return result, 0, ""

        order_depth: OrderDepth = state.order_depths[PRODUCT]
        orders: List[Order] = []

        # --- Position bookkeeping -------------------------------------------
        position = state.position.get(PRODUCT, 0)
        buy_room = POSITION_LIMIT - position
        sell_room = POSITION_LIMIT + position

        # --- Book snapshot --------------------------------------------------
        # bot_asks have NEGATIVE volume per IMC convention.
        bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
        bot_asks = sorted(order_depth.sell_orders.items())

        best_bid = bot_bids[0][0] if bot_bids else None
        best_ask = bot_asks[0][0] if bot_asks else None

        if best_bid is None or best_ask is None:
            result[PRODUCT] = orders
            return result, 0, ""

        fair = FAIR_VALUE

        # ====================================================================
        # PHASE 1 — TAKE: hit mispriced orders in the book
        # ====================================================================
        # Buy any ask strictly below fair. Also buy AT fair only if we're flat
        # or short (we don't want to accumulate more long exposure at fair).
        for ask_price, ask_vol in bot_asks:
            if buy_room <= 0:
                break
            ask_size = -ask_vol  # convert to positive
            if ask_price < fair:
                take = min(ask_size, buy_room)
                orders.append(Order(PRODUCT, ask_price, take))
                buy_room -= take
                position += take
            elif ask_price == fair and position < 0:
                # Unwind short at exactly fair — neutral EV, strictly reduces risk.
                take = min(ask_size, buy_room, -position)
                if take > 0:
                    orders.append(Order(PRODUCT, ask_price, take))
                    buy_room -= take
                    position += take
                break
            else:
                break  # sorted ascending, rest is worse

        # Sell any bid strictly above fair. Sell AT fair only if we're long.
        for bid_price, bid_vol in bot_bids:
            if sell_room <= 0:
                break
            if bid_price > fair:
                take = min(bid_vol, sell_room)
                orders.append(Order(PRODUCT, bid_price, -take))
                sell_room -= take
                position -= take
            elif bid_price == fair and position > 0:
                take = min(bid_vol, sell_room, position)
                if take > 0:
                    orders.append(Order(PRODUCT, bid_price, -take))
                    sell_room -= take
                    position -= take
                break
            else:
                break

        # ====================================================================
        # PHASE 2 — MAKE: post passive quotes inside the bot spread
        # ====================================================================
        # Default: penny the best bot quote by 1 tick.
        my_bid = best_bid + 1
        my_ask = best_ask - 1

        # Safety clamp: never quote through fair value.
        #   bid must be <= 9999, ask must be >= 10001.
        my_bid = min(my_bid, fair - 1)
        my_ask = max(my_ask, fair + 1)

        # If pennying would collide with the bot on the same side (e.g. best
        # bot ask is 10001 so penny would be 10000 -> clamped to 10001 = same
        # price), we still post at the clamped price — effectively joining
        # the bot quote. That's fine: we still earn the spread vs fair.

        # Inventory skew: if we're long and approaching the limit, quote a
        # less competitive bid (back off buying) and a more competitive ask
        # (encourage selling). Mirror for short. This prevents one-sided
        # fills from pinning us at +/- 80.
        if position > HARD_SKEW_THRESHOLD:
            # Strongly long: pull bid back by 2, push ask inward by 1.
            my_bid = min(my_bid, fair - 3)
            my_ask = max(fair + 1, my_ask - 1)
        elif position > SKEW_THRESHOLD:
            my_bid = min(my_bid, fair - 2)
        elif position < -HARD_SKEW_THRESHOLD:
            my_ask = max(my_ask, fair + 3)
            my_bid = min(fair - 1, my_bid + 1)
        elif position < -SKEW_THRESHOLD:
            my_ask = max(my_ask, fair + 2)

        # Post the quotes using ALL remaining capacity on each side. Full-size
        # quoting maximizes fills; the TAKE phase next tick will unwind any
        # one-sided inventory whenever the book crosses fair.
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