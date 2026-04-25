"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v17
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + small cycle around target
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

WHY THIS VERSION EXISTS:

    After reviewing our own best bots (v11 / v14 / v16) against the 10k+
    uploaded bot, the big conclusion was:

        The missing edge is mostly NOT a hidden prediction signal.
        The missing edge is mostly OSMIUM execution quality.

    Our older bots made money in OSMIUM by:
      - taking obvious mispricings around fair = 10000
      - posting passive quotes around fair with inventory skew

    But they usually quoted in "fair space" (for example 9997 / 10003),
    which leaves money on the table when the live spread is wide.

    The uploaded 10k+ bot did something simpler and better:
      - still use fair = 10000
      - but quote INSIDE the CURRENT spread:
            bid at best_bid + 1
            ask at best_ask - 1
        while still respecting fair

    That small change matters a lot:
      - better queue position
      - better average buy price
      - better average sell price
      - much more spread capture

WHAT v17 DOES:

    OSMIUM:
      1. Keep fair fixed at 10000
      2. Take asks below fair and bids above fair
      3. Also trade exactly at fair when reducing inventory
      4. Quote inside the live spread, not just around static offsets
      5. Keep our one-sided-book handling from v11
      6. Add one tiny safety filter:
           - the weakest buy fade (9999) is only taken freely when we are
             not already very long

    PEPPER:
      Use the uploaded bot's stronger version:
      - maintain a large long target near +80
      - allow a small slice to cycle around that core
      - avoid selling too much by using a hold floor

GOAL:
    Take the strongest idea we found in practice (uploaded Osmium execution)
    and combine it with the one robustness improvement our code clearly had
    (handling one-sided books).
=============================================================================
"""

import json
import math
from typing import Any, Dict, List

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
    # PEPPER — large core long + small cycle
    # =========================================================================
    #
    # This is the strongest Pepper pattern we saw in realized PnL:
    #     hold a big long position most of the time
    #     but allow a small slice to trade around that long
    #
    # Intuition:
    #   PEPPER has a very strong upward drift, so the main edge is to get long.
    #   But once we are near +80, we can occasionally sell a few units at
    #   unusually rich prices and buy them back later, as long as we do not
    #   accidentally lose the big core long.
    #
    # The controls below create that behavior.
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    # =========================================================================
    # OSMIUM — aggressive live-spread MM around fair = 10000
    # =========================================================================
    OSMIUM_FAIR = 10000

    # We keep the exchange limit available for both active takes and passive
    # quoting. This follows the stronger uploaded bot. The idea is that the
    # strategy makes money by constantly warehousing inventory temporarily
    # while harvesting spread.
    OSMIUM_FULL_LIMIT = 80

    # Inventory comfort bands:
    #   - PANIC threshold: if inventory is large, trade at fair to reduce it.
    #   - SOFT_LONG threshold: tiny guard used only on the weakest buy fade
    #     (buying at 9999). This is the one light filter inspired by v14/v16.
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_SOFT_LONG_THRESHOLD = 50

    # =========================================================================
    # RUN
    # =========================================================================
    def run(self, state: TradingState):
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            if product == "ASH_COATED_OSMIUM":
                result[product] = self.trade_osmium(
                    order_depth=state.order_depths[product],
                    position=state.position.get(product, 0),
                )
            elif product == "INTARIAN_PEPPER_ROOT":
                result[product] = self.trade_pepper(
                    order_depth=state.order_depths[product],
                    position=state.position.get(product, 0),
                )
            else:
                result[product] = self.trade_unknown(
                    product=product,
                    order_depth=state.order_depths[product],
                    position=state.position.get(product, 0),
                )

        trader_data = json.dumps({})
        conversions = 0
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data

    # =========================================================================
    # OSMIUM
    # =========================================================================
    def trade_osmium(self, order_depth: OrderDepth, position: int) -> List[Order]:
        bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
        bot_asks = sorted(order_depth.sell_orders.items())

        best_bid = bot_bids[0][0] if bot_bids else None
        best_ask = bot_asks[0][0] if bot_asks else None

        # v17 keeps our v11 robustness:
        # if one side of the book disappears, we do NOT give up the whole tick.
        # We still:
        #   - take obvious value on the visible side
        #   - post on the side where we still can
        if best_bid is None and best_ask is None:
            return []

        fair_value = self.OSMIUM_FAIR
        inventory = position
        orders: List[Order] = []

        buy_room = self.OSMIUM_FULL_LIMIT - inventory
        sell_room = self.OSMIUM_FULL_LIMIT + inventory

        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # ---------------------------------------------------------------------
        # Layer 1: TAKE clear value already sitting in the book
        # ---------------------------------------------------------------------
        #
        # We buy:
        #   - any ask below 10000
        #   - any ask at 10000 if we are short and want to reduce that short
        #
        # We sell:
        #   - any bid above 10000
        #   - any bid at 10000 if we are long and want to reduce that long
        #
        # Small extra guard:
        #   the weakest buy fade is 9999 (only 1 point below fair).
        #   We still want it most of the time, but not when we are already
        #   heavily long. That is the only deliberate "quality filter" here.
        if best_ask is not None:
            for ask_price, ask_vol in bot_asks:
                if buy_room <= 0:
                    break

                should_take = False

                if ask_price < fair_value - 1:
                    should_take = True
                elif ask_price == fair_value - 1 and inventory < self.OSMIUM_SOFT_LONG_THRESHOLD:
                    should_take = True
                elif ask_price == fair_value and inventory <= 0 and is_panic_short:
                    should_take = True

                if not should_take:
                    break

                take_qty = min(-ask_vol, buy_room)
                orders.append(Order("ASH_COATED_OSMIUM", ask_price, take_qty))
                inventory += take_qty
                buy_room -= take_qty

        if best_bid is not None:
            for bid_price, bid_vol in bot_bids:
                if sell_room <= 0:
                    break

                should_take = False

                if bid_price > fair_value:
                    should_take = True
                elif bid_price == fair_value and inventory >= 0 and is_panic_long:
                    should_take = True

                if not should_take:
                    break

                take_qty = min(bid_vol, sell_room)
                orders.append(Order("ASH_COATED_OSMIUM", bid_price, -take_qty))
                inventory -= take_qty
                sell_room -= take_qty

        # ---------------------------------------------------------------------
        # Layer 2: PASSIVE market making inside the CURRENT spread
        # ---------------------------------------------------------------------
        #
        # This is the key upgrade over our older bots.
        #
        # Old style:
        #   quote around fixed fair offsets like 9997 / 10003
        #
        # New style:
        #   use the live market:
        #       best_bid + 1
        #       best_ask - 1
        #
        # But we still do not cross fair:
        #   bid must stay <= 9999
        #   ask must stay >= 10001
        #
        # This usually gives us:
        #   - earlier fills
        #   - better queue position
        #   - much better average execution
        #
        # Inventory skew:
        #   if long -> quote less aggressively on the bid, more aggressively
        #              on the ask
        #   if short -> the opposite
        #
        # We keep the skew gentle because the uploaded bot's big win came from
        # quoting profitably, not from fancy inventory math.
        buy_room = self.OSMIUM_FULL_LIMIT - inventory
        sell_room = self.OSMIUM_FULL_LIMIT + inventory

        if best_bid is not None:
            base_bid = min(best_bid + 1, fair_value - 1)

            # If already long, back the bid off by 1-2 ticks.
            if inventory >= 60:
                base_bid -= 2
            elif inventory >= 30:
                base_bid -= 1

            if buy_room > 0 and base_bid > 0:
                orders.append(Order("ASH_COATED_OSMIUM", base_bid, buy_room))

        if best_ask is not None:
            base_ask = max(best_ask - 1, fair_value + 1)

            # If already short, lift the ask by 1-2 ticks.
            if inventory <= -60:
                base_ask += 2
            elif inventory <= -30:
                base_ask += 1

            if sell_room > 0 and base_ask > 0:
                orders.append(Order("ASH_COATED_OSMIUM", base_ask, -sell_room))

        return orders

    # =========================================================================
    # PEPPER
    # =========================================================================
    def trade_pepper(self, order_depth: OrderDepth, position: int) -> List[Order]:
        bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
        bot_asks = sorted(order_depth.sell_orders.items())

        best_bid = bot_bids[0][0] if bot_bids else None
        best_ask = bot_asks[0][0] if bot_asks else None

        if best_bid is None or best_ask is None:
            return []

        # ---------------------------------------------------------------------
        # PEPPER fair value
        # ---------------------------------------------------------------------
        #
        # We use:
        #   1. mid-price
        #   2. top-of-book imbalance adjustment
        #   3. inventory target adjustment
        #
        # Why not use the explicit deterministic drift formula here?
        # Because the uploaded bot already proved that a simpler "trade around
        # the current book while preserving a large long target" works well.
        fair_value = (best_bid + best_ask) / 2.0
        fair_value += self._imbalance_shift(
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_volume=bot_bids[0][1],
            best_ask_volume=-bot_asks[0][1],
            alpha=self.PEPPER_IMBALANCE_ALPHA,
        )

        # Inventory target term:
        #   if below target, fair is shifted UP so we buy more eagerly
        #   if above target, fair is shifted DOWN so we sell more readily
        fair_value -= (position - self.PEPPER_LONG_TARGET) * self.PEPPER_RES_K

        fair_floor = math.floor(fair_value)
        fair_ceil = math.ceil(fair_value)

        inventory = position
        buy_room = POSITION_LIMITS["INTARIAN_PEPPER_ROOT"] - inventory
        sell_room = POSITION_LIMITS["INTARIAN_PEPPER_ROOT"] + inventory
        orders: List[Order] = []

        # ---------------------------------------------------------------------
        # Buy cheap asks
        # ---------------------------------------------------------------------
        for ask_price, ask_vol in bot_asks:
            if buy_room <= 0 or ask_price >= fair_value:
                break

            take_qty = min(-ask_vol, buy_room)
            orders.append(Order("INTARIAN_PEPPER_ROOT", ask_price, take_qty))
            inventory += take_qty
            buy_room -= take_qty

        # ---------------------------------------------------------------------
        # Sell rich bids, but protect the core long
        # ---------------------------------------------------------------------
        #
        # If we are below the hold floor, require EXTRA edge before selling.
        # This stops us from carelessly bleeding out the long PEPPER position,
        # which is where most of the asset's edge comes from.
        sell_threshold = fair_value
        if inventory < self.PEPPER_HOLD_FLOOR:
            sell_threshold += self.PEPPER_HOLD_EXTRA_EDGE

        for bid_price, bid_vol in bot_bids:
            if sell_room <= 0 or bid_price <= sell_threshold:
                break

            take_qty = min(bid_vol, sell_room)
            orders.append(Order("INTARIAN_PEPPER_ROOT", bid_price, -take_qty))
            inventory -= take_qty
            sell_room -= take_qty

            sell_threshold = fair_value
            if inventory < self.PEPPER_HOLD_FLOOR:
                sell_threshold += self.PEPPER_HOLD_EXTRA_EDGE

        # ---------------------------------------------------------------------
        # Passive quotes
        # ---------------------------------------------------------------------
        #
        # Bid:
        #   post a buy order, but only where we still keep a minimum edge
        #
        # Ask:
        #   only post passive sells if we already hold enough inventory
        #   to preserve the "core + slice" structure
        my_bid = min(best_bid + 1, fair_floor - self.PEPPER_MIN_EDGE)
        my_ask = max(best_ask - 1, fair_ceil + self.PEPPER_MIN_EDGE)

        if buy_room > 0 and my_bid > 0:
            orders.append(Order("INTARIAN_PEPPER_ROOT", my_bid, buy_room))

        if sell_room > 0 and my_ask > 0 and inventory >= self.PEPPER_HOLD_FLOOR:
            orders.append(Order("INTARIAN_PEPPER_ROOT", my_ask, -sell_room))

        return orders

    # =========================================================================
    # UNKNOWN PRODUCT — safe fallback
    # =========================================================================
    def trade_unknown(self, product: str, order_depth: OrderDepth, position: int) -> List[Order]:
        bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
        bot_asks = sorted(order_depth.sell_orders.items())

        best_bid = bot_bids[0][0] if bot_bids else None
        best_ask = bot_asks[0][0] if bot_asks else None

        if best_bid is None or best_ask is None:
            return []

        limit = POSITION_LIMITS.get(product, 80)
        buy_room = limit - position
        sell_room = limit + position
        fair_int = round((best_bid + best_ask) / 2.0)
        orders: List[Order] = []

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

        return orders

    def _imbalance_shift(
        self,
        best_bid: int,
        best_ask: int,
        best_bid_volume: int,
        best_ask_volume: int,
        alpha: float,
    ) -> float:
        total_volume = best_bid_volume + best_ask_volume
        if total_volume <= 0:
            return 0.0

        spread = best_ask - best_bid
        imbalance = (best_bid_volume - best_ask_volume) / total_volume
        return alpha * (spread / 2.0) * imbalance
