"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — price follows a deterministic LINEAR RAMP
    2. ASH_COATED_OSMIUM     — price mean-reverts around a FIXED value (~10,000)

High-level strategy:
    For both products we run a market-making algorithm that:
      (a) Computes a fair value each tick
      (b) TAKES any mispriced resting orders (aggressive fills)
      (c) POSTS passive bid/ask quotes around fair to earn the spread

The key insight from data analysis:
    - Both assets exhibit very strong mean-reversion (autocorrelation = -0.50)
    - INTARIAN_PEPPER_ROOT drifts upward at exactly +0.001 per timestamp unit
      so its fair value is perfectly predictable
    - ASH_COATED_OSMIUM has zero drift — fair value is simply ~10,000
    - Spreads are wide (13 for pepper, 16 for osmium) giving plenty of room
      to market-make profitably

Position limits: 80 for both products.
Profit target: 200,000 XIRECs before day 3.
=============================================================================
"""

import json
from typing import Any, List, Dict

# IMC-provided datamodel classes:
#   Order(symbol, price, quantity)  — +qty = buy, -qty = sell
#   OrderDepth                     — .buy_orders (bids) and .sell_orders (asks)
#   TradingState                   — full market snapshot each tick
from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# =============================================================================
# POSITION LIMITS
# =============================================================================
# The exchange rejects ALL orders for a product if the net position would
# exceed these limits. We must track remaining buy/sell room carefully.
# From the round 1 rules: both products are capped at 80.
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS  (standard IMC Prosperity visualizer helper)
# =============================================================================
# This is NOT part of the trading logic. It serializes state + orders into
# compact JSON on stdout so the jmerle/imc-prosperity-visualizer GUI can
# replay your run. You can safely ignore this entire class — just know that
# logger.flush() at the end of run() makes the GUI work.
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750  # exchange truncates above ~3750 chars/tick

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        # Buffered print — collects into self.logs instead of printing immediately
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]],
              conversions: int, trader_data: str) -> None:
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
# TRADER CLASS — the actual strategy
# =============================================================================
class Trader:
    """
    Round 1 market-making strategy for two products:

    INTARIAN_PEPPER_ROOT (the "drifter"):
        - Described as "steady" — like EMERALDS from the tutorial
        - Fair value increases linearly at +0.001 per timestamp unit
        - That means +1 every 1,000 timestamps, +1,000 over a full day
        - Day -2 starts ~9998, day -1 starts ~11000, day 0 starts ~12000
        - The hint says it's "hardy, slow-growing" — the slow linear drift!
        - We track the fair value using the first mid-price we see each day
          then add the linear drift from there
        - Spread is ~13, std around fair is only ~2 → very profitable to
          market-make

    ASH_COATED_OSMIUM (the "mean-reverter"):
        - Described as "more volatile" with a "hidden pattern"
        - The hidden pattern: it mean-reverts tightly around ~10,000
        - Price oscillates in a tight band (std ~5) and snaps back quickly
        - Return autocorrelation = -0.50 (moves tend to reverse next tick)
        - Spread is ~16 → even more room for market-making
        - We use a simple EMA of mid-prices as fair value, which stays
          near 10,000 but adapts to any small regime shifts

    Both products reward market-making: take mispriced orders aggressively,
    then post passive quotes to earn the spread.
    """

    # -------------------------------------------------------------------------
    # CONFIGURATION CONSTANTS
    # -------------------------------------------------------------------------

    # ASH_COATED_OSMIUM: fixed fair value anchor.
    # From data analysis: mean price across all 3 days is ~10,000.
    OSMIUM_FAIR_ANCHOR = 10000

    # EMA smoothing factor for ASH_COATED_OSMIUM fair value estimation.
    # alpha=0.3 means we weight 30% on the new observation, 70% on our
    # running estimate. Responsive enough to track small shifts but smooth
    # enough to not chase noise.
    OSMIUM_EMA_ALPHA = 0.3

    # INTARIAN_PEPPER_ROOT: the exact slope of the linear price ramp.
    # From data analysis: slope = +0.001 per timestamp unit, confirmed
    # identical across all 3 days. This is the most important parameter —
    # if wrong, our fair value drifts away from reality.
    PEPPER_SLOPE = 0.001

    # How many ticks wide to quote around fair value for each product.
    # These are "half-spreads" — bid at (fair - offset), ask at (fair + offset).
    #
    # Why 2?
    #   PEPPER: natural bot spread is 13 (half ~6.5). Quoting at 2 puts us
    #     well inside the bot spread for queue priority. Since std around
    #     fair is only ~2, adverse selection risk is minimal.
    #   OSMIUM: natural bot spread is 16 (half ~8). Even more room to sit
    #     inside the bots. Strong mean-reversion protects us.
    PEPPER_QUOTE_OFFSET = 2
    OSMIUM_QUOTE_OFFSET = 2

    # Inventory skew parameters.
    # When we accumulate a position, we lean quotes to encourage fills
    # that reduce inventory. The skew formula:
    #   bid_offset  = base_offset + skew_factor * (position / limit)
    #   ask_offset  = base_offset - skew_factor * (position / limit)
    #
    # So if we're long (position > 0):
    #   - bid_offset increases → bid price drops → less eager to buy more
    #   - ask_offset decreases → ask price drops → more eager to sell
    # This pushes us back toward flat inventory.
    PEPPER_SKEW_FACTOR = 3
    OSMIUM_SKEW_FACTOR = 3

    def run(self, state: TradingState):
        """
        Called by the exchange once per tick with the full market snapshot.

        Returns:
            result      — dict of {product: [Order, ...]} with our orders
            conversions — int, always 0 for round 1 (no conversion mechanic)
            trader_data — str, JSON blob to persist state across ticks
        """

        # =====================================================================
        # STEP 0: RESTORE PERSISTED STATE FROM PREVIOUS TICK
        # =====================================================================
        # The exchange is stateless between ticks — class variables and globals
        # are NOT guaranteed to survive. The only way to carry state forward is
        # via `state.traderData`, a string we returned last tick.
        #
        # We persist:
        #   pepper_day_start_price — mid-price at timestamp=0 of current day
        #   pepper_last_timestamp  — last seen timestamp (to detect day changes)
        #   osmium_ema             — running EMA estimate of osmium fair value
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        # Retrieve persisted values (or None if first tick)
        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)
        osmium_ema = stored.get("osmium_ema", None)

        # =====================================================================
        # STEP 1: BUILD ORDERS FOR EACH PRODUCT
        # =====================================================================
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []

            # -----------------------------------------------------------------
            # Position bookkeeping
            # -----------------------------------------------------------------
            # limit    = max absolute position (80 for both products)
            # position = current net position (+ve = long, -ve = short)
            # buy_room = how many more units we can BUY before hitting +limit
            # sell_room = how many more units we can SELL before hitting -limit
            #
            # Example: limit=80, position=30
            #   buy_room  = 80 - 30 = 50  (can buy 50 more)
            #   sell_room = 80 + 30 = 110 (can sell 110 more)
            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position
            sell_room = limit + position

            # -----------------------------------------------------------------
            # Read the order book
            # -----------------------------------------------------------------
            # buy_orders (bids) = other participants wanting to BUY from us
            #   keys = price, values = positive volume
            # sell_orders (asks) = other participants wanting to SELL to us
            #   keys = price, values = NEGATIVE volume (IMC convention!)
            #
            # Sort bids descending (best/highest first) and asks ascending
            # (best/cheapest first) so we process the most attractive first.
            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            # If either side of the book is empty, we can't compute a fair
            # value or safely trade. Skip this product for this tick.
            if best_bid is None or best_ask is None:
                result[product] = orders
                continue

            # Current mid-price (average of best bid and best ask)
            mid_price = (best_bid + best_ask) / 2

            # =================================================================
            # INTARIAN_PEPPER_ROOT — linear ramp fair value
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                # -------------------------------------------------------------
                # Fair value computation
                # -------------------------------------------------------------
                # The price follows a perfect linear ramp:
                #   fair(t) = day_start_price + PEPPER_SLOPE * timestamp
                #
                # where PEPPER_SLOPE = 0.001, meaning:
                #   +1 every 1,000 timestamp units
                #   +100 every 100,000 timestamp units
                #   +1,000 over a full day (timestamps 0 to 999,900)
                #
                # The game hint says pepper root is "hardy, slow-growing" —
                # this is literally the slow linear price growth!
                #
                # To use this formula we need "day_start_price" — the fair
                # value at timestamp=0 of the current day. We detect a new day
                # when the timestamp is smaller than the last one we saw
                # (it resets to 0 each day).

                is_new_day = False
                if pepper_day_start_price is None:
                    # Very first tick ever — no stored state
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    # Timestamp went backwards → new day started (reset to 0)
                    is_new_day = True

                if is_new_day:
                    # On a new day, snapshot the current mid-price as our anchor.
                    # The ramp is continuous across days, so this picks up
                    # where yesterday left off.
                    pepper_day_start_price = mid_price

                # Update last-seen timestamp for next tick's day detection
                pepper_last_timestamp = state.timestamp

                # The actual fair value right now:
                # start from the day's opening price and add the linear drift
                fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp

                # Round to nearest int since order prices must be integers
                fair_int = round(fair)

                # -------------------------------------------------------------
                # Inventory-aware quote offsets
                # -------------------------------------------------------------
                # position_ratio ranges from -1.0 (max short) to +1.0 (max long)
                # When long → widen bid (less eager to buy) + tighten ask (sell faster)
                # When short → tighten bid (buy faster) + widen ask (less eager to sell)
                position_ratio = position / limit if limit > 0 else 0

                bid_offset = self.PEPPER_QUOTE_OFFSET + self.PEPPER_SKEW_FACTOR * position_ratio
                ask_offset = self.PEPPER_QUOTE_OFFSET - self.PEPPER_SKEW_FACTOR * position_ratio

                # Ensure offsets stay positive (minimum 1 tick from fair)
                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # -------------------------------------------------------------
                # AGGRESSIVE PHASE: take mispriced resting orders
                # -------------------------------------------------------------
                # Walk through asks (people selling to us). Anyone selling
                # below our fair value is offering us a bargain — we buy.
                #
                # Why include == fair_int when position <= 0?
                #   At exactly fair, the expected profit is ~0, BUT the strong
                #   mean-reversion means price will likely bounce above fair
                #   next tick. We only do this when short/flat as inventory
                #   control — don't pile on if already long.
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and buy_room > 0:
                        # ask_vol is negative (IMC convention), negate it
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                        position += take_qty  # track for skew calc
                    elif ask_price == fair_int and buy_room > 0 and position <= 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                        position += take_qty
                    else:
                        # Asks sorted ascending — once above fair, stop
                        break

                # Mirror: hit bids above fair (people overpaying to buy from us)
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and sell_room > 0 and position >= 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # -------------------------------------------------------------
                # PASSIVE PHASE: post quotes around fair value
                # -------------------------------------------------------------
                # Place resting bid below fair and ask above fair.
                # Offsets adjusted by inventory skew (computed above).
                #
                # Why this works: the natural bot spread is ~13 ticks, but
                # our fair value is very accurate. By quoting ~2 ticks from
                # fair (adjusted by skew), we sit well inside the bot spread
                # and get priority on incoming flow. The strong mean-reversion
                # (autocorr = -0.50) means fills revert to profit quickly.
                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

                # Safety: never bid above fair or ask below fair
                my_bid = min(my_bid, fair_int - 1)
                my_ask = max(my_ask, fair_int + 1)

                # Post remaining capacity as passive quotes
                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            # =================================================================
            # ASH_COATED_OSMIUM — stationary mean-reversion around ~10,000
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # -------------------------------------------------------------
                # Fair value computation via EMA
                # -------------------------------------------------------------
                # The true fair value is ~10,000 and barely moves. We use an
                # Exponential Moving Average of observed mid-prices to track it.
                #
                # EMA update rule:
                #   ema_new = alpha * observation + (1 - alpha) * ema_old
                #
                # With alpha=0.3, the EMA responds fairly quickly but stays
                # smooth. Since the true value barely moves, it hovers around
                # 10,000 naturally.
                #
                # The game hints say osmium is "more volatile" with a "hidden
                # pattern" — the hidden pattern IS the mean-reversion. Price
                # looks random but always snaps back to ~10,000.
                #
                # Why EMA instead of hardcoding 10,000?
                #   - More robust: if fair shifts slightly between days, EMA adapts
                #   - Still anchored: extreme outliers can't yank it far
                #   - The anchor (10000) is used as the initial seed

                if osmium_ema is None:
                    # First tick: seed EMA with our anchor value.
                    # We use the anchor (not mid-price) because the first
                    # tick's mid might be slightly noisy, and we KNOW the
                    # true center is ~10,000.
                    osmium_ema = self.OSMIUM_FAIR_ANCHOR

                # Update EMA with this tick's mid-price
                osmium_ema = (self.OSMIUM_EMA_ALPHA * mid_price
                              + (1 - self.OSMIUM_EMA_ALPHA) * osmium_ema)

                fair = osmium_ema
                fair_int = round(fair)

                # -------------------------------------------------------------
                # Inventory-aware quote offsets (same logic as pepper)
                # -------------------------------------------------------------
                position_ratio = position / limit if limit > 0 else 0

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # -------------------------------------------------------------
                # AGGRESSIVE PHASE: take mispriced resting orders
                # -------------------------------------------------------------
                # Same logic: sweep asks below fair (cheap buys) and bids
                # above fair (expensive sells).
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and buy_room > 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and buy_room > 0 and position <= 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and sell_room > 0 and position >= 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # -------------------------------------------------------------
                # PASSIVE PHASE: post quotes around fair value
                # -------------------------------------------------------------
                # Osmium has a wider natural spread (16 vs 13) → even more
                # room to sit inside the bot quotes and earn the spread.
                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

                # Safety: never bid above fair or ask below fair
                my_bid = min(my_bid, fair_int - 1)
                my_ask = max(my_ask, fair_int + 1)

                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            # =================================================================
            # UNKNOWN PRODUCT — fallback (shouldn't happen in round 1)
            # =================================================================
            else:
                # If a surprise product shows up, do basic mid-price
                # market-making as a safe fallback.
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
        # STEP 2: PERSIST STATE FOR NEXT TICK
        # =====================================================================
        # Serialize everything we need into JSON. The exchange hands this
        # back as state.traderData next tick.
        trader_data = json.dumps({
            # Pepper: day anchor price and last timestamp for day detection
            "pepper_day_start_price": pepper_day_start_price,
            "pepper_last_timestamp": pepper_last_timestamp,
            # Osmium: EMA for smooth fair value tracking
            "osmium_ema": osmium_ema,
        })

        # No conversions in round 1
        conversions = 0

        # Flush visualizer logs (try/except so logging bugs never crash strategy)
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        # The exchange expects this exact 3-tuple
        return result, conversions, trader_data
