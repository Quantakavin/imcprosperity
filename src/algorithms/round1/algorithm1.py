"""
=============================================================================
PROSPERITY 4 - TUTORIAL ROUND TRADER (annotated for learning)
=============================================================================
Strategy in one sentence:
    Market-make on EMERALDS and TOMATOES by (1) taking mispriced orders
    already in the book, then (2) posting our own bid/ask one tick inside
    the best bot quotes to earn the spread passively.
=============================================================================
"""

import json
from typing import Any, List, Dict

# These come from datamodel.py provided by IMC. They define the shape of
# everything the exchange sends us and everything we send back.
#   - Order: an order we want to place (symbol, price, qty; +qty=buy, -qty=sell)
#   - OrderDepth: the current book for one product (buy_orders + sell_orders dicts)
#   - TradingState: the full snapshot the exchange gives us each tick
#   - Trade, Listing, Observation: other stuff in the snapshot
#   - ProsperityEncoder: a JSON encoder that knows how to serialize the above
from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)

# -----------------------------------------------------------------------------
# POSITION LIMITS
# -----------------------------------------------------------------------------
# The exchange enforces these. If our orders, summed up, would push us past
# these limits, ALL our orders for that product get rejected. So we must
# always track how much "room" we have left before placing orders.
# Tutorial round: both products capped at 80 (long or short).
POSITION_LIMITS = {
    "EMERALDS": 80,
    "TOMATOES": 80,
}


# =============================================================================
# LOGGER CLASS
# =============================================================================
# This is NOT part of the trading strategy. It's a standard helper from the
# open-source Prosperity visualizer (jmerle/imc-prosperity-visualizer on GitHub).
# It packages up the state + our orders into a single compressed JSON line
# printed to stdout. The visualizer tool then parses those lines so you can
# replay your run in a nice GUI. You can mostly ignore this class — just know
# that `logger.flush(...)` at the end of run() is what makes the GUI work.
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        # The exchange truncates log output above ~3750 chars per tick, so
        # we have to be careful not to overflow.
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        # Drop-in replacement for print() that buffers into self.logs
        # instead of printing immediately. Lets us batch everything into
        # one JSON blob at the end of the tick.
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]],
              conversions: int, trader_data: str) -> None:
        # Computes how much space we have left after the fixed parts of the
        # JSON, then divides that budget across the three "free text" fields
        # (traderData, our own logs, etc.) so nothing overflows.
        base_length = len(
            self.to_json([
                self.compress_state(state, ""),
                self.compress_orders(orders),
                conversions,
                "",
                "",
            ])
        )
        max_item_length = (self.max_log_length - base_length) // 3

        # Print one big JSON array per tick. The visualizer reads stdout and
        # picks up these lines.
        print(
            self.to_json([
                self.compress_state(state, self.truncate(state.traderData, max_item_length)),
                self.compress_orders(orders),
                conversions,
                self.truncate(trader_data, max_item_length),
                self.truncate(self.logs, max_item_length),
            ])
        )
        self.logs = ""  # reset buffer for next tick

    # The compress_* methods below just turn the verbose objects into compact
    # arrays so the JSON stays small. Boring serialization plumbing.
    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp,
            trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        compressed = []
        for listing in listings.values():
            compressed.append([listing.symbol, listing.product, listing.denomination])
        return compressed

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        compressed = {}
        for symbol, order_depth in order_depths.items():
            compressed[symbol] = [order_depth.buy_orders, order_depth.sell_orders]
        return compressed

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for trade in arr:
                compressed.append([
                    trade.symbol, trade.price, trade.quantity,
                    trade.buyer, trade.seller, trade.timestamp,
                ])
        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        # Observations are mostly relevant for later rounds (conversion-based
        # arbitrage). For the tutorial round you can basically ignore this.
        conversion_observations = {}
        try:
            for product, observation in observations.conversionObservations.items():
                conversion_observations[product] = [
                    observation.bidPrice,
                    observation.askPrice,
                    observation.transportFees,
                    observation.exportTariff,
                    observation.importTariff,
                    getattr(observation, "sugarPrice", 0),
                    getattr(observation, "sunlightIndex", 0),
                ]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for order in arr:
                compressed.append([order.symbol, order.price, order.quantity])
        return compressed

    def to_json(self, value: Any) -> str:
        # ProsperityEncoder knows how to serialize the custom datamodel classes
        # by reading their __dict__. separators=(",", ":") = no whitespace = smaller.
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
        # Binary-searches for the longest prefix of `value` that, once JSON-
        # encoded (with quotes/escapes), still fits within `max_length`.
        # Adds "..." if it had to cut anything off.
        lo, hi = 0, min(len(value), max_length)
        out = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = value[:mid]
            if len(candidate) < len(value):
                candidate += "..."
            encoded_candidate = json.dumps(candidate)
            if len(encoded_candidate) <= max_length:
                out = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return out


# Single global logger instance, used by the Trader class below.
logger = Logger()


# =============================================================================
# TRADER CLASS — this is the actual strategy
# =============================================================================
class Trader:
    """
    Market-making strategy:

      EMERALDS — assumed to have a fixed fair value of 10,000.
                 Take any ask < 10000 and any bid > 10000 (risk-free profit).
                 Then quote a passive bid/ask 1 tick inside the bot quotes.

      TOMATOES — fair value is dynamic, computed as the mid-price of the
                 current book. Same take + penny structure.
    """

    # Hardcoded fair value for the stable product. The tutorial blurb says
    # emeralds are "stable", and historically in Prosperity these stable
    # products sit at a clean round number like 10,000.
    EMERALD_FAIR = 10000

    def run(self, state: TradingState):
        # `result` will hold the orders we want to place this tick,
        # keyed by product symbol. We must return this from run().
        result: Dict[Symbol, List[Order]] = {}

        # Loop over every product the exchange is offering this tick.
        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []  # orders we'll send for THIS product

            # ---- Position bookkeeping --------------------------------------
            # limit  = max absolute position allowed (e.g. 80)
            # position = where we currently are (positive=long, negative=short)
            # buy_room  = how much MORE we're allowed to BUY before hitting +limit
            # sell_room = how much MORE we're allowed to SELL before hitting -limit
            #
            # Example: limit=80, position=30
            #   buy_room  = 80 - 30  = 50  (can buy 50 more before hitting +80)
            #   sell_room = 80 + 30  = 110 (can sell 110 before hitting -80)
            limit = POSITION_LIMITS.get(product, 50)
            position = state.position.get(product, 0)
            buy_room = limit - position
            sell_room = limit + position

            # ---- Read the order book ---------------------------------------
            # buy_orders  = bids from bots (people who want to BUY from us)
            #               keys = price, values = positive volume
            # sell_orders = asks from bots (people who want to SELL to us)
            #               keys = price, values = NEGATIVE volume (IMC quirk)
            #
            # We sort bids descending (best/highest first) and asks ascending
            # (best/lowest first) so we can iterate from "most attractive" outward.
            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            # Best bid = highest price someone wants to buy at
            # Best ask = lowest price someone wants to sell at
            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            # If either side is empty there's no real market — skip this tick.
            if best_bid is None or best_ask is None:
                continue

            # =============================================================
            # EMERALDS branch — fixed fair value of 10,000
            # =============================================================
            if product == "EMERALDS":
                fair = self.EMERALD_FAIR  # 10,000

                # ---------------------------------------------------------
                # STEP 1a: TAKE cheap asks (someone selling below fair)
                # ---------------------------------------------------------
                # Walk through asks from cheapest to most expensive. Anything
                # priced strictly below 10,000 is free money — we'd be buying
                # below true value. Asks priced AT 10,000 are only taken if
                # we're flat or short (position <= 0), as a soft inventory
                # control: don't keep loading up if we're already long.
                for ask_price, ask_vol in bot_asks:
                    if ask_price <= fair and buy_room > 0 and position <= 0:
                        # ask_vol is negative (IMC convention), so -ask_vol = positive size
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))  # +qty = BUY
                        buy_room -= take_qty
                    elif ask_price < fair and buy_room > 0:
                        # Strictly below fair: take regardless of position
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                    else:
                        # Asks are sorted ascending, so once one fails the
                        # condition, all later (higher) ones will too. Stop.
                        break

                # ---------------------------------------------------------
                # STEP 1b: TAKE expensive bids (someone buying above fair)
                # ---------------------------------------------------------
                # Mirror image: hit any bid priced above fair. Bids exactly
                # at fair are only hit if we're long (position >= 0), again
                # as inventory control.
                for bid_price, bid_vol in bot_bids:
                    if bid_price >= fair and sell_room > 0 and position >= 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))  # -qty = SELL
                        sell_room -= take_qty
                    elif bid_price > fair and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                    else:
                        break

                # ---------------------------------------------------------
                # STEP 2: PENNY — post passive quotes inside the spread
                # ---------------------------------------------------------
                # "Pennying" = quoting one tick better than the best bot quote.
                # If best bot bid is 9998, we bid 9999. If best bot ask is
                # 10002, we ask 10001. This puts us at the FRONT of the queue
                # so any incoming flow hits us first.
                #
                # (Side note from me: in past Prosperity rounds, sitting AT
                # the bot levels with a bigger spread was sometimes more
                # profitable than pennying. Worth experimenting with.)
                my_bid = best_bid + 1
                my_ask = best_ask - 1

                # Safety: never quote a bid >= fair (we'd be buying above true
                # value) or an ask <= fair (selling below true value).
                my_bid = min(my_bid, fair - 1)  # cap bid at 9999
                my_ask = max(my_ask, fair + 1)  # floor ask at 10001

                # Use ALL remaining room. If we get filled on both sides we
                # earn the full spread, risk-free. If we only get filled on
                # one side we end up with inventory, but the take logic in
                # step 1 will help unwind it next tick.
                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            # =============================================================
            # TOMATOES branch — dynamic fair value (mid-price)
            # =============================================================
            else:
                # Mid-price as fair value. Naive but reasonable starting point.
                # WEAKNESS: if the book is one-sided or has a junk level on
                # one side, the mid lies. A volume-weighted mid, or filtering
                # to only the deep "true" bot levels, would be better.
                fair = (best_bid + best_ask) / 2

                # Same take logic as emeralds — see comments above.
                for ask_price, ask_vol in bot_asks:
                    if ask_price <= fair and buy_room > 0 and position <= 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                    elif ask_price < fair and buy_room > 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                    else:
                        break

                for bid_price, bid_vol in bot_bids:
                    if bid_price >= fair and sell_room > 0 and position >= 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                    elif bid_price > fair and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                    else:
                        break

                # Penny + clamp around dynamic fair.
                # int(fair) because fair might be x.5 (mid of two integers)
                # and order prices must be integers.
                my_bid = best_bid + 1
                my_ask = best_ask - 1
                my_bid = min(my_bid, int(fair) - 1)
                my_ask = max(my_ask, int(fair) + 1)

                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            # Save this product's orders into the result dict
            result[product] = orders

        # ---- Things we return alongside orders ----------------------------
        # traderData: a string we can use to persist state across ticks
        #   (the exchange is stateless between calls — class/global vars are
        #    NOT guaranteed to survive). Empty here because this strategy
        #    doesn't need any memory: every decision is based purely on the
        #    current snapshot. If you wanted to compute e.g. a moving average
        #    of tomato prices, you'd serialize it into traderData here and
        #    deserialize it next tick.
        trader_data = ""

        # conversions: only relevant in later rounds (conversion arbitrage).
        # 0 means "no conversion request".
        conversions = 0

        # Send the visualizer log line. Wrapped in try/except so a logging
        # bug can never crash the actual strategy.
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        # The exchange expects exactly this 3-tuple back from run().
        return result, conversions, trader_data