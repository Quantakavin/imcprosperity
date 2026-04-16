"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v20
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + small slice cycling
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

WHAT v20 IS:

    A clean "best-of" version after the v18 ablations and the failed v19
    quote-aggressiveness experiment.

WHY THIS FILE EXISTS:

    We tested the v18 ideas separately:

      v18.1 one-sided book handling     -> strong positive
      v18.2 spread-aware sizing         -> no effect
      v18.3 weak-edge inventory filter  -> tiny positive
      v19 quote aggressiveness          -> clearly worse

    So the correct response is:

      keep what worked
      delete what did not
      stop getting fancy

WHAT v20 KEEPS:

    OSMIUM:
      1. One-sided book handling
      2. Tiny weak-edge inventory filter

    PEPPER:
      same uploaded-bot logic

WHAT v20 REMOVES:

    OSMIUM:
      - spread-aware passive sizing
      - smarter quote aggressiveness

RATIONALE:

    The data says the main edge still comes from:
      - broad participation in wide OSMIUM spreads
      - not skipping useful one-sided states

    The failed v19 run showed that being "too smart" about quote placement can
    accidentally cut the exact turnover that makes the strategy profitable.

    So this bot goes back to:
      quote aggressively enough to get filled
      keep the structure simple
      avoid only the weakest inventory-adds when already stretched
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
    #
    # Keep fair fixed at 10000.
    # The strategy is still about execution, not fancy prediction.
    OSMIUM_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0

    # Inventory controls
    #
    # PANIC:
    #   when inventory is large enough, we allow fair-price reduction trades to
    #   free up capacity
    #
    # WEAK_EDGE_GUARD:
    #   suppress only the weakest fades (9999 buys / 10001 sells) when already
    #   stretched in the wrong direction
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45

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

        # Proven improvement from v18.1:
        # do not abandon the tick just because one side is missing.
        # Only skip when the book is completely empty.
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
        # Structure stays very close to the uploaded winner:
        #   buy below fair
        #   sell above fair
        #   allow fair-price inventory reduction when position is stressed
        #
        # The only extra filter we keep:
        #   trim the weakest fades when inventory is already stretched
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            # Weakest buy fade = 9999.
            if ask_price == self.OSMIUM_FAIR_VALUE - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            # Buying at fair is only for inventory relief, not for alpha capture.
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

            # Selling at fair is only for inventory relief.
            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # ---------------------------------------------------------------------
        # Passive quotes
        # ---------------------------------------------------------------------
        #
        # Go back to the simpler, empirically stronger quote style:
        #   improve the best bid by 1 tick
        #   improve the best ask by 1 tick
        #
        # No spread-sizing.
        # No smarter skew logic.
        # The ablations / v19 result said simplicity wins here.
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        if best_bid is not None:
            my_bid = min(best_bid + 1, math.floor(fair_value) - 1)
            if buy_room > 0:
                orders.append(Order(OSMIUM, my_bid, buy_room))

        if best_ask is not None:
            my_ask = max(best_ask - 1, math.ceil(fair_value) + 1)
            if sell_room > 0:
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
