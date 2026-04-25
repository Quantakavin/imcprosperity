"""
=============================================================================
PROSPERITY 4 — ROUND 2 TRADER 3
=============================================================================
Goals relative to algorithm2:
    1. Keep the PEPPER core-long structure that generated most of the profit.
    2. Improve OSMIUM by replacing the static fair with a slow adaptive fair.
    3. Skew OSMIUM quoting harder against stretched inventory so we do not
       finish heavily short as often.

Round 2 bid:
    Keep the same moderate Market Access Fee bid while improving the trading
    logic itself.
=============================================================================
"""

from __future__ import annotations

import json
import math
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
    OSMIUM_BASE_FAIR = 10_000.0
    OSMIUM_ALPHA = 0.18
    OSMIUM_IMBALANCE_ALPHA = 0.20
    OSMIUM_RECENTER_WEIGHT = 0.08

    OSMIUM_PANIC_THRESHOLD = 26
    OSMIUM_WEAK_EDGE_GUARD = 40
    OSMIUM_SHORT_SKEW_START = -25
    OSMIUM_LONG_SKEW_START = 25

    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.24
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 76
    PEPPER_HOLD_EXTRA_EDGE = 3
    PEPPER_RELOAD_EDGE = 1

    def bid(self) -> int:
        return 15

    def run(self, state: TradingState):
        trader_state = self._decode_state(state.traderData)
        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            osmium_orders, trader_state = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
                trader_state=trader_state,
            )
            result[OSMIUM] = osmium_orders

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        return result, 0, self._encode_state(trader_state)

    def trade_osmium(self, order_depth: OrderDepth, position: int, trader_state: dict) -> tuple[list[Order], dict]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None and best_ask is None:
            return [], trader_state

        fair_value, trader_state = self._osmium_fair(best_bid, best_ask, bids, asks, trader_state)

        inventory = position
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        orders: list[Order] = []

        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            if ask_price >= fair_value - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            if ask_price >= fair_value and inventory <= 0 and not is_panic_short:
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

            if bid_price <= fair_value + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False

            if bid_price <= fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        buy_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        sell_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="sell")

        inventory_shift = self._inventory_price_shift(inventory)

        if best_bid is not None:
            my_bid = min(best_bid + 1, math.floor(fair_value - inventory_shift) - 1)
            if buy_size > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_size))

        if best_ask is not None:
            my_ask = max(best_ask - 1, math.ceil(fair_value - inventory_shift) + 1)
            if sell_size > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_size))

        return orders, trader_state

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

        reload_threshold = fair_value + self.PEPPER_RELOAD_EDGE if inventory < self.PEPPER_HOLD_FLOOR else fair_value
        for ask_price, ask_volume in asks:
            if buy_room <= 0 or ask_price > reload_threshold:
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(PEPPER, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

            reload_threshold = fair_value + self.PEPPER_RELOAD_EDGE if inventory < self.PEPPER_HOLD_FLOOR else fair_value

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

    def _osmium_fair(
        self,
        best_bid: Optional[int],
        best_ask: Optional[int],
        bids: list[tuple[int, int]],
        asks: list[tuple[int, int]],
        trader_state: dict,
    ) -> tuple[float, dict]:
        prev = trader_state.get("osmium_fair", self.OSMIUM_BASE_FAIR)

        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            mid = best_bid + 1.0
        else:
            mid = best_ask - 1.0

        imbalance_shift = self._imbalance_shift(
            best_bid=best_bid if best_bid is not None else int(prev - 1),
            best_ask=best_ask if best_ask is not None else int(prev + 1),
            best_bid_volume=bids[0][1] if bids else 0,
            best_ask_volume=-asks[0][1] if asks else 0,
            alpha=self.OSMIUM_IMBALANCE_ALPHA,
        )

        target_fair = (1.0 - self.OSMIUM_RECENTER_WEIGHT) * mid + self.OSMIUM_RECENTER_WEIGHT * self.OSMIUM_BASE_FAIR
        fair = (1.0 - self.OSMIUM_ALPHA) * prev + self.OSMIUM_ALPHA * target_fair + imbalance_shift

        trader_state["osmium_fair"] = fair
        return fair, trader_state

    def _inventory_price_shift(self, inventory: int) -> float:
        if inventory <= self.OSMIUM_SHORT_SKEW_START:
            return -2.0
        if inventory >= self.OSMIUM_LONG_SKEW_START:
            return 2.0
        return inventory / 20.0

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
            size = max(1, room // 3)
        elif spread >= self.OSMIUM_WIDE_SPREAD:
            size = max(1, math.ceil(room * 0.8))
        elif spread >= self.OSMIUM_MEDIUM_SPREAD:
            size = max(1, math.ceil(room * 0.55))
        elif spread >= self.OSMIUM_TIGHT_SPREAD:
            size = max(1, math.ceil(room * 0.25))
        else:
            size = max(1, math.ceil(room * 0.15))

        if side == "buy":
            if inventory > 40:
                size = max(1, math.ceil(size * 0.35))
            elif inventory < -40:
                size = max(1, math.ceil(size * 1.35))
        else:
            if inventory < -40:
                size = max(1, math.ceil(size * 0.25))
            elif inventory > 40:
                size = max(1, math.ceil(size * 1.25))

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

    def _decode_state(self, trader_data: str) -> dict:
        if not trader_data:
            return {}
        try:
            return json.loads(trader_data)
        except json.JSONDecodeError:
            return {}

    def _encode_state(self, trader_state: dict) -> str:
        return json.dumps(trader_state, separators=(",", ":"))
