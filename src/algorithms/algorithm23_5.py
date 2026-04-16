"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v23-5
=============================================================================
STRUCTURAL TEST: OSMIUM live mid-price tracking instead of fixed 10,000

HYPOTHESIS:
    v23 uses a hardcoded fair value of 10,000 for OSMIUM. While the
    long-run mean IS 10,000, the sinusoidal wave means the SHORT-TERM
    fair value oscillates around it. Using the live mid-price as fair
    could improve take timing:

        - When mid = 9997, we know price is in a dip — takes at 9999
          are less attractive (only +2 edge vs fair)
        - When mid = 10003, takes at 10001 are also only +2 edge

    By anchoring takes to live mid rather than fixed 10,000, we avoid
    taking when the "edge" is just the wave being on our side temporarily.

    We blend: fair = (1 - weight) * 10000 + weight * live_mid
    Starting with weight = 0.3 — enough to shift fair by ~1-2 ticks
    when mid deviates, but still anchored to the structural 10,000.

CHANGE FROM v23 (using 23-1 clean base):
    - OSMIUM fair_value now blends fixed 10,000 with live mid-price
    - New constant: OSMIUM_MID_WEIGHT = 0.3
    - Active takes and passive quotes use this blended fair
    - Everything else unchanged

EXPECTED IMPACT:
    +100 to +300 if better take timing avoids adverse wave fills
    -100 to -200 if the blended fair causes us to miss good takes
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

    # *** NEW: blend live mid into fair value ***
    # fair = (1 - weight) * 10000 + weight * live_mid
    # At weight=0.3, if mid=9996 then fair=9998.8 (rounds to 9999)
    # This shifts take thresholds by ~1 tick in the direction of the wave
    OSMIUM_MID_WEIGHT = 0.3

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

        # *** NEW: compute live mid and blend with structural fair ***
        # If only one side exists, fall back to fixed 10,000
        if best_bid is not None and best_ask is not None:
            live_mid = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            live_mid = best_bid + 4  # assume typical half-spread
        elif best_ask is not None:
            live_mid = best_ask - 4
        else:
            live_mid = self.OSMIUM_FAIR_VALUE

        # Blended fair: mostly anchored to 10,000, slightly pulled toward mid
        blended_fair = (
            (1.0 - self.OSMIUM_MID_WEIGHT) * self.OSMIUM_FAIR_VALUE
            + self.OSMIUM_MID_WEIGHT * live_mid
        )

        # Still apply imbalance shift on top of the blended fair
        fair_value = blended_fair + self._imbalance_shift(
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

        # -----------------------------------------------------------------
        # Active takes — same logic, but using blended fair_value
        #
        # Key difference: when mid=9996 (wave dip), fair~9998.8, so
        # asks at 9999 are now ABOVE fair — we skip them instead of taking.
        # When mid=10004 (wave peak), fair~10001.2, so bids at 10001
        # are now BELOW fair — we skip selling.
        # This means we only take when there's REAL edge vs current price.
        # -----------------------------------------------------------------
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

        # -----------------------------------------------------------------
        # Passive quotes — same structure, uses blended fair for pricing
        # -----------------------------------------------------------------
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

        return orders

    def trade_pepper(self, order_depth: OrderDepth, position: int) -> list[Order]:
        """Identical to v23 PEPPER — no changes."""
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

    # =====================================================================
    # Helpers — identical to v23
    # =====================================================================

    def _scaled_passive_size(
        self, buy_room: int, sell_room: int, spread: Optional[int],
        inventory: int, side: str,
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
        self, best_bid: int, best_ask: int,
        best_bid_volume: int, best_ask_volume: int, alpha: float,
    ) -> float:
        total_volume = best_bid_volume + best_ask_volume
        if total_volume <= 0:
            return 0.0
        spread = best_ask - best_bid
        imbalance = (best_bid_volume - best_ask_volume) / total_volume
        return alpha * (spread / 2.0) * imbalance
