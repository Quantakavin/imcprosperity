"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v28
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long (unchanged)
    2. ASH_COATED_OSMIUM     — adaptive fair value via running EMA

BASE: v23-1 (clean, no pressure overlay)

THE BIG DISCOVERY:

    The bot market-makers do NOT center on 10,000 every day.

    Raw data analysis across 3 days:
        Day -2: bots center ~9996-9997,  EoD mid ~9992
        Day -1: bots center ~10001,      EoD mid ~10001
        Day 0:  bots center ~10001-10002, EoD mid ~10008

    Using a fixed fair=10000 leaves hundreds of seashells on the table:
        Day -2: fixed=2474, optimal=4409  (+1935 gap!)
        Day -1: fixed=3882, optimal=3975  (+93)
        Day 0:  fixed=2973, optimal=3279  (+306)

    This is NOT overfitting — it's a structural feature that varies
    per day. We can't know the day's center in advance, but we CAN
    track it in real-time using an EMA of the observed mid-price.

WHAT'S NEW:

    OSMIUM fair value is now a running EMA of (best_bid + best_ask) / 2,
    persisted across ticks via traderData. Starts at 10,000 and adapts.

    Raw data simulation results (with actual EoD MtM):
        alpha=0.05:  day-2=3472, day-1=3786, day0=3905
        fixed 10000: day-2=2474, day-1=3882, day0=2973

    alpha=0.05 wins on 2 of 3 days and has the best total.
    It's slow enough to not chase noise but fast enough to detect
    the daily regime within ~200 ticks.

    The weak_edge_guard and panic thresholds still reference the
    STRUCTURAL fair (10,000) since those are absolute safety limits.
    Only the take/quote decisions use the adaptive EMA fair.

PEPPER: Completely unchanged.
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
    # OSMIUM
    # =========================================================================
    # Structural anchor — the long-run mean. Used for safety guards only.
    OSMIUM_STRUCTURAL_FAIR = 10_000

    # EMA smoothing factor for adaptive fair value.
    # 0.05 means ~20-tick half-life. Fast enough to detect daily regime
    # within 200 ticks, slow enough to not chase single-tick noise.
    OSMIUM_EMA_ALPHA = 0.05

    OSMIUM_IMBALANCE_ALPHA = 0.0
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

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
        # ---- Restore persisted EMA from traderData ----
        stored: dict = {}
        if state.traderData:
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        osmium_ema = float(stored.get("osmium_ema", self.OSMIUM_STRUCTURAL_FAIR))

        result: dict[str, list[Order]] = {}

        if OSMIUM in state.order_depths:
            result[OSMIUM], osmium_ema = self.trade_osmium(
                order_depth=state.order_depths[OSMIUM],
                position=state.position.get(OSMIUM, 0),
                ema=osmium_ema,
            )

        if PEPPER in state.order_depths:
            result[PEPPER] = self.trade_pepper(
                order_depth=state.order_depths[PEPPER],
                position=state.position.get(PEPPER, 0),
            )

        trader_data = json.dumps({"osmium_ema": osmium_ema})
        return result, 0, trader_data

    def trade_osmium(
        self,
        order_depth: OrderDepth,
        position: int,
        ema: float,
    ) -> tuple[list[Order], float]:
        bids, asks, best_bid, best_ask = self._book(order_depth)

        if best_bid is None and best_ask is None:
            return [], ema

        # =================================================================
        # *** NEW: Update EMA from current mid-price ***
        #
        # Only update when both sides of the book are present, so
        # one-sided snapshots don't drag the EMA to an extreme.
        # =================================================================
        if best_bid is not None and best_ask is not None:
            live_mid = (best_bid + best_ask) / 2.0
            ema = self.OSMIUM_EMA_ALPHA * live_mid + (1.0 - self.OSMIUM_EMA_ALPHA) * ema

        # The adaptive fair is the EMA. This is what drives take/quote decisions.
        fair_value = ema

        # Apply imbalance shift on top of EMA (currently alpha=0, so no-op)
        fair_value += self._imbalance_shift(
            best_bid=best_bid if best_bid is not None else self.OSMIUM_STRUCTURAL_FAIR - 1,
            best_ask=best_ask if best_ask is not None else self.OSMIUM_STRUCTURAL_FAIR + 1,
            best_bid_volume=bids[0][1] if bids else 0,
            best_ask_volume=-asks[0][1] if asks else 0,
            alpha=self.OSMIUM_IMBALANCE_ALPHA,
        )

        inventory = position
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        orders: list[Order] = []

        # Panic thresholds still use absolute position, not relative to EMA.
        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # -----------------------------------------------------------------
        # Active takes — same structure as v23, but using adaptive fair_value
        #
        # Key difference: if EMA has drifted to 10001.5, then:
        #   - asks at 10001 are now BELOW fair → we buy them
        #   - bids at 10001 are now BELOW fair → we DON'T sell
        # This naturally aligns our trading with the market's true center.
        # -----------------------------------------------------------------
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            # Weak edge guard still uses STRUCTURAL fair (10000).
            # This is a hard safety limit: don't buy 9999 when very long,
            # regardless of where the EMA is.
            if ask_price == self.OSMIUM_STRUCTURAL_FAIR - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            if ask_price == math.floor(fair_value) and inventory <= 0 and not is_panic_short:
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

            # Weak edge guard: don't sell 10001 when very short
            if bid_price == self.OSMIUM_STRUCTURAL_FAIR + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False

            if bid_price == math.ceil(fair_value) and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # -----------------------------------------------------------------
        # Passive quotes — same structure, using adaptive fair for pricing
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

        return orders, ema

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
    # Helpers
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
