"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v18
=============================================================================
Products:
    1. INTARIAN_PEPPER_ROOT  — core long + small slice cycling
    2. ASH_COATED_OSMIUM     — aggressive live-spread market making

BASELINE FOR THIS VERSION:

    This file starts from the uploaded 10k+ bot, not from our older family of
    algorithms.

    Why?
    Because after reviewing:
      - our own best runs (v11 / v14 / v16)
      - the uploaded winning-ish run
      - the Round 1 raw book data

    the conclusion was:

      The uploaded bot is already doing the most important thing right:
      it monetizes OSMIUM's wide spread much better than our bots did.

    So v18 does NOT try to reinvent the strategy.
    It keeps the winner's shape, then adds only the few changes that still look
    justified from the logs instead of from wishful thinking.

WHAT CHANGES FROM THE UPLOADED BOT:

    PEPPER:
      - unchanged in spirit
      - keep the same "large core long + small cycling slice" logic

    OSMIUM:
      1. Handle one-sided books instead of skipping the whole tick
      2. Make passive quote size depend on spread width
      3. Add a very light filter on the weakest edges when inventory is already
         stretched

RATIONALE:

    1. One-sided books
       The uploaded bot returned no orders whenever either best bid or best ask
       was missing. That likely leaves some money on the table, because one
       side of the book is often still informative and tradable.

    2. Spread-aware sizing
       Most OSMIUM profit does not come from rare obvious mispricings.
       It comes from passively harvesting wide spreads.
       So when the spread is huge, we want full size.
       When the spread is narrow, we should be a little less eager.

    3. Weak-edge filter
       We do NOT want to smother the strategy with filters.
       But taking the very weakest fades while already heavily loaded is one
       place where a tiny bit of discipline may help.

DESIGN RULE:
    If a change is not clearly additive on paper, leave the uploaded baseline
    alone.
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
    # Fair stays fixed at 10000.
    # We are NOT using imbalance to predict fair here.
    # The earlier review suggested the big edge is execution, not forecasting.
    OSMIUM_FAIR_VALUE = 10_000
    OSMIUM_IMBALANCE_ALPHA = 0.0

    # Inventory control
    #
    # PANIC:
    #   once inventory is large enough, trading at fair to reduce inventory is
    #   acceptable because it frees room for the next profitable spread capture
    #
    # WEAK_EDGE_GUARD:
    #   only used on the smallest fades (9999 buys / 10001 sells)
    #   this is intentionally mild
    OSMIUM_PANIC_THRESHOLD = 30
    OSMIUM_WEAK_EDGE_GUARD = 45

    # Spread-aware passive sizing
    #
    # On very wide spreads, the passive edge is large, so quote full size.
    # On tighter spreads, scale down rather than turn off completely.
    OSMIUM_WIDE_SPREAD = 16
    OSMIUM_MEDIUM_SPREAD = 10
    OSMIUM_TIGHT_SPREAD = 8

    # =========================================================================
    # PEPPER
    # =========================================================================
    #
    # This is the uploaded bot's style:
    #   hold a large long target
    #   cycle a small slice around it
    PEPPER_LONG_TARGET = 80
    PEPPER_RES_K = 0.30
    PEPPER_MIN_EDGE = 4
    PEPPER_IMBALANCE_ALPHA = 1.0
    PEPPER_HOLD_FLOOR = 74
    PEPPER_HOLD_EXTRA_EDGE = 2

    def run(self, state: TradingState):
        # The engine calls this once per timestamp.
        #
        # We return:
        #   - a dict of product -> list[Order]
        #   - conversions = 0 (not used in Round 1)
        #   - traderData = "" (we are not carrying state between ticks here)
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
        # Rebuild the visible book in sorted order:
        #   bids descending  -> best buy price first
        #   asks ascending   -> best sell price first
        bids, asks, best_bid, best_ask = self._book(order_depth)

        # v18 CHANGE 1:
        # The uploaded bot skipped one-sided books entirely.
        # Here we only skip when the book is fully empty.
        if best_bid is None and best_ask is None:
            return []

        fair_value = self.OSMIUM_FAIR_VALUE + self._imbalance_shift(
            best_bid=best_bid if best_bid is not None else self.OSMIUM_FAIR_VALUE - 1,
            best_ask=best_ask if best_ask is not None else self.OSMIUM_FAIR_VALUE + 1,
            best_bid_volume=bids[0][1] if bids else 0,
            best_ask_volume=-asks[0][1] if asks else 0,
            alpha=self.OSMIUM_IMBALANCE_ALPHA,
        )

        # "inventory" is our running position as we simulate our own actions
        # inside this function.
        #
        # This matters because after we decide to buy 20 lots, our remaining
        # capacity to buy more should drop immediately for the rest of this tick.
        inventory = position
        buy_room, sell_room = self._rooms(OSMIUM, inventory)
        orders: list[Order] = []

        # Panic means inventory is already meaningful enough that flattening has
        # real value, even if we only flatten at fair instead of at a rich price.
        is_panic_long = inventory >= self.OSMIUM_PANIC_THRESHOLD
        is_panic_short = inventory <= -self.OSMIUM_PANIC_THRESHOLD

        # ---------------------------------------------------------------------
        # Active takes
        # ---------------------------------------------------------------------
        #
        # Core logic copied from the uploaded bot:
        #   - buy below fair
        #   - sell above fair
        #   - trade at fair to reduce inventory
        #
        # v18 CHANGE 3:
        #   be slightly more selective on the weakest edge only
        #   weakest buy fade  = 9999
        #   weakest sell fade = 10001
        #
        # We still allow them most of the time.
        # We only suppress them when inventory is already stretched the wrong way.
        for ask_price, ask_volume in asks:
            if buy_room <= 0:
                break

            # Two independent reasons to buy:
            #
            # 1. ask < fair
            #    Someone is offering below our fair estimate, so we just take it.
            #
            # 2. ask == fair while we are short and in panic
            #    This is NOT alpha-taking.
            #    This is inventory relief.
            #    We accept a flat-ish trade now to make room for better trades
            #    later.
            take_at_fair_to_cover = ask_price <= fair_value and inventory <= 0
            take_below_fair = ask_price < fair_value

            # Weakest buy fade:
            # buying at 9999 is only 1 point below fair.
            # That can still be fine, but when we are already quite long it is
            # the first trade we should be willing to skip.
            if ask_price == self.OSMIUM_FAIR_VALUE - 1 and inventory >= self.OSMIUM_WEAK_EDGE_GUARD:
                take_below_fair = False

            # Buying at exactly fair only makes sense as a short-covering /
            # inventory-reduction action.
            # If we are not in panic-short mode, turn that behavior off.
            if ask_price == fair_value and inventory <= 0 and not is_panic_short:
                take_at_fair_to_cover = False

            if not (take_at_fair_to_cover or take_below_fair):
                # Important:
                # once the best remaining ask is not worth taking, all later asks
                # are even worse because asks are sorted low -> high.
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(OSMIUM, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        for bid_price, bid_volume in bids:
            if sell_room <= 0:
                break

            # Symmetric logic on the sell side:
            #
            # 1. bid > fair     -> profitable sale
            # 2. bid == fair    -> allowed only when reducing a stressed long
            sell_at_fair_to_reduce = bid_price >= fair_value and inventory >= 0
            sell_above_fair = bid_price > fair_value

            # Weakest sell fade:
            # selling at 10001 is only 1 point above fair.
            # If we are already heavily short, this is the first sale to suppress.
            if bid_price == self.OSMIUM_FAIR_VALUE + 1 and inventory <= -self.OSMIUM_WEAK_EDGE_GUARD:
                sell_above_fair = False

            # Same idea as the buy-side fair trade:
            # selling exactly at fair is not a profit-seeking trade by itself.
            # It is only allowed when inventory is stretched enough to justify
            # giving up immediate edge in exchange for risk relief.
            if bid_price == fair_value and inventory >= 0 and not is_panic_long:
                sell_at_fair_to_reduce = False

            if not (sell_at_fair_to_reduce or sell_above_fair):
                # bids are sorted high -> low, so once this one is not attractive,
                # later bids are not attractive either
                break

            quantity = min(bid_volume, sell_room)
            orders.append(Order(OSMIUM, bid_price, -quantity))
            inventory -= quantity
            sell_room -= quantity

        # ---------------------------------------------------------------------
        # Passive quotes inside the live spread
        # ---------------------------------------------------------------------
        #
        # This is still the main money engine:
        #   bid at best_bid + 1
        #   ask at best_ask - 1
        # while respecting fair
        #
        # v18 CHANGE 2:
        #   scale the size depending on spread width
        #   wider spread -> quote more size
        #   tighter spread -> quote less size
        buy_room, sell_room = self._rooms(OSMIUM, inventory)

        spread = None
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        # We size the passive quotes BEFORE posting them.
        #
        # Think of this as:
        #   "How much of my remaining inventory room do I actually want to use
        #    for passive market making under this spread / inventory regime?"
        buy_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="buy")
        sell_size = self._scaled_passive_size(buy_room, sell_room, spread, inventory, side="sell")

        if best_bid is not None:
            # Quote one tick better than the current best bid if possible,
            # but never cross our fair-value boundary.
            #
            # Example:
            #   fair = 10000
            #   best bid = 9992
            #   then best_bid + 1 = 9993
            #   which is still safely below fair
            my_bid = min(best_bid + 1, math.floor(fair_value) - 1)
            if buy_size > 0 and my_bid > 0:
                orders.append(Order(OSMIUM, my_bid, buy_size))

        if best_ask is not None:
            # Symmetric sell quote:
            # improve the best ask by one tick if possible,
            # but never sell below fair
            my_ask = max(best_ask - 1, math.ceil(fair_value) + 1)
            if sell_size > 0 and my_ask > 0:
                orders.append(Order(OSMIUM, my_ask, -sell_size))

        return orders

    def trade_pepper(self, order_depth: OrderDepth, position: int) -> list[Order]:
        bids, asks, best_bid, best_ask = self._book(order_depth)
        if best_bid is None or best_ask is None:
            return []

        # Pepper fair here is NOT the deterministic drift-line formula from our
        # older bots.
        #
        # Instead this follows the uploaded bot's logic:
        #   current mid
        #   + imbalance shift
        #   + inventory target pressure
        #
        # That setup encourages us to stay heavily long, while still cycling a
        # small slice around the target when the book gets temporarily rich/cheap.
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

        # Buy any asks that are cheap versus our current Pepper fair.
        #
        # Because the desired end state is "very long Pepper", these buys are
        # serving both alpha capture and target accumulation.
        for ask_price, ask_volume in asks:
            if buy_room <= 0 or ask_price >= fair_value:
                break

            quantity = min(-ask_volume, buy_room)
            orders.append(Order(PEPPER, ask_price, quantity))
            inventory += quantity
            buy_room -= quantity

        # Sell threshold starts at fair.
        # If our inventory has dropped below the hold floor, raise the threshold.
        #
        # Translation:
        #   when we do not own enough Pepper, become pickier about selling.
        #   This protects the core long.
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

        # Passive Pepper quotes:
        #
        # Bid:
        #   keep trying to refill / maintain the long, but only with a minimum
        #   edge buffer relative to fair
        #
        # Ask:
        #   only offer passive sells when we are already above the hold floor
        #   so we are cycling the "slice", not liquidating the core
        my_bid = min(best_bid + 1, fair_floor - self.PEPPER_MIN_EDGE)
        my_ask = max(best_ask - 1, fair_ceil + self.PEPPER_MIN_EDGE)

        if buy_room > 0 and my_bid > 0:
            orders.append(Order(PEPPER, my_bid, buy_room))

        if sell_room > 0 and my_ask > 0 and inventory >= self.PEPPER_HOLD_FLOOR:
            orders.append(Order(PEPPER, my_ask, -sell_room))

        return orders

    def _scaled_passive_size(
        self,
        buy_room: int,
        sell_room: int,
        spread: Optional[int],
        inventory: int,
        side: str,
    ) -> int:
        # Pick which remaining room applies to the side we are sizing.
        room = buy_room if side == "buy" else sell_room
        if room <= 0:
            return 0

        # One-sided book:
        # we do not know the true spread, so stay somewhat conservative rather
        # than shutting down completely.
        if spread is None:
            size = max(1, room // 2)
        elif spread >= self.OSMIUM_WIDE_SPREAD:
            # This is the regime where OSMIUM usually shines:
            # very fat spread, lots of room to earn from passive fills.
            size = room
        elif spread >= self.OSMIUM_MEDIUM_SPREAD:
            # Still attractive, but not as exceptional.
            size = max(1, math.ceil(room * 0.65))
        elif spread >= self.OSMIUM_TIGHT_SPREAD:
            # Here we are much more cautious.
            size = max(1, math.ceil(room * 0.35))
        else:
            # Very tight spread -> smallest passive interest.
            size = max(1, math.ceil(room * 0.20))

        # Small inventory-aware nudge:
        # if already long, trim passive buy size
        # if already short, trim passive sell size
        if side == "buy" and inventory > 40:
            size = max(1, math.ceil(size * 0.5))
        if side == "sell" and inventory < -40:
            size = max(1, math.ceil(size * 0.5))

        return min(size, room)

    def _rooms(self, product: str, position: int) -> tuple[int, int]:
        # Returns:
        #   buy_room  = how many more we can buy before hitting +limit
        #   sell_room = how many more we can sell before hitting -limit
        limit = POSITION_LIMITS[product]
        return limit - position, limit + position

    def _book(
        self, order_depth: OrderDepth
    ) -> tuple[list[tuple[int, int]], list[tuple[int, int]], Optional[int], Optional[int]]:
        # Standard order book representation:
        #   bids sorted highest first
        #   asks sorted lowest first
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
        # Imbalance shift formula:
        #
        #   imbalance = (bid_vol - ask_vol) / total_vol
        #
        # Positive imbalance means the bid side is thicker than the ask side.
        # Negative imbalance means the ask side is thicker.
        #
        # In this file:
        #   - Pepper uses it
        #   - Osmium alpha is set to 0, so the function effectively returns 0
        #     for Osmium and just keeps the code structure tidy
        total_volume = best_bid_volume + best_ask_volume
        if total_volume <= 0:
            return 0.0

        spread = best_ask - best_bid
        imbalance = (best_bid_volume - best_ask_volume) / total_volume
        return alpha * (spread / 2.0) * imbalance
