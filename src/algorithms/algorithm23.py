"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v23
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + small slice cycling
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

WHAT v23 IS:

    Start from the best-performing v18 structure and add one tiny stateful
    OSMIUM overlay:

        recent fill / recent regime memory

WHY THIS VERSION EXISTS:

    By now we have learned:

      - OSMIUM is where almost all incremental edge comes from
      - broad participation is important
      - being too selective kills turnover
      - local threshold nudges around v18 mostly do nothing

    So if there is still a little hidden alpha left, it is probably not in a
    new fair-value model or in large strategy rewrites.

    It is more likely in a tiny adaptation layer that answers:

        "If I have just been repeatedly bought from, or repeatedly sold to,
         should I briefly lean back on that passive side?"

THE NEW IDEA:

    We keep two small decaying pressure scores for OSMIUM:

      - buy_pressure
      - sell_pressure

    They are increased by:
      1. recent own fills
         - if we just bought, buy_pressure goes up
         - if we just sold, sell_pressure goes up

      2. one-sided book states
         - only asks visible -> mild buy-side pressure
         - only bids visible -> mild sell-side pressure

    Then, when posting passive OSMIUM quotes:
      - if buy_pressure is elevated, trim passive buy size a bit
      - if sell_pressure is elevated, trim passive sell size a bit

IMPORTANT:

    This is NOT a new prediction model.
    It is only a tiny adverse-selection / repeated-fill guard.

    The effect is intentionally mild.
    We still want the v18 behavior of participating broadly.
=============================================================================
"""

from __future__ import annotations

import json
import math
from typing import Optional

try:
    from datamodel import Order, OrderDepth, Trade, TradingState
except ModuleNotFoundError:
    from prosperity3bt.datamodel import Order, OrderDepth, Trade, TradingState


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

    # Tiny stateful overlay.
    #
    # Pressure values decay every tick and are increased by recent fills /
    # one-sided states. They only scale passive size mildly.
    OSMIUM_PRESSURE_DECAY = 0.70
    OSMIUM_FILL_PRESSURE_UNIT = 0.25
    OSMIUM_ONESIDED_PRESSURE_UNIT = 0.40
    OSMIUM_MAX_PRESSURE = 2.0
    OSMIUM_MAX_TRIM = 0.45

    # =========================================================================
    # PEPPER
    # =========================================================================
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    def run(self, state: TradingState):
        stored: dict[str, float] = {}
        if state.traderData:
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        # Start from decayed memory from the previous tick.
        buy_pressure = float(stored.get("osmium_buy_pressure", 0.0)) * self.OSMIUM_PRESSURE_DECAY
        sell_pressure = float(stored.get("osmium_sell_pressure", 0.0)) * self.OSMIUM_PRESSURE_DECAY

        # Update pressure from newly reported own trades.
        buy_pressure, sell_pressure = self._update_osmium_pressure_from_fills(
            own_trades=state.own_trades.get(OSMIUM, []),
            current_timestamp=state.timestamp,
            buy_pressure=buy_pressure,
            sell_pressure=sell_pressure,
        )

        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            osmium_orders, buy_pressure, sell_pressure = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
                buy_pressure=buy_pressure,
                sell_pressure=sell_pressure,
            )
            result[OSMIUM] = osmium_orders

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        trader_data = json.dumps(
            {
                "osmium_buy_pressure": buy_pressure,
                "osmium_sell_pressure": sell_pressure,
            }
        )
        return result, 0, trader_data

    def trade_osmium(
        self,
        order_depth: OrderDepth,
        position: int,
        buy_pressure: float,
        sell_pressure: float,
    ) -> tuple[list[Order], float, float]:
        bids, asks, best_bid, best_ask = self._book(order_depth)

        if best_bid is None and best_ask is None:
            return [], buy_pressure, sell_pressure

        # Mild regime update from one-sided books.
        #
        # If only asks exist, the market is currently presenting sell liquidity
        # to us, so we mark a little buy-side pressure.
        # If only bids exist, mark a little sell-side pressure.
        if best_bid is None and best_ask is not None:
            buy_pressure = min(self.OSMIUM_MAX_PRESSURE, buy_pressure + self.OSMIUM_ONESIDED_PRESSURE_UNIT)
        if best_ask is None and best_bid is not None:
            sell_pressure = min(self.OSMIUM_MAX_PRESSURE, sell_pressure + self.OSMIUM_ONESIDED_PRESSURE_UNIT)

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

        # ---------------------------------------------------------------------
        # Active takes — unchanged from v18
        # ---------------------------------------------------------------------
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

        # ---------------------------------------------------------------------
        # Passive quotes — v18 plus mild pressure-based trimming
        # ---------------------------------------------------------------------
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        buy_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        sell_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="sell")

        # Pressure only trims size. It does NOT change quote price, because we
        # already learned that changing quote aggressiveness too much hurts.
        buy_size = self._apply_pressure_trim(buy_size, buy_pressure)
        sell_size = self._apply_pressure_trim(sell_size, sell_pressure)

        if best_bid is not None:
            my_bid = min(best_bid + 1, math.floor(fair_value) - 1)
            if buy_size > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_size))

        if best_ask is not None:
            my_ask = max(best_ask - 1, math.ceil(fair_value) + 1)
            if sell_size > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_size))

        return orders, buy_pressure, sell_pressure

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

    def _update_osmium_pressure_from_fills(
        self,
        own_trades: list[Trade],
        current_timestamp: int,
        buy_pressure: float,
        sell_pressure: float,
    ) -> tuple[float, float]:
        # Use only the most recent own trades (previous tick / same tick reports).
        for trade in own_trades:
            if trade.timestamp < current_timestamp - 100:
                continue

            # If we were the buyer, add buy-side pressure.
            if trade.buyer == "SUBMISSION":
                buy_pressure += self.OSMIUM_FILL_PRESSURE_UNIT * trade.quantity / 10.0

            # If we were the seller, add sell-side pressure.
            if trade.seller == "SUBMISSION":
                sell_pressure += self.OSMIUM_FILL_PRESSURE_UNIT * trade.quantity / 10.0

        buy_pressure = max(0.0, min(self.OSMIUM_MAX_PRESSURE, buy_pressure))
        sell_pressure = max(0.0, min(self.OSMIUM_MAX_PRESSURE, sell_pressure))
        return buy_pressure, sell_pressure

    def _apply_pressure_trim(self, size: int, pressure: float) -> int:
        if size <= 0:
            return 0
        trim_ratio = min(self.OSMIUM_MAX_TRIM, pressure * 0.20)
        return max(1, math.ceil(size * (1.0 - trim_ratio)))

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
