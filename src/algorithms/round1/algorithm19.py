"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v19
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + small slice cycling
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

WHAT v19 IS:

    This is the first post-ablation version.

    We tested the 3 Osmium changes from v18 separately:

      v18.1 = one-sided book handling only   -> strong positive
      v18.2 = spread-aware sizing only       -> no effect
      v18.3 = weak-edge filter only          -> tiny positive

    So v19 keeps what worked and removes what didn't.

WHAT v19 KEEPS:

    OSMIUM:
      1. One-sided book handling
      2. Weak-edge inventory filter

    PEPPER:
      same uploaded-bot logic as before

WHAT v19 REMOVES:

    OSMIUM:
      - spread-aware passive sizing

    Reason:
      The ablation showed it added effectively nothing in this implementation.

WHAT v19 ADDS:

    OSMIUM smarter quote aggressiveness.

    This is the one new idea:

      We still quote around:
          best_bid + 1
          best_ask - 1

      But now we do NOT always improve both sides equally.

      Instead:
        - if inventory is near flat and spread is wide, stay aggressive
        - if inventory is long, back off the bid and lean harder into the ask
        - if inventory is short, do the opposite
        - if spread is relatively tight, do not force needless aggression

Why this is the next sensible step:

    The ablations said the main gain came from participating on more OSMIUM
    states, especially one-sided books.

    The next place to look for extra PnL is not a fancy predictor.
    It is the exact price at which we choose to rest our passive quotes.

    In short:
      same fair
      same general idea
      better quote geometry
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

    # Keep the tiny positive filter from v18.3.
    OSMIUM_WEAK_EDGE_GUARD = 45
    OSMIUM_PANIC_THRESHOLD = 30

    # Quote-aggressiveness controls.
    #
    # BIG spread:
    #   improving the quote by one tick is usually worthwhile
    #
    # SMALL spread:
    #   less reason to pay up aggressively for queue position
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_TIGHT_SPREAD = 10

    # Inventory bands for quote skew.
    OSMIUM_LIGHT_INVENTORY = 20
    OSMIUM_HEAVY_INVENTORY = 45

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

        # Keep the proven improvement from v18.1:
        # only skip if the book is fully empty.
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

        # ---------------------------------------------------------------------
        # Active takes
        # ---------------------------------------------------------------------
        #
        # Keep friend baseline structure, plus the small positive weak-edge
        # filter from v18.3.
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            # Weakest buy fade = 9999.
            # Still okay sometimes, but first thing to drop when already long.
            if ask_price == self.OSMIUM_FAIR_VALUE - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            # Trading at fair is only for stressed short-covering.
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

            # Weakest sell fade = 10001.
            if bid_price == self.OSMIUM_FAIR_VALUE + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False

            # Trading at fair is only for stressed long reduction.
            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # ---------------------------------------------------------------------
        # Passive quotes with smarter aggressiveness
        # ---------------------------------------------------------------------
        #
        # v19's one new idea:
        #
        # The old winner effectively always improved the market by one tick:
        #   bid = best_bid + 1
        #   ask = best_ask - 1
        #
        # Here we still often do that, but we make the aggressiveness depend on:
        #   1. current spread width
        #   2. current inventory
        #
        # Intuition:
        #   - near flat + wide spread -> stay aggressive
        #   - long inventory          -> buy less aggressively, sell more eagerly
        #   - short inventory         -> sell less aggressively, buy more eagerly
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        if best_bid is not None:
            my_bid = self._osmium_bid_price(best_bid, fair_value, spread, inventory)
            if buy_room > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_room))

        if best_ask is not None:
            my_ask = self._osmium_ask_price(best_ask, fair_value, spread, inventory)
            if sell_room > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_room))

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

    def _osmium_bid_price(
        self,
        best_bid: int,
        fair_value: float,
        spread: Optional[int],
        inventory: int,
    ) -> int:
        # Start from a conservative baseline: join the current best bid.
        my_bid = best_bid

        # If spread is not visible, only improve aggressively when inventory says
        # we actually WANT more long exposure.
        if spread is None:
            if inventory <= -self.OSMIUM_LIGHT_INVENTORY:
                my_bid = best_bid + 1
        else:
            # Wide spread + neutral/short inventory -> improve by 1 tick.
            if spread >= self.OSMIUM_WIDE_SPREAD and inventory < self.OSMIUM_HEAVY_INVENTORY:
                my_bid = best_bid + 1

            # If the spread is merely moderate and we are not short, do not force
            # extra aggressiveness on the buy side.
            if spread <= self.OSMIUM_TIGHT_SPREAD and inventory >= 0:
                my_bid = best_bid

            # Inventory skew:
            #   long inventory -> back off the bid
            #   short inventory -> stay more aggressive on the bid
            if inventory >= self.OSMIUM_HEAVY_INVENTORY:
                my_bid = best_bid
            elif inventory <= -self.OSMIUM_HEAVY_INVENTORY:
                my_bid = best_bid + 1

        # Never cross above fair on the bid.
        return min(my_bid, math.floor(fair_value) - 1)

    def _osmium_ask_price(
        self,
        best_ask: int,
        fair_value: float,
        spread: Optional[int],
        inventory: int,
    ) -> int:
        # Start from a conservative baseline: join the current best ask.
        my_ask = best_ask

        if spread is None:
            if inventory >= self.OSMIUM_LIGHT_INVENTORY:
                my_ask = best_ask - 1
        else:
            # Wide spread + neutral/long inventory -> improve by 1 tick.
            if spread >= self.OSMIUM_WIDE_SPREAD and inventory > -self.OSMIUM_HEAVY_INVENTORY:
                my_ask = best_ask - 1

            # If spread is tighter and we are not long, no need to lean hard on
            # the sell side.
            if spread <= self.OSMIUM_TIGHT_SPREAD and inventory <= 0:
                my_ask = best_ask

            # Inventory skew:
            #   long inventory -> stay more aggressive on the ask
            #   short inventory -> back off the ask
            if inventory >= self.OSMIUM_HEAVY_INVENTORY:
                my_ask = best_ask - 1
            elif inventory <= -self.OSMIUM_HEAVY_INVENTORY:
                my_ask = best_ask

        # Never cross below fair on the ask.
        return max(my_ask, math.ceil(fair_value) + 1)

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
