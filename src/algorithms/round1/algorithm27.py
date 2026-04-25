"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v27
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — unchanged from the proven best family
    2. ASH_COATED_OSMIUM     — v18 structure + conservative rolling-center gate

WHY THIS VERSION EXISTS:

    After going back through the raw Round 1 price data again, the strongest
    remaining non-overfit lead is NOT:

      - opening-window daily fair detection
      - trade-print direction
      - simple top-of-book imbalance alpha

    The cleaner signal is this:

      OSMIUM's center moves slowly within the day.

    A slow rolling median of recent two-sided mids gives a better picture of
    where OSMIUM is centered *right now* than a hardcoded 10000 does.

    But we learned the hard way that fully replacing the trading logic with a
    "smarter fair" can kill fills and hurt PnL.

SO THE DESIGN HERE IS DELIBERATELY CONSERVATIVE:

    Keep the proven v18 engine:
      - same one-sided handling
      - same passive market making shape
      - same Pepper logic

    Only use the rolling center for the weakest OSMIUM fades:
      - buying at 9999
      - selling at 10001

    Strong edges still trade exactly like v18.

RATIONALE:

    If the rolling center is drifting upward, a 9999 ask is more attractive.
    If the rolling center is drifting downward, a 10001 bid is more attractive.

    That gives us a way to trim the weakest bad fades without shutting down the
    high-turnover spread capture that already works.
=============================================================================
"""

from __future__ import annotations

import json
import math
from statistics import median
from typing import Optional

try:
    from datamodel import Order, OrderDepth, TradingState
except ModuleNotFoundError:
    from prosperity3bt.datamodel import Order, OrderDepth, TradingState


OSMIUM = "ASH_COATED_OSMIUM"
PEPPER = "INTARIAN_PEPPER_ROOT"

POSITION_LIMITS = {
    OSMIUM: 80,
    PEPPER: 80,
}


class Trader:
    # =========================================================================
    # OSMIUM
    # =========================================================================
    #
    # Keep the base fair at 10000 because that is still the stable anchor for
    # the whole strategy. The rolling center is only a light overlay.
    OSMIUM_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0

    # Inventory control copied from v18.
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45

    # Spread-aware passive sizing copied from v18.
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

    # Rolling-center overlay.
    #
    # We store recent two-sided mids as integers multiplied by 2:
    #   mid2 = best_bid + best_ask
    #
    # That avoids floats in traderData and keeps the payload compact.
    OSMIUM_ROLLING_WINDOW = 120

    # We only trust the rolling center once we have a reasonable sample.
    OSMIUM_MIN_CENTER_SAMPLES = 25

    # The overlay is intentionally weak:
    # only the 1-tick fades are filtered by the rolling center.
    OSMIUM_WEAK_BUY_PRICE = 9_999
    OSMIUM_WEAK_SELL_PRICE = 10_001

    # =========================================================================
    # PEPPER
    # =========================================================================
    #
    # Keep the proven "core long + small slice" behavior unchanged.
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    def run(self, state: TradingState):
        # Carry only the small rolling-center state we need for OSMIUM.
        stored: dict[str, object] = {}
        if state.traderData:
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        osmium_mid2_history = stored.get("osmium_mid2_history", [])
        osmium_last_timestamp = stored.get("osmium_last_timestamp")

        # Day reset: Prosperity reuses timestamp from 0 each day.
        if osmium_last_timestamp is not None and state.timestamp < int(osmium_last_timestamp):
            osmium_mid2_history = []

        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            result[OSMIUM], osmium_mid2_history = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
                mid2_history=[int(x) for x in osmium_mid2_history],
            )

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        trader_data = json.dumps(
            {
                "osmium_mid2_history": osmium_mid2_history,
                "osmium_last_timestamp": state.timestamp,
            }
        )
        return result, 0, trader_data

    def trade_osmium(
        self,
        order_depth: OrderDepth,
        position: int,
        mid2_history: list[int],
    ) -> tuple[list[Order], list[int]]:
        # Standard sorted book view:
        #   bids descending
        #   asks ascending
        bids, asks, best_bid, best_ask = self._book(order_depth)

        # Same as v18: skip only if the book is fully empty.
        if best_bid is None and best_ask is None:
            return [], mid2_history

        # Keep the main fair-value anchor exactly like v18.
        fair_value = self.OSMIUM_FAIR_VALUE + self._imbalance_shift(
            best_bid=best_bid if best_bid is not None else self.OSMIUM_FAIR_VALUE - 1,
            best_ask=best_ask if best_ask is not None else self.OSMIUM_FAIR_VALUE + 1,
            best_bid_volume=bids[0][1] if bids else 0,
            best_ask_volume=-asks[0][1] if asks else 0,
            alpha=self.OSMIUM_IMBALANCE_ALPHA,
        )

        # Update the rolling-center history using only two-sided books.
        #
        # We do NOT estimate a daily fair from the open anymore because that
        # turned out to be too fragile in the raw data. Instead we maintain a
        # slow intraday center all session long.
        rolling_center: Optional[float] = None
        if best_bid is not None and best_ask is not None:
            mid2_history = (mid2_history + [best_bid + best_ask])[-self.OSMIUM_ROLLING_WINDOW :]

        if len(mid2_history) >= self.OSMIUM_MIN_CENTER_SAMPLES:
            rolling_center = median(mid2_history) / 2.0

        inventory = position
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        orders: list[Order] = []

        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # ---------------------------------------------------------------------
        # Active takes
        # ---------------------------------------------------------------------
        #
        # The whole point of v27 is:
        #   keep strong trades untouched
        #   only use the rolling center to screen the weakest fades
        #
        # So:
        #   asks <= 9998     -> same as v18
        #   bids >= 10002    -> same as v18
        #
        # Only:
        #   ask == 9999
        #   bid == 10001
        #
        # are influenced by the rolling center.
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            # v18 inventory discipline stays in place.
            if ask_price == self.OSMIUM_WEAK_BUY_PRICE and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            # New rolling-center filter on the weakest buy only.
            #
            # Intuition:
            #   buying 9999 is only attractive if the local center is not below
            #   10000. If the market has sagged lower, this "cheap" ask is
            #   often not really cheap.
            #
            # Panic-short covering is still allowed because inventory relief
            # matters more than the micro-edge.
            if (
                ask_price == self.OSMIUM_WEAK_BUY_PRICE
                and rolling_center is not None
                and rolling_center < self.OSMIUM_FAIR_VALUE
                and inventory > -self.OSMIUM_PANIC_THRESHOLD
            ):
                take_below_fair = False

            if ask_price == fair_value and inventory <= 0 and not is_panic_short:
                take_at_fair_to_cover = False

            if not (take_at_fair_to_cover or take_below_fair):
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(OSMIUM, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        for bid_price, bid_volume in bids:
            if sell_room <= 0:
                break

            sell_at_fair_to_reduce = bid_price >= fair_value and inventory >= 0
            sell_above_fair = bid_price > fair_value

            if bid_price == self.OSMIUM_WEAK_SELL_PRICE and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False

            # Symmetric rolling-center filter on the weakest sell only.
            #
            # If the local center has drifted above 10000, a bid at 10001 is
            # not especially rich anymore, so we can skip that weakest sale
            # unless inventory pressure says we should keep trading.
            if (
                bid_price == self.OSMIUM_WEAK_SELL_PRICE
                and rolling_center is not None
                and rolling_center > self.OSMIUM_FAIR_VALUE
                and inventory < self.OSMIUM_PANIC_THRESHOLD
            ):
                sell_above_fair = False

            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # ---------------------------------------------------------------------
        # Passive quotes inside the live spread
        # ---------------------------------------------------------------------
        #
        # Leave the money engine alone.
        # The biggest proven edge is still getting filled on wide spreads.
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        buy_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        sell_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="sell")

        if best_bid is not None:
            my_bid = min(best_bid + 1, math.floor(fair_value) - 1)
            if buy_size > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_size))

        if best_ask is not None:
            my_ask = max(best_ask - 1, math.ceil(fair_value) + 1)
            if sell_size > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_size))

        return orders, mid2_history

    def trade_pepper(self, order_depth: OrderDepth, position: int) -> list[Order]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None or best_ask is None:
            return []

        # Pepper stays unchanged from v18 because that family has been the most
        # reliable version for this asset:
        #   large core long
        #   plus a smaller slice that gets recycled when prices are rich/cheap
        fair_value = (best_bid + best_ask) / 2.0
        fair_value += self._imbalance_shift(
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_volume=bids[0][1],
            best_ask_volume=-asks[0][1],
            alpha=self.PEPPER_IMBALANCE_ALPHA,
        )
        fair_value -= (position - self.PEPPER_LONG_TARGET) * self.PEPPER_RES_K

        fair_floor = math.floor(fair_value)
        fair_ceil = math.ceil(fair_value)
        inventory = position
        buy_room, sell_room = self._rooms(PEPPER, inventory)
        orders: list[Order] = []

        for ask_price, ask_volume in asks:
            if buy_room <= 0 or ask_price >= fair_value:
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(PEPPER, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        sell_threshold = fair_value
        if inventory < self.PEPPER_HOLD_FLOOR:
            sell_threshold += self.PEPPER_HOLD_EXTRA_EDGE

        for bid_price, bid_volume in bids:
            if sell_room <= 0 or bid_price <= sell_threshold:
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(PEPPER, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

            sell_threshold = fair_value
            if inventory < self.PEPPER_HOLD_FLOOR:
                sell_threshold += self.PEPPER_HOLD_EXTRA_EDGE

        my_bid = min(best_bid + 1, fair_floor - self.PEPPER_MIN_EDGE)
        my_ask = max(best_ask - 1, fair_ceil + self.PEPPER_MIN_EDGE)

        if buy_room > 0 and my_bid > 0:
            orders.append(Order(PEPPER, my_bid, buy_room))

        if sell_room > 0 and my_ask > 0 and inventory >= self.PEPPER_HOLD_FLOOR:
            orders.append(Order(PEPPER, my_ask, -sell_room))

        return orders

    def _scaled_passive_size(
        self,
        buy_room: int,
        sell_room: int,
        spread: Optional[int],
        inventory: int,
        side: str,
    ) -> int:
        room = buy_room if side == "buy" else sell_room
        if room <= 0:
            return 0

        if spread is None:
            size = max(1, room // 2)
        elif spread >= self.OSMIUM_WIDE_SPREAD:
            size = room
        elif spread >= self.OSMIUM_MEDIUM_SPREAD:
            size = max(1, math.ceil(room * 0.65))
        elif spread >= self.OSMIUM_TIGHT_SPREAD:
            size = max(1, math.ceil(room * 0.35))
        else:
            size = max(1, math.ceil(room * 0.20))

        if side == "buy" and inventory > 40:
            size = max(1, math.ceil(size * 0.5))
        if side == "sell" and inventory < -40:
            size = max(1, math.ceil(size * 0.5))

        return min(size, room)

    def _rooms(self, product: str, position: int) -> tuple[int, int]:
        limit = POSITION_LIMITS[product]
        return limit - position, limit + position

    def _book(
        self, order_depth: OrderDepth
    ) -> tuple[list[tuple[int, int]], list[tuple[int, int]], Optional[int], Optional[int]]:
        bids = sorted(order_depth.buy_orders.items(), reverse=True)
        asks = sorted(order_depth.sell_orders.items())
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        return bids, asks, best_bid, best_ask

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
