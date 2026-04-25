"""
Round 3 trader, version 9.

Goal of this version
====================
The recent iterations taught us that trying to do "a bit of everything"
created too much noise:
  - aggressive HYDROGEL became a directional bet
  - VFE alpha was weak but stable
  - options either added variance or did nothing

So trader9 is intentionally simple:
  1. trade only HYDROGEL_PACK and VELVETFRUIT_EXTRACT
  2. market make around a fair value proxy
  3. recycle inventory aggressively
  4. prefer many small wins over a few large swings

This is a stability-first design. If it gives us a smoother curve, we can
always layer additional alpha on top later.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import statistics


# ============================================================================
# Small order-book helpers
# ============================================================================

def best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    """Return the best bid and best ask if both exist."""
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_price(order_depth: OrderDepth) -> Optional[float]:
    """Simple midpoint between best bid and best ask."""
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def popular_mid(order_depth: OrderDepth) -> Optional[float]:
    """Fair proxy using the highest-size visible bid and ask prices.

    This follows the "popular mid" idea used by several public Prosperity
    writeups: the book often has tiny noisy orders in front of the real
    consensus levels, so using the biggest size on each side can be more
    stable than blindly trusting the top tick.
    """
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return mid_price(order_depth)

    pop_bid = max(order_depth.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(order_depth.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


class Trader:
    """Prosperity-compatible trader.

    We keep the Round 2 `bid()` method because the wiki says that is harmless
    in other rounds.
    """

    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"

    POSITION_LIMITS: Dict[str, int] = {
        HYDROGEL: 200,
        VFE: 200,
    }

    # Small rolling windows only for gentle fair-value smoothing.
    HISTORY_WINDOW = 50

    # Soft caps are much lower than exchange limits. This is deliberate:
    # we are optimizing for a smoother equity curve, not for max inventory.
    HYDROGEL_SOFT_CAP = 70
    VFE_SOFT_CAP = 30

    # Base quote sizes. These are modest because we want many small fills.
    HYDROGEL_QUOTE_SIZE = 12
    VFE_QUOTE_SIZE = 10

    def bid(self):
        return 15

    def run(self, state: TradingState):
        """Main Prosperity entry point."""
        memory = self._load_memory(state.traderData)
        self._ensure_memory_shape(memory)
        self._update_histories(memory, state)

        result: Dict[str, List[Order]] = {}

        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._trade_hydrogel(
                order_depth=state.order_depths[self.HYDROGEL],
                position=state.position.get(self.HYDROGEL, 0),
                history=memory["history"][self.HYDROGEL],
            )

        if self.VFE in state.order_depths:
            result[self.VFE] = self._trade_vfe(
                order_depth=state.order_depths[self.VFE],
                position=state.position.get(self.VFE, 0),
                history=memory["history"][self.VFE],
            )

        trader_data = json.dumps(memory, separators=(",", ":"))
        conversions = 0
        return result, conversions, trader_data

    # ========================================================================
    # State helpers
    # ========================================================================

    def _load_memory(self, raw: str) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _ensure_memory_shape(self, memory: dict) -> None:
        memory.setdefault("history", {})
        memory["history"].setdefault(self.HYDROGEL, [])
        memory["history"].setdefault(self.VFE, [])

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        for product in (self.HYDROGEL, self.VFE):
            if product not in state.order_depths:
                continue
            mid = mid_price(state.order_depths[product])
            if mid is None:
                continue
            hist = memory["history"][product]
            hist.append(mid)
            if len(hist) > self.HISTORY_WINDOW:
                del hist[:-self.HISTORY_WINDOW]

    # ========================================================================
    # Shared market-making helper
    # ========================================================================

    def _stable_market_make(
        self,
        product: str,
        order_depth: OrderDepth,
        position: int,
        fair: float,
        soft_cap: int,
        base_size: int,
        take_buy_below: int,
        take_sell_above: int,
        flatten_threshold: int,
        flatten_to: int,
    ) -> List[Order]:
        """Shared logic for a stability-first market maker.

        The structure is:
          1. take only very obvious gifts
          2. if inventory is stretched, flatten toward a smaller target
          3. quote passively around fair
          4. if inventory is very stretched, quote only the flattening side

        This is designed to reduce PnL swings caused by sitting on inventory.
        """
        orders: List[Order] = []
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        fair_int = int(round(fair))
        sim_pos = position

        # ------------------------------------------------------------
        # Step 1: take only obvious gifts already available in the book
        # ------------------------------------------------------------
        # Buy if asks are clearly below fair.
        for ask in sorted(order_depth.sell_orders.keys()):
            if ask > take_buy_below:
                break
            if sim_pos >= soft_cap:
                break
            qty = min(-order_depth.sell_orders[ask], soft_cap - sim_pos)
            qty = min(qty, base_size)
            if qty > 0:
                orders.append(Order(product, ask, qty))
                sim_pos += qty

        # Sell if bids are clearly above fair.
        for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid < take_sell_above:
                break
            if sim_pos <= -soft_cap:
                break
            qty = min(order_depth.buy_orders[bid], sim_pos + soft_cap)
            qty = min(qty, base_size)
            if qty > 0:
                orders.append(Order(product, bid, -qty))
                sim_pos -= qty

        # ------------------------------------------------------------
        # Step 2: explicit flattening near fair
        # ------------------------------------------------------------
        # This is the most important smoothness feature in the whole design.
        # Once we get too far from flat, we actively trade back toward a smaller
        # inventory target instead of letting the position sit there.
        if sim_pos > flatten_threshold:
            target_reduction = sim_pos - flatten_to
            qty = min(target_reduction, base_size)
            if qty > 0:
                # Sell near fair / into the bid to recycle inventory.
                flatten_price = max(best_bid, fair_int)
                orders.append(Order(product, flatten_price, -qty))
                sim_pos -= qty

        elif sim_pos < -flatten_threshold:
            target_increase = -flatten_to - sim_pos
            qty = min(target_increase, base_size)
            if qty > 0:
                # Buy near fair / into the ask to recycle inventory.
                flatten_price = min(best_ask, fair_int)
                orders.append(Order(product, flatten_price, qty))
                sim_pos += qty

        # ------------------------------------------------------------
        # Step 3: passive quotes around fair
        # ------------------------------------------------------------
        # These are our normal market-making quotes.
        bid_quote = min(best_bid + 1, fair_int - 1)
        ask_quote = max(best_ask - 1, fair_int + 1)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + 1

        # If inventory is already stretched, we only quote the side that helps
        # us flatten. That is how we stop "market making" from becoming a
        # disguised directional strategy.
        inventory_frac = sim_pos / max(soft_cap, 1)

        if inventory_frac > 0.6:
            bid_size = 0
            ask_size = min(base_size + 4, sim_pos + soft_cap)
        elif inventory_frac < -0.6:
            bid_size = min(base_size + 4, soft_cap - sim_pos)
            ask_size = 0
        else:
            bid_size = min(base_size, soft_cap - sim_pos)
            ask_size = min(base_size, sim_pos + soft_cap)

        if bid_size > 0:
            orders.append(Order(product, bid_quote, bid_size))
        if ask_size > 0:
            orders.append(Order(product, ask_quote, -ask_size))

        return orders

    # ========================================================================
    # Product-specific strategies
    # ========================================================================

    def _trade_hydrogel(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """HYDROGEL behaves like a stable, spready Resin-style product.

        We use a mostly static fair value. Earlier versions kept letting this
        product dominate the entire equity curve, so this version keeps the
        inventory band much tighter and flattens earlier.
        """
        # Static center seen in the historical data. We keep only a tiny rolling
        # adjustment so the bot is not completely rigid if the live book shifts.
        hist_mean = statistics.mean(history) if history else 9990.0
        fair = 0.85 * 9990.0 + 0.15 * hist_mean
        fair_int = int(round(fair))

        return self._stable_market_make(
            product=self.HYDROGEL,
            order_depth=order_depth,
            position=position,
            fair=fair,
            soft_cap=self.HYDROGEL_SOFT_CAP,
            base_size=self.HYDROGEL_QUOTE_SIZE,
            take_buy_below=fair_int - 1,
            take_sell_above=fair_int + 1,
            flatten_threshold=35,
            flatten_to=18,
        )

    def _trade_vfe(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """VELVETFRUIT_EXTRACT behaves more like a low-risk Kelp-style product.

        We trust the current order book more than a predictive signal.
        The goal is many small spread captures with tiny inventory.
        """
        book_fair = popular_mid(order_depth)
        if book_fair is None:
            book_fair = mid_price(order_depth)
        if book_fair is None:
            return []

        # Blend book fair and a small rolling average to reduce jumpiness.
        hist_mean = statistics.mean(history) if history else book_fair
        fair = 0.7 * book_fair + 0.3 * hist_mean
        fair_int = int(round(fair))

        spread = 0
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is not None and best_ask is not None:
            spread = best_ask - best_bid

        # VFE is tighter, so we require a clearer edge before taking.
        take_edge = max(2, spread)

        return self._stable_market_make(
            product=self.VFE,
            order_depth=order_depth,
            position=position,
            fair=fair,
            soft_cap=self.VFE_SOFT_CAP,
            base_size=self.VFE_QUOTE_SIZE,
            take_buy_below=fair_int - take_edge,
            take_sell_above=fair_int + take_edge,
            flatten_threshold=15,
            flatten_to=6,
        )
