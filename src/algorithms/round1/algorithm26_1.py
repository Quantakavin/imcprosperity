"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v26.1
=============================================================================
Strategy fork:
    Refined multi-level OSMIUM quoting

BASE:
    algorithm18

WHY THIS VERSION EXISTS:

    The best run's OSMIUM fills cluster heavily at a few nearby price levels
    (for example repeated sells at 10003 / 10008 and repeated buys around
    9990 / 9994 / 9995). That suggests the market can move through more than
    one attractive passive level during the same wave.

    The earlier multi-level attempt used a fairly aggressive 60/40 split.
    This version is more conservative:

      - keep most size at the primary best+1 / best-1 quote
      - place only a small secondary quote deeper in the book
      - only activate the secondary quote when the spread is truly wide

WHAT CHANGES FROM algorithm18:

    OSMIUM passive quotes:
      - primary level gets 80% of passive size
      - secondary level gets 20% of passive size
      - secondary quote is 2 ticks deeper
      - secondary quote is posted only when spread >= 16

WHAT STAYS THE SAME:
    - OSMIUM fair = 10000
    - one-sided handling
    - weak-edge filter
    - Pepper unchanged
=============================================================================
"""

from __future__ import annotations

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
    # =========================================================================
    # OSMIUM
    # =========================================================================
    OSMIUM_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

    # New multi-level quote controls.
    OSMIUM_PRIMARY_FRACTION = 0.80
    OSMIUM_SECONDARY_OFFSET = 2

    # =========================================================================
    # PEPPER — unchanged from algorithm18
    # =========================================================================
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    def run(self, state: TradingState):
        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            result[OSMIUM] = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
            )

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        return result, 0, ""

    def trade_osmium(self, order_depth: OrderDepth, position: int) -> list[Order]:
        bids, asks, best_bid, best_ask = self._book(order_depth)

        if best_bid is None and best_ask is None:
            return []

        fair_value = self.OSMIUM_FAIR_VALUE + self._imbalance_shift(
            best_bid=best_bid if best_bid is not None else self.OSMIUM_FAIR_VALUE - 1,
            best_ask=best_ask if best_ask is not None else self.OSMIUM_FAIR_VALUE + 1,
            best_bid_volume=bids[0][1] if bids else 0,
            best_ask_volume=-asks[0][1] if asks else 0,
            alpha=self.OSMIUM_IMBALANCE_ALPHA,
        )

        inventory = position
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        orders: list[Order] = []
        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # Active takes — unchanged from algorithm18.
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break
            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value
            if ask_price == self.OSMIUM_FAIR_VALUE - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
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
            if bid_price == self.OSMIUM_FAIR_VALUE + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False
            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False
            if not (sell_at_fair_to_reduce or sell_above_fair):
                break
            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # Passive quotes — refined multi-level version.
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        total_buy = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        total_sell = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="sell")

        fair_floor = math.floor(fair_value)
        fair_ceil = math.ceil(fair_value)

        # BUY side
        if best_bid is not None and total_buy > 0:
            primary_bid = min(best_bid + 1, fair_floor - 1)
            primary_buy = min(total_buy, max(1, math.ceil(total_buy * self.OSMIUM_PRIMARY_FRACTION)))
            secondary_buy = total_buy - primary_buy

            if primary_bid > 0:
                orders.append(Order(OSMIUM, primary_bid, min(primary_buy, buy_room)))

            # Only place the deeper quote when spread is very wide.
            if spread is not None and spread >= self.OSMIUM_WIDE_SPREAD and secondary_buy > 0:
                secondary_bid = primary_bid - self.OSMIUM_SECONDARY_OFFSET
                remaining_buy = buy_room - primary_buy
                if secondary_bid > 0 and remaining_buy > 0:
                    orders.append(Order(OSMIUM, secondary_bid, min(secondary_buy, remaining_buy)))

        # SELL side
        if best_ask is not None and total_sell > 0:
            primary_ask = max(best_ask - 1, fair_ceil + 1)
            primary_sell = min(total_sell, max(1, math.ceil(total_sell * self.OSMIUM_PRIMARY_FRACTION)))
            secondary_sell = total_sell - primary_sell

            if primary_ask > 0:
                orders.append(Order(OSMIUM, primary_ask, -min(primary_sell, sell_room)))

            if spread is not None and spread >= self.OSMIUM_WIDE_SPREAD and secondary_sell > 0:
                secondary_ask = primary_ask + self.OSMIUM_SECONDARY_OFFSET
                remaining_sell = sell_room - primary_sell
                if secondary_ask > 0 and remaining_sell > 0:
                    orders.append(Order(OSMIUM, secondary_ask, -min(secondary_sell, remaining_sell)))

        return orders

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
        self, buy_room: int, sell_room: int, spread: Optional[int], inventory: int, side: str
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
        self, best_bid: int, best_ask: int, best_bid_volume: int, best_ask_volume: int, alpha: float
    ) -> float:
        total_volume = best_bid_volume + best_ask_volume
        if total_volume <= 0:
            return 0.0
        spread = best_ask - best_bid
        imbalance = (best_bid_volume - best_ask_volume) / total_volume
        return alpha * (spread / 2.0) * imbalance
