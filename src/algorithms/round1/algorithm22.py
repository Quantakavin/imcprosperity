"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v22
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + slice cycling with drift residual
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

WHAT v22 IS:

    Start from the best-performing v18 structure and add one small new idea:
    use PEPPER's known upward drift line as an extra filter for slice trades.

WHY THIS VERSION EXISTS:

    By now the OSMIUM experiments have mostly converged:
      - one-sided handling mattered
      - weak-edge filter helped a little
      - many later nudges did nothing or hurt

    That suggests the missing path from ~10.53k toward ~10.8k may not be
    another OSMIUM threshold tweak.

    Pepper is already good, but there may still be a little room in HOW we
    cycle the slice around the big long core.

THE NEW IDEA:

    PEPPER has a strong deterministic upward drift.

    So instead of only asking:
        "is price rich/cheap versus local book fair?"

    we also ask:
        "is price rich/cheap versus today's drift line?"

    Concretely:
      - when PEPPER is above the drift line by enough, be a bit more willing
        to sell the slice
      - when PEPPER is below the drift line by enough, be a bit more willing
        to refill the slice

    The goal is NOT to fight the core long.
    The goal is only to make the slice trades a little smarter.

WHAT STAYS THE SAME:

    OSMIUM:
      same as v18

    PEPPER:
      still large long target, still hold floor, still local-fair logic
      but now with a drift-residual overlay
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
    # =========================================================================
    # OSMIUM — unchanged from v18
    # =========================================================================
    OSMIUM_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

    # =========================================================================
    # PEPPER
    # =========================================================================
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    # Drift-line overlay.
    #
    # The empirical round-1 Pepper fair rises by about 0.001 per timestamp unit.
    # We estimate the day's starting anchor from the first two-sided mid we see,
    # then project the line forward.
    PEPPER_DRIFT_SLOPE = 0.001
    PEPPER_DRIFT_BIAS = 2.0

    # Residual thresholds:
    #   residual = local_mid - drift_fair
    #
    # Positive residual means Pepper is rich versus its drift line.
    # Negative residual means Pepper is cheap versus its drift line.
    #
    # These are intentionally small because we only want to guide the slice,
    # not override the whole strategy.
    PEPPER_RESIDUAL_SELL_EDGE = 1.5
    PEPPER_RESIDUAL_BUY_EDGE = 1.5

    def run(self, state: TradingState):
        stored: dict[str, float | int | None] = {}
        if state.traderData:
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price")
        pepper_last_timestamp = stored.get("pepper_last_timestamp")
        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            result[OSMIUM] = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
            )

        if PEPPER in state.order_depths:
            pepper_orders, pepper_day_start_price, pepper_last_timestamp = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
                timestamp=state.timestamp,
                pepper_day_start_price=pepper_day_start_price,
                pepper_last_timestamp=pepper_last_timestamp,
            )
            result[PEPPER] = pepper_orders

        trader_data = json.dumps(
            {
                "pepper_day_start_price": pepper_day_start_price,
                "pepper_last_timestamp": pepper_last_timestamp,
            }
        )
        return result, 0, trader_data

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

    def trade_pepper(
        self,
        order_depth: OrderDepth,
        position: int,
        timestamp: int,
        pepper_day_start_price: Optional[float],
        pepper_last_timestamp: Optional[int],
    ) -> tuple[list[Order], Optional[float], int]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None or best_ask is None:
            # Preserve timestamp tracking even if the book is one-sided.
            return [], pepper_day_start_price, timestamp

        # Detect day reset by timestamp going backwards.
        is_new_day = pepper_day_start_price is None
        if pepper_last_timestamp is not None and timestamp < pepper_last_timestamp:
            is_new_day = True

        local_mid = (best_bid + best_ask) / 2.0
        if is_new_day:
            pepper_day_start_price = local_mid

        # Standard local-fair logic from v18 / uploaded-bot family.
        fair_value = local_mid
        fair_value += self._imbalance_shift(
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_volume=bids[0][1],
            best_ask_volume=-asks[0][1],
            alpha=self.PEPPER_IMBALANCE_ALPHA,
        )
        fair_value -= (position - self.PEPPER_LONG_TARGET) * self.PEPPER_RES_K

        # New drift-line overlay.
        #
        # We estimate where Pepper "should" be on its deterministic trend, then
        # compare the actual local mid against that line.
        drift_fair = (
            pepper_day_start_price
            + self.PEPPER_DRIFT_SLOPE * timestamp
            + self.PEPPER_DRIFT_BIAS
        )
        residual = local_mid - drift_fair

        fair_floor = math.floor(fair_value)
        fair_ceil = math.ceil(fair_value)
        inventory = position
        buy_room, sell_room = self._rooms(PEPPER, inventory)
        orders: list[Order] = []

        # Buy cheap asks:
        # require both local-fair cheapness and, if possible, some help from the
        # drift residual when trying to refill the slice.
        for ask_price, ask_volume in asks:
            if buy_room <= 0 or ask_price >= fair_value:
                break

            # If Pepper is already rich versus the drift line, do not rush to
            # refill the slice at merely locally-cheap prices.
            if residual > self.PEPPER_RESIDUAL_BUY_EDGE and inventory >= self.PEPPER_HOLD_FLOOR:
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(PEPPER, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        # Selling the slice:
        # normal local-fair condition stays, but when we are near the hold floor
        # we also want PEPPER to look rich versus the drift line.
        sell_threshold = fair_value
        if inventory < self.PEPPER_HOLD_FLOOR:
            sell_threshold += self.PEPPER_HOLD_EXTRA_EDGE

        for bid_price, bid_volume in bids:
            if sell_room <= 0 or bid_price <= sell_threshold:
                break

            # New extra guard:
            # if we are close to the core long, only sell when PEPPER is also
            # rich versus its drift line.
            if inventory <= self.PEPPER_LONG_TARGET and residual < self.PEPPER_RESIDUAL_SELL_EDGE:
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

        # Passive buy:
        # if Pepper is rich versus drift, do not lean passively on the bid as
        # much while already holding the core.
        if buy_room > 0 and my_bid > 0:
            if not (inventory >= self.PEPPER_HOLD_FLOOR and residual > self.PEPPER_RESIDUAL_BUY_EDGE):
                orders.append(Order(PEPPER, my_bid, buy_room))

        # Passive ask:
        # only post if we already own enough inventory AND Pepper looks rich
        # enough versus its drift line to justify cycling the slice out.
        if sell_room > 0 and my_ask > 0 and inventory >= self.PEPPER_HOLD_FLOOR:
            if residual >= self.PEPPER_RESIDUAL_SELL_EDGE:
                orders.append(Order(PEPPER, my_ask, -sell_room))

        return orders, pepper_day_start_price, timestamp

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
