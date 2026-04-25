"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v26
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — unchanged
    2. ASH_COATED_OSMIUM     — v25 logic with adaptive daily fair

WHY THIS VERSION EXISTS:

    The raw Round 1 price data suggests OSMIUM is not centered on exactly
    10000 every day.

    A fixed fair of 10000 appears slightly wrong on at least some days:
      - day -2 looks centered lower
      - day -1 / day 0 look centered a bit higher

    But the first few ticks are noisy and can be misleading.
    So instead of guessing the daily fair from the very start, v26:

      1. collects two-sided OSMIUM mids for an opening window
      2. uses the median of those mids as the day's estimated fair
      3. freezes that value for the rest of the day

    This is intentionally simple:
      - no dynamic fair updates all day
      - no complicated prediction model
      - just a daily center estimate

BASE:
    algorithm25

WHAT CHANGES FROM v25:
    - OSMIUM_FAIR_VALUE is no longer always 10000
    - it becomes an estimated per-day fair after the opening window

WHAT STAYS THE SAME:
    - flatten-at-fair logic
    - weak-edge filter
    - passive quote structure
    - Pepper unchanged
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
    # v26 no longer assumes the fair is always exactly 10000.
    # We still use 10000 as the fallback default until the opening window is
    # complete, then replace it with the day's estimated center.
    OSMIUM_DEFAULT_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8
    OSMIUM_FLATTEN_THRESHOLD = 40

    # Opening-window fair estimation.
    #
    # We collect the first ~20 two-sided OSMIUM mids (timestamps 0..1900 if
    # data arrives every 100), then freeze the median as the day's fair.
    OSMIUM_FAIR_ESTIMATION_TICKS = 20

    # =========================================================================
    # PEPPER — unchanged
    # =========================================================================
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    def run(self, state: TradingState):
        stored: dict[str, object] = {}
        if state.traderData:
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        osmium_last_timestamp = stored.get("osmium_last_timestamp")
        osmium_opening_mids = stored.get("osmium_opening_mids", [])
        osmium_day_fair = stored.get("osmium_day_fair")

        # Detect day reset by timestamp going backwards.
        if osmium_last_timestamp is not None and state.timestamp < int(osmium_last_timestamp):
            osmium_opening_mids = []
            osmium_day_fair = None

        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            result[OSMIUM], osmium_opening_mids, osmium_day_fair = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
                opening_mids=[float(x) for x in osmium_opening_mids],
                day_fair=int(osmium_day_fair) if osmium_day_fair is not None else None,
            )

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        trader_data = json.dumps(
            {
                "osmium_last_timestamp": state.timestamp,
                "osmium_opening_mids": osmium_opening_mids,
                "osmium_day_fair": osmium_day_fair,
            }
        )
        return result, 0, trader_data

    def trade_osmium(
        self,
        order_depth: OrderDepth,
        position: int,
        opening_mids: list[float],
        day_fair: Optional[int],
    ) -> tuple[list[Order], list[float], int]:
        bids, asks, best_bid, best_ask = self._book(order_depth)

        if best_bid is None and best_ask is None:
            return [], opening_mids, day_fair if day_fair is not None else self.OSMIUM_DEFAULT_FAIR_VALUE

        # ---------------------------------------------------------------------
        # Estimate / freeze daily fair
        # ---------------------------------------------------------------------
        #
        # We only use two-sided books for the opening sample.
        # Once enough samples are collected, freeze the median as the day's fair.
        if best_bid is not None and best_ask is not None and day_fair is None:
            if len(opening_mids) < self.OSMIUM_FAIR_ESTIMATION_TICKS:
                opening_mids.append((best_bid + best_ask) / 2.0)

            if len(opening_mids) >= self.OSMIUM_FAIR_ESTIMATION_TICKS:
                day_fair = round(median(opening_mids))

        fair_value = day_fair if day_fair is not None else self.OSMIUM_DEFAULT_FAIR_VALUE

        inventory = position
        orders: list[Order] = []
        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # =================================================================
        # Flatten at fair — identical structure to v25
        # =================================================================
        if inventory > self.OSMIUM_FLATTEN_THRESHOLD:
            for bid_price, bid_volume in bids:
                if bid_price != fair_value:
                    continue
                max_to_flatten = inventory - self.OSMIUM_FLATTEN_THRESHOLD
                sell_room = POSITION_LIMITS[OSMIUM] + inventory
                qty = min(bid_volume, max_to_flatten, sell_room)
                if qty > 0:
                    orders.append(Order(OSMIUM, bid_price, -qty))
                    inventory -= qty

        elif inventory < -self.OSMIUM_FLATTEN_THRESHOLD:
            for ask_price, ask_volume in asks:
                if ask_price != fair_value:
                    continue
                max_to_flatten = (-inventory) - self.OSMIUM_FLATTEN_THRESHOLD
                buy_room = POSITION_LIMITS[OSMIUM] - inventory
                qty = min(-ask_volume, max_to_flatten, buy_room)
                if qty > 0:
                    orders.append(Order(OSMIUM, ask_price, qty))
                    inventory += qty

        # =================================================================
        # Profitable takes
        # =================================================================
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break
            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value
            if ask_price == fair_value - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
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
            if bid_price == fair_value + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False
            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False
            if not (sell_at_fair_to_reduce or sell_above_fair):
                break
            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # =================================================================
        # Passive quotes
        # =================================================================
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        buy_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        sell_size = self._scaled_passive_size(sell_room, sell_room, spread, inventory, side="sell")

        if best_bid is not None:
            my_bid = min(best_bid + 1, fair_value - 1)
            if buy_size > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_size))

        if best_ask is not None:
            my_ask = max(best_ask - 1, fair_value + 1)
            if sell_size > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_size))

        return orders, opening_mids, fair_value

    def trade_pepper(self, order_depth: OrderDepth, position: int) -> list[Order]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None or best_ask is None:
            return []

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
