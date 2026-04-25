"""
Round 3 trader, version 8.

Design goal:
  1. smoother equity curve first
  2. good PnL second

So this version is intentionally built around:
  - frequent inventory recycling
  - small, repeatable spread capture
  - only small and selective option risk

Compared with earlier versions:
  - HYDROGEL is no longer allowed to become a giant directional bet
  - VFE is treated as a pure Kelp-style market maker
  - options are a small "satellite" strategy, not a core engine
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math
import statistics


# ============================================================================
# Math helpers
# ============================================================================

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(spot: float, strike: float, time_years: float, sigma: float) -> float:
    intrinsic = max(spot - strike, 0.0)
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * norm_cdf(d1) - strike * norm_cdf(d2)


def implied_volatility(price: float, spot: float, strike: float, time_years: float) -> Optional[float]:
    intrinsic = max(spot - strike, 0.0)
    if spot <= 0 or price <= intrinsic + 1e-6 or price >= spot:
        return None

    low = 1e-4
    high = 2.0
    for _ in range(50):
        mid = 0.5 * (low + high)
        model = bs_call_price(spot, strike, time_years, mid)
        if model > price:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


# ============================================================================
# Book helpers
# ============================================================================

def best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_price(order_depth: OrderDepth) -> Optional[float]:
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def popular_mid(order_depth: OrderDepth) -> Optional[float]:
    """Fair value proxy that ignores tiny noisy wisps in the book."""
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return mid_price(order_depth)

    pop_bid = max(order_depth.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(order_depth.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


class Trader:
    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"

    VOUCHERS: Dict[str, int] = {
        "VEV_4000": 4000,
        "VEV_4500": 4500,
        "VEV_5000": 5000,
        "VEV_5100": 5100,
        "VEV_5200": 5200,
        "VEV_5300": 5300,
        "VEV_5400": 5400,
        "VEV_5500": 5500,
        "VEV_6000": 6000,
        "VEV_6500": 6500,
    }

    POSITION_LIMITS: Dict[str, int] = {
        HYDROGEL: 200,
        VFE: 200,
        **{name: 300 for name in VOUCHERS},
    }

    ACTIVE_VOUCHERS = ("VEV_5200", "VEV_5300", "VEV_5400")

    START_TTE_DAYS = 5.0
    TICKS_PER_DAY = 1_000_000

    SPOT_HISTORY_WINDOW = 60
    IV_HISTORY_WINDOW = 80

    # Smoother inventory bands than trader7.
    HYDROGEL_SOFT_CAP = 90
    VFE_SOFT_CAP = 35
    VOUCHER_SOFT_CAP = 30

    HYDROGEL_QUOTE_SIZE = 14
    VFE_QUOTE_SIZE = 10

    def bid(self):
        return 15

    def run(self, state: TradingState):
        memory = self._load_memory(state.traderData)
        self._ensure_memory_shape(memory)
        self._update_histories(memory, state)
        self._update_iv_histories(memory, state)

        result: Dict[str, List[Order]] = {}

        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._trade_hydrogel(
                state.order_depths[self.HYDROGEL],
                state.position.get(self.HYDROGEL, 0),
                memory["spot_history"][self.HYDROGEL],
            )

        if self.VFE in state.order_depths:
            result[self.VFE] = self._trade_vfe(
                state.order_depths[self.VFE],
                state.position.get(self.VFE, 0),
                memory["spot_history"][self.VFE],
            )

        if self.VFE in state.order_depths:
            spot = mid_price(state.order_depths[self.VFE])
            time_years = self._time_years(state.timestamp)
            for product in self.ACTIVE_VOUCHERS:
                if product not in state.order_depths:
                    continue
                orders = self._trade_voucher(
                    product=product,
                    strike=self.VOUCHERS[product],
                    order_depth=state.order_depths[product],
                    position=state.position.get(product, 0),
                    spot=spot,
                    time_years=time_years,
                    iv_history=memory["iv_history"][product],
                )
                if orders:
                    result[product] = orders

        trader_data = json.dumps(memory, separators=(",", ":"))
        return result, 0, trader_data

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
        memory.setdefault("spot_history", {})
        memory["spot_history"].setdefault(self.HYDROGEL, [])
        memory["spot_history"].setdefault(self.VFE, [])
        memory.setdefault("iv_history", {})
        for product in self.ACTIVE_VOUCHERS:
            memory["iv_history"].setdefault(product, [])

    def _time_years(self, timestamp: int) -> float:
        tte_days = max(0.05, self.START_TTE_DAYS - timestamp / self.TICKS_PER_DAY)
        return tte_days / 365.0

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        for product in (self.HYDROGEL, self.VFE):
            if product not in state.order_depths:
                continue
            mid = mid_price(state.order_depths[product])
            if mid is None:
                continue
            hist = memory["spot_history"][product]
            hist.append(mid)
            if len(hist) > self.SPOT_HISTORY_WINDOW:
                del hist[:-self.SPOT_HISTORY_WINDOW]

    def _update_iv_histories(self, memory: dict, state: TradingState) -> None:
        if self.VFE not in state.order_depths:
            return
        spot = mid_price(state.order_depths[self.VFE])
        if spot is None:
            return

        time_years = self._time_years(state.timestamp)
        for product in self.ACTIVE_VOUCHERS:
            if product not in state.order_depths:
                continue
            opt_mid = mid_price(state.order_depths[product])
            if opt_mid is None:
                continue
            iv = implied_volatility(opt_mid, spot, self.VOUCHERS[product], time_years)
            if iv is None or not (0.001 <= iv <= 0.50):
                continue
            hist = memory["iv_history"][product]
            hist.append(iv)
            if len(hist) > self.IV_HISTORY_WINDOW:
                del hist[:-self.IV_HISTORY_WINDOW]

    # ========================================================================
    # Stable market makers
    # ========================================================================

    def _trade_hydrogel(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """Controlled Resin-style market maker.

        Idea:
        - static fair works well here
        - take obvious gifts
        - flatten early
        - quote both sides most of the time
        """
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return []

        fair = 9990.0
        fair_int = int(round(fair))
        soft_cap = self.HYDROGEL_SOFT_CAP
        sim_pos = position
        orders: List[Order] = []

        # Only take if the edge is clearly positive.
        for ask in sorted(order_depth.sell_orders.keys()):
            if ask >= fair_int - 1:
                break
            if sim_pos >= soft_cap:
                break
            qty = min(-order_depth.sell_orders[ask], soft_cap - sim_pos)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, ask, qty))
                sim_pos += qty

        for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid <= fair_int + 1:
                break
            if sim_pos <= -soft_cap:
                break
            qty = min(order_depth.buy_orders[bid], sim_pos + soft_cap)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, bid, -qty))
                sim_pos -= qty

        # Explicit flattening near fair is the key smoothness feature.
        if sim_pos > 45:
            flatten_price = max(fair_int, best_bid)
            qty = min(sim_pos - 25, max(0, sim_pos + soft_cap))
            qty = min(qty, 20)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, flatten_price, -qty))
                sim_pos -= qty
        elif sim_pos < -45:
            flatten_price = min(fair_int, best_ask)
            qty = min(-25 - sim_pos, max(0, soft_cap - sim_pos))
            qty = min(qty, 20)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, flatten_price, qty))
                sim_pos += qty

        # Passive quotes. If inventory is stretched, quote only the flattening side.
        inventory_frac = sim_pos / max(soft_cap, 1)
        bid_quote = min(best_bid + 1, fair_int - 1)
        ask_quote = max(best_ask - 1, fair_int + 1)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + 1

        if inventory_frac > 0.6:
            bid_size = 0
            ask_size = min(18, sim_pos + soft_cap)
        elif inventory_frac < -0.6:
            bid_size = min(18, soft_cap - sim_pos)
            ask_size = 0
        else:
            bid_size = min(self.HYDROGEL_QUOTE_SIZE, soft_cap - sim_pos)
            ask_size = min(self.HYDROGEL_QUOTE_SIZE, sim_pos + soft_cap)

        if bid_size > 0:
            orders.append(Order(self.HYDROGEL, bid_quote, bid_size))
        if ask_size > 0:
            orders.append(Order(self.HYDROGEL, ask_quote, -ask_size))
        return orders

    def _trade_vfe(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """Low-risk Kelp-style market maker using popular mid.

        We do not assume strong predictive alpha.
        We mostly want small, frequent fills and low inventory.
        """
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return []

        fair = popular_mid(order_depth)
        if fair is None:
            fair = mid_price(order_depth)
        if fair is None:
            return []
        fair_int = int(round(fair))

        soft_cap = self.VFE_SOFT_CAP
        sim_pos = position
        orders: List[Order] = []

        spread = best_ask - best_bid
        take_edge = max(2, spread)

        # Much more selective than Hydrogel.
        for ask in sorted(order_depth.sell_orders.keys()):
            if ask > fair_int - take_edge:
                break
            if sim_pos >= soft_cap:
                break
            qty = min(-order_depth.sell_orders[ask], soft_cap - sim_pos)
            qty = min(qty, 8)
            if qty > 0:
                orders.append(Order(self.VFE, ask, qty))
                sim_pos += qty

        for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid < fair_int + take_edge:
                break
            if sim_pos <= -soft_cap:
                break
            qty = min(order_depth.buy_orders[bid], sim_pos + soft_cap)
            qty = min(qty, 8)
            if qty > 0:
                orders.append(Order(self.VFE, bid, -qty))
                sim_pos -= qty

        # Flatten quickly if stretched.
        if sim_pos > 18:
            qty = min(sim_pos - 8, 10)
            if qty > 0:
                orders.append(Order(self.VFE, max(fair_int, best_bid), -qty))
                sim_pos -= qty
        elif sim_pos < -18:
            qty = min(-8 - sim_pos, 10)
            if qty > 0:
                orders.append(Order(self.VFE, min(fair_int, best_ask), qty))
                sim_pos += qty

        inventory_frac = sim_pos / max(soft_cap, 1)
        bid_quote = min(best_bid + 1, fair_int - 1)
        ask_quote = max(best_ask - 1, fair_int + 1)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + 1

        if inventory_frac > 0.55:
            bid_size = 0
            ask_size = min(12, sim_pos + soft_cap)
        elif inventory_frac < -0.55:
            bid_size = min(12, soft_cap - sim_pos)
            ask_size = 0
        else:
            bid_size = min(self.VFE_QUOTE_SIZE, soft_cap - sim_pos)
            ask_size = min(self.VFE_QUOTE_SIZE, sim_pos + soft_cap)

        if bid_size > 0:
            orders.append(Order(self.VFE, bid_quote, bid_size))
        if ask_size > 0:
            orders.append(Order(self.VFE, ask_quote, -ask_size))
        return orders

    # ========================================================================
    # Small selective option strategy
    # ========================================================================

    def _trade_voucher(
        self,
        product: str,
        strike: int,
        order_depth: OrderDepth,
        position: int,
        spot: Optional[float],
        time_years: float,
        iv_history: List[float],
    ) -> List[Order]:
        """Small per-strike IV mean-reversion strategy.

        Important:
        - no passive quoting
        - tiny inventory
        - only act on strong deviations
        """
        if spot is None or len(iv_history) < 25:
            return []

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return []

        opt_mid = 0.5 * (best_bid + best_ask)
        iv_now = implied_volatility(opt_mid, spot, strike, time_years)
        if iv_now is None:
            return []

        fair_iv = statistics.mean(iv_history)
        iv_std = statistics.pstdev(iv_history) if len(iv_history) >= 2 else 0.0
        iv_std = max(iv_std, 0.002)
        iv_z = (iv_now - fair_iv) / iv_std

        fair_price = bs_call_price(spot, strike, time_years, fair_iv)
        spread = best_ask - best_bid
        price_edge = max(2.0, 1.0 * spread)

        cap = self.VOUCHER_SOFT_CAP
        sim_pos = position
        orders: List[Order] = []

        if iv_z <= -1.5:
            for ask in sorted(order_depth.sell_orders.keys()):
                if ask > fair_price - price_edge:
                    break
                if sim_pos >= cap:
                    break
                qty = min(-order_depth.sell_orders[ask], cap - sim_pos, 10)
                if qty > 0:
                    orders.append(Order(product, ask, qty))
                    sim_pos += qty

        if iv_z >= 1.5:
            for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
                if bid < fair_price + price_edge:
                    break
                if sim_pos <= -cap:
                    break
                qty = min(order_depth.buy_orders[bid], sim_pos + cap, 10)
                if qty > 0:
                    orders.append(Order(product, bid, -qty))
                    sim_pos -= qty

        return orders
