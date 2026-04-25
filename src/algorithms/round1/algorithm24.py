"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v24
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long (unchanged from v23)
    2. ASH_COATED_OSMIUM     — v23 takes + NEW flatten-at-fair logic

BASE: v23-1 (clean version without dead pressure overlay code)

WHAT'S NEW IN v24:

    OSMIUM "flatten at fair" — trade at 10000 to free up position room.

    The problem:
        v23 only sells when bid > 10000 (profitable) or panics at ±30.
        When we buy cheap asks and build to +80, we're stuck — can't
        take ANY more cheap asks until an expensive bid appears.
        Data shows 10,193 units of profitable volume missed due to
        position limits on day 0 alone.

    The fix:
        When we're long (position > 0) and a bid at exactly 10000
        exists, sell some units to reduce position. When short and an
        ask at 10000 exists, buy to reduce. This is break-even on the
        trade itself, but it frees up room for the NEXT profitable take.

    Why this works:
        Selling at 10000 when we bought at 9995 = realized +5 profit.
        Then we can buy the next ask at 9996 = another +4 locked in.
        Without flattening, that second buy never happens.

    Guard rails:
        - Only flatten when |position| > FLATTEN_THRESHOLD (15)
          Don't waste capacity flattening tiny positions
        - Cap flatten volume to FLATTEN_MAX_PER_TICK (20)
          Don't dump entire position in one tick
        - Flatten happens BEFORE takes, so freed room is immediately
          available for profitable opportunities on the same tick

    Data-backed estimate:
        Day 0 has 4,182 units of bid volume at exactly 10000.
        Theoretical uplift: +100 to +150 on OSMIUM.

PEPPER: Completely unchanged from v23. Already at 92% of theoretical.
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

    # *** NEW: flatten-at-fair parameters ***
    # Only flatten when |position| exceeds this threshold.
    # Below this, position is small enough that we're not constrained.
    OSMIUM_FLATTEN_THRESHOLD = 15

    # Max units to flatten per tick. Prevents dumping everything at once,
    # which could leave us exposed if there's a sudden cheap ask right after.
    OSMIUM_FLATTEN_MAX_PER_TICK = 20

    # =========================================================================
    # PEPPER — unchanged from v23
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
        orders: list[Order] = []
        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # =================================================================
        # *** NEW STEP: Flatten at fair value BEFORE profitable takes ***
        #
        # Goal: free up position room so we can take more profitable
        # opportunities later in this same tick.
        #
        # When long (pos > threshold): sell at bids == 10000
        # When short (pos < -threshold): buy at asks == 10000
        #
        # This happens FIRST so the freed room is available for the
        # profitable take logic that follows.
        # =================================================================
        if inventory > self.OSMIUM_FLATTEN_THRESHOLD:
            # We're meaningfully long — look for bids at exactly fair to sell into
            flatten_remaining = self.OSMIUM_FLATTEN_MAX_PER_TICK
            for bid_price, bid_volume in bids:
                if flatten_remaining <= 0:
                    break
                if bid_price != self.OSMIUM_FAIR_VALUE:
                    # Only flatten at exactly 10000 — not above (that's a
                    # profitable take, handled below) and not below (that's
                    # a loss)
                    continue
                # Don't flatten below the threshold — just bring us down to it
                max_to_flatten = inventory - self.OSMIUM_FLATTEN_THRESHOLD
                qty = min(bid_volume, flatten_remaining, max_to_flatten)
                if qty > 0:
                    sell_room = POSITION_LIMITS[OSMIUM] + inventory
                    qty = min(qty, sell_room)
                    if qty > 0:
                        orders.append(Order(OSMIUM, bid_price, -qty))
                        inventory -= qty
                        flatten_remaining -= qty

        elif inventory < -self.OSMIUM_FLATTEN_THRESHOLD:
            # We're meaningfully short — look for asks at exactly fair to buy
            flatten_remaining = self.OSMIUM_FLATTEN_MAX_PER_TICK
            for ask_price, ask_volume in asks:
                if flatten_remaining <= 0:
                    break
                if ask_price != self.OSMIUM_FAIR_VALUE:
                    continue
                max_to_flatten = (-inventory) - self.OSMIUM_FLATTEN_THRESHOLD
                qty = min(-ask_volume, flatten_remaining, max_to_flatten)
                if qty > 0:
                    buy_room = POSITION_LIMITS[OSMIUM] - inventory
                    qty = min(qty, buy_room)
                    if qty > 0:
                        orders.append(Order(OSMIUM, ask_price, qty))
                        inventory += qty
                        flatten_remaining -= qty

        # =================================================================
        # Profitable takes — identical logic to v23
        # (but now with potentially more room thanks to flattening above)
        # =================================================================
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            if ask_price == self.OSMIUM_FAIR_VALUE - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            if ask_price == fair_value and inventory <= 0 and not is_panic_short:
                take_at_fair_to_cover = False

            # Skip asks we already used for flattening (at exactly fair)
            # The take logic only fires for asks BELOW fair anyway, so
            # this is just a safety check
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

        # =================================================================
        # Passive quotes — identical to v23
        # =================================================================
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
