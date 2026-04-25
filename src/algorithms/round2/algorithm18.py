"""
=============================================================================
PROSPERITY 4 — ROUND 2 TRADER 15
=============================================================================
Changes from trader14:

PEPPER:
    - Get long MUCH faster. PEPPER_MIN_EDGE lowered (4 -> 2) so passive MM sits
      closer to fair and fills more aggressively early in the day.
    - Removed the PEPPER_RELOAD_EDGE gate, which was preventing aggressive
      buys when position was already high but fair was still rising.
    - Taking logic: buy any ask at or below fair + small slack, since the
      0.1/tick drift means holding is nearly free-money until pos limit.
    - Hold floor lowered to 60 so the "don't sell unless wide edge" regime
      kicks in earlier and protects the directional long.

OSMIUM:
    - Spread regime thresholds tightened (19/13/9 -> 14/10/7) so more book
      states qualify as tradeable.
    - Passive sizing boosted in medium/tight regimes (0.42/0.12/0.05 ->
      0.6/0.25/0.10) to capture more MM fills.
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

    # Loosened spread thresholds -- more book states now qualify as tradeable
    OSMIUM_WIDE_SPREAD = 14
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 7

    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.15             # was 0.27 -- softer inventory skew so we don't fight the drift
    PEPPER_MIN_EDGE = 2             # was 4 -- tighter MM quotes fill much faster
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 60          # was 72 -- start defending the long earlier
    PEPPER_HOLD_EXTRA_EDGE = 3      # was 2 -- only sell on bigger edge when defending
    PEPPER_TAKE_SLACK = 1           # buy any ask at fair + 1 or below (trend pays us back)

    def bid(self) -> int:
        return 1500

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

    # ================================================================
    # OSMIUM
    # ================================================================
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

        # Take cheap asks
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

        # Take rich bids
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

    # ================================================================
    # PEPPER -- aggressive directional long
    # ================================================================
    def trade_pepper(self, order_depth: OrderDepth, position: int) -> list[Order]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None or best_ask is None:
            return []

        # Fair = mid + small imbalance shift; the 0.1/tick drift is implicit
        # (we just keep buying anything at or below fair, and the trend pays us).
        fair_value = (best_bid + best_ask) / 2.0
        fair_value += self._imbalance_shift(
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_volume=bids[0][1],
            best_ask_volume=-asks[0][1],
            alpha=self.PEPPER_IMBALANCE_ALPHA,
        )
        # Softer inventory skew -- we WANT to hold long here
        fair_value -= (position - self.PEPPER_LONG_TARGET) * self.PEPPER_RES_K

        fair_floor = math.floor(fair_value)
        fair_ceil = math.ceil(fair_value)
        inventory = position
        buy_room, sell_room = self._rooms(PEPPER, inventory)
        orders: list[Order] = []

        # ---- TAKE: buy anything at or below fair + slack ----
        # The deterministic drift means even buying at fair is +EV if we hold.
        buy_ceiling = fair_value + self.PEPPER_TAKE_SLACK
        for ask_price, ask_volume in asks:
            if buy_room <= 0 or ask_price > buy_ceiling:
                break
            quantity = min(-ask_volume, buy_room)
            if quantity <= 0:
                continue
            orders.append(Order(PEPPER, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        # ---- TAKE: only sell into rich bids; defend long aggressively ----
        # If we're at or above hold floor, require extra edge before selling
        sell_threshold = fair_value
        if inventory >= self.PEPPER_HOLD_FLOOR:
            sell_threshold = fair_value + self.PEPPER_HOLD_EXTRA_EDGE

        for bid_price, bid_volume in bids:
            if sell_room <= 0 or bid_price <= sell_threshold:
                break
            quantity = min(bid_volume, sell_room)
            if quantity <= 0:
                continue
            orders.append(Order(PEPPER, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

            # Update threshold after each fill in case we crossed the floor
            sell_threshold = fair_value
            if inventory >= self.PEPPER_HOLD_FLOOR:
                sell_threshold = fair_value + self.PEPPER_HOLD_EXTRA_EDGE

        # ---- MAKE: aggressive bid, conservative ask ----
        # Tight bid (fair - 2) to accumulate long fast
        my_bid = min(best_bid + 1, fair_floor - self.PEPPER_MIN_EDGE)
        # Ask stays wide -- we don't want to get short
        my_ask = max(best_ask - 1, fair_ceil + self.PEPPER_MIN_EDGE + 1)

        if buy_room > 0 and my_bid > 0:
            orders.append(Order(PEPPER, my_bid, buy_room))

        # Only post ask if we're at the long target or beyond (trim excess)
        if sell_room > 0 and my_ask > 0 and inventory >= self.PEPPER_LONG_TARGET:
            # Only sell a small slice at a time to keep the inventory high
            slice_size = min(5, sell_room)
            orders.append(Order(PEPPER, my_ask, -slice_size))

        return orders

    # ================================================================
    # Helpers
    # ================================================================
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

        # Boosted sizing across all regimes
        if spread is None:
            size = max(1, room // 4)
        elif spread >= self.OSMIUM_WIDE_SPREAD:
            size = max(1, math.ceil(room))
        elif spread >= self.OSMIUM_MEDIUM_SPREAD:
            size = max(1, math.ceil(room * 0.60))
        elif spread >= self.OSMIUM_TIGHT_SPREAD:
            size = max(1, math.ceil(room * 0.25))
        else:
            size = max(1, math.ceil(room * 0.10))

        if side == "buy":
            if inventory > 40:
                size = max(1, math.ceil(size * 0.25))
            elif inventory < -40:
                size = max(1, math.ceil(size * 1.60))
        else:
            if inventory < -40:
                size = max(1, math.ceil(size * 0.15))
            elif inventory > 40:
                size = max(1, math.ceil(size * 1.50))

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