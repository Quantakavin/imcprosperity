"""
Round 3 trader, version 7.

This version is based on the backtest review of trader5 and trader6:

1. HYDROGEL_PACK was consistently our best product.
   -> We make it the main PnL engine and quote it more aggressively.

2. VELVETFRUIT_EXTRACT did not show strong predictive alpha for us.
   -> We simplify it into a cleaner, lower-risk market maker.

3. The option chain was not well captured by one single fair volatility.
   -> We move to per-strike rolling implied-volatility mean reversion.

This file is written as a teachable version with many comments.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math
import statistics


# ============================================================================
# Option math helpers
# ============================================================================

def norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(spot: float, strike: float, time_years: float, sigma: float) -> float:
    """Black-Scholes price of a European call."""
    intrinsic = max(spot - strike, 0.0)
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * norm_cdf(d1) - strike * norm_cdf(d2)


def implied_volatility(price: float, spot: float, strike: float, time_years: float) -> Optional[float]:
    """Invert Black-Scholes using robust bisection."""
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
    """Best bid = highest buy price, best ask = lowest sell price."""
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_price(order_depth: OrderDepth) -> Optional[float]:
    """Simple midpoint between best bid and ask."""
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def popular_mid(order_depth: OrderDepth) -> Optional[float]:
    """Use the highest-size top-side prices as a simple fair-value anchor.

    This is a lightweight proxy for the "popular mid" idea public teams used:
    look where the biggest visible size is sitting instead of trusting every
    tiny wisp in the book equally.
    """
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return mid_price(order_depth)

    pop_bid = max(order_depth.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(order_depth.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


def top_level_imbalance(order_depth: OrderDepth) -> float:
    """Simple top-of-book size imbalance in [-1, 1]."""
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return 0.0
    bid_qty = order_depth.buy_orders.get(best_bid, 0)
    ask_qty = -order_depth.sell_orders.get(best_ask, 0)
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total


class Trader:
    """Prosperity-compatible trader."""

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

    # Focus the option strategy where the chain actually has meaningful action.
    ACTIVE_VOUCHERS = ("VEV_5200", "VEV_5300", "VEV_5400")

    START_TTE_DAYS = 5.0
    TICKS_PER_DAY = 1_000_000

    SPOT_HISTORY_WINDOW = 60
    IV_HISTORY_WINDOW = 80

    # Hydrogel is our main engine now.
    HYDROGEL_QUOTE_SIZE = 28
    HYDROGEL_SOFT_CAP = 190

    # VFE is intentionally much lower-risk.
    VFE_QUOTE_SIZE = 10
    VFE_SOFT_CAP = 40

    # Much smaller directional option risk than the exchange permits.
    VOUCHER_SOFT_CAP = 70

    def bid(self):
        return 15

    def run(self, state: TradingState):
        """Called by Prosperity every iteration."""
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

        for product in self.ACTIVE_VOUCHERS:
            if product not in state.order_depths:
                continue
            orders = self._trade_voucher(
                product=product,
                strike=self.VOUCHERS[product],
                order_depth=state.order_depths[product],
                position=state.position.get(product, 0),
                spot=mid_price(state.order_depths[self.VFE]) if self.VFE in state.order_depths else None,
                time_years=self._time_years(state.timestamp),
                iv_history=memory["iv_history"].get(product, []),
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
        """Convert round timestamp into remaining time-to-expiry in years."""
        tte_days = max(0.05, self.START_TTE_DAYS - timestamp / self.TICKS_PER_DAY)
        return tte_days / 365.0

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        """Persist rolling spot mids in traderData."""
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
        """Store rolling implied vol history separately for each active strike."""
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
            if iv is None:
                continue
            if not (0.001 <= iv <= 0.50):
                continue

            hist = memory["iv_history"][product]
            hist.append(iv)
            if len(hist) > self.IV_HISTORY_WINDOW:
                del hist[:-self.IV_HISTORY_WINDOW]

    # ========================================================================
    # Hydrogel
    # ========================================================================

    def _trade_hydrogel(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """Aggressive market making around a stable fair value.

        Rationale:
        - historical data shows Hydrogel is our best spread-capture product
        - its center is stable enough that a fixed anchor is useful
        - we want to use much more of the available spread than trader6 did
        """
        orders: List[Order] = []
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        # Static anchor plus tiny rolling adjustment.
        rolling_mean = statistics.mean(history) if history else 9990.0
        fair = 0.85 * 9990.0 + 0.15 * rolling_mean
        fair_int = int(round(fair))

        soft_cap = self.HYDROGEL_SOFT_CAP
        sim_pos = position
        long_cap = min(self.POSITION_LIMITS[self.HYDROGEL], soft_cap)
        short_cap = -min(self.POSITION_LIMITS[self.HYDROGEL], soft_cap)

        # STEP 1: take obvious gifts.
        # This is the "classic Prosperity market maker" move:
        # if someone offers below fair, buy; if someone bids above fair, sell.
        for ask in sorted(order_depth.sell_orders.keys()):
            if ask >= fair_int:
                break
            if sim_pos >= long_cap:
                break
            qty = min(-order_depth.sell_orders[ask], long_cap - sim_pos)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, ask, qty))
                sim_pos += qty

        for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid <= fair_int:
                break
            if sim_pos <= short_cap:
                break
            qty = min(order_depth.buy_orders[bid], sim_pos - short_cap)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, bid, -qty))
                sim_pos -= qty

        # STEP 2: flatten at or near fair when inventory is stretched.
        # This is important because we want to recycle risk capacity so we can
        # keep harvesting more spread later.
        if sim_pos > 120 and fair_int in order_depth.buy_orders:
            qty = min(order_depth.buy_orders[fair_int], sim_pos - 80)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, fair_int, -qty))
                sim_pos -= qty
        if sim_pos < -120 and fair_int in order_depth.sell_orders:
            qty = min(-order_depth.sell_orders[fair_int], -80 - sim_pos)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, fair_int, qty))
                sim_pos += qty

        # STEP 3: quote inside the spread, inventory-aware.
        inventory_frac = sim_pos / max(soft_cap, 1)
        quote_shift = int(round(2.0 * inventory_frac))

        bid_quote = min(best_bid + 1, fair_int - 1 - quote_shift)
        ask_quote = max(best_ask - 1, fair_int + 1 - quote_shift)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + 1

        buy_capacity = max(0, long_cap - sim_pos)
        sell_capacity = max(0, sim_pos - short_cap)

        if inventory_frac > 0.75:
            buy_size = 0
            sell_size = min(sell_capacity, self.HYDROGEL_QUOTE_SIZE * 2)
        elif inventory_frac < -0.75:
            buy_size = min(buy_capacity, self.HYDROGEL_QUOTE_SIZE * 2)
            sell_size = 0
        else:
            buy_size = min(buy_capacity, int(round(self.HYDROGEL_QUOTE_SIZE * max(0.25, 1.0 - inventory_frac))))
            sell_size = min(sell_capacity, int(round(self.HYDROGEL_QUOTE_SIZE * max(0.25, 1.0 + inventory_frac))))

        if buy_size > 0:
            orders.append(Order(self.HYDROGEL, bid_quote, buy_size))
        if sell_size > 0:
            orders.append(Order(self.HYDROGEL, ask_quote, -sell_size))

        return orders

    # ========================================================================
    # VFE
    # ========================================================================

    def _trade_vfe(self, order_depth: OrderDepth, position: int, history: List[float]) -> List[Order]:
        """Cleaner, low-risk VFE market making.

        We intentionally avoid pretending we can forecast VFE strongly.
        This is a Kelp-style strategy:
        - current fair is close to current book
        - only take obvious edges
        - mostly collect spread with tight inventory limits
        """
        orders: List[Order] = []
        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        roll_mean = statistics.mean(history) if history else 0.5 * (best_bid + best_ask)
        fair = 0.5 * popular_mid(order_depth) + 0.5 * roll_mean
        fair_int = int(round(fair))

        soft_cap = self.VFE_SOFT_CAP
        sim_pos = position
        long_cap = min(self.POSITION_LIMITS[self.VFE], soft_cap)
        short_cap = -min(self.POSITION_LIMITS[self.VFE], soft_cap)

        spread = best_ask - best_bid
        take_edge = max(2.0, spread)

        # Only take very clear gifts. We do not want VFE to become a big view.
        for ask in sorted(order_depth.sell_orders.keys()):
            if ask > fair_int - take_edge:
                break
            if sim_pos >= long_cap:
                break
            qty = min(-order_depth.sell_orders[ask], long_cap - sim_pos)
            if qty > 0:
                orders.append(Order(self.VFE, ask, qty))
                sim_pos += qty

        for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid < fair_int + take_edge:
                break
            if sim_pos <= short_cap:
                break
            qty = min(order_depth.buy_orders[bid], sim_pos - short_cap)
            if qty > 0:
                orders.append(Order(self.VFE, bid, -qty))
                sim_pos -= qty

        inventory_frac = sim_pos / max(soft_cap, 1)
        quote_shift = int(round(2.0 * inventory_frac))

        bid_quote = min(best_bid + 1, fair_int - 1 - quote_shift)
        ask_quote = max(best_ask - 1, fair_int + 1 - quote_shift)
        if ask_quote <= bid_quote:
            ask_quote = bid_quote + 1

        buy_capacity = max(0, long_cap - sim_pos)
        sell_capacity = max(0, sim_pos - short_cap)

        if inventory_frac > 0.70:
            buy_size = 0
            sell_size = min(sell_capacity, self.VFE_QUOTE_SIZE * 2)
        elif inventory_frac < -0.70:
            buy_size = min(buy_capacity, self.VFE_QUOTE_SIZE * 2)
            sell_size = 0
        else:
            buy_size = min(buy_capacity, int(round(self.VFE_QUOTE_SIZE * max(0.25, 1.0 - inventory_frac))))
            sell_size = min(sell_capacity, int(round(self.VFE_QUOTE_SIZE * max(0.25, 1.0 + inventory_frac))))

        if buy_size > 0:
            orders.append(Order(self.VFE, bid_quote, buy_size))
        if sell_size > 0:
            orders.append(Order(self.VFE, ask_quote, -sell_size))

        return orders

    # ========================================================================
    # Options
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
        """Per-strike rolling-IV mean reversion, take-only.

        This is the big options change from trader6.

        Instead of saying "all options share one fair sigma", we say:
        - each strike has its own rolling implied-volatility behavior
        - if current IV is much higher than that strike's normal IV, sell it
        - if current IV is much lower than that strike's normal IV, buy it

        We still convert back to a fair PRICE, because the order book is in
        prices, not in vol points.
        """
        orders: List[Order] = []
        if spot is None:
            return orders

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders
        if len(iv_history) < 20:
            return orders

        opt_mid = 0.5 * (best_bid + best_ask)
        iv_now = implied_volatility(opt_mid, spot, strike, time_years)
        if iv_now is None:
            return orders

        fair_iv = statistics.mean(iv_history)
        iv_std = statistics.pstdev(iv_history) if len(iv_history) >= 2 else 0.0
        iv_std = max(iv_std, 0.0015)

        # Entry logic:
        # trade only if current IV is far enough from its own history.
        iv_z = (iv_now - fair_iv) / iv_std
        fair_price = bs_call_price(spot, strike, time_years, fair_iv)

        cap = min(self.POSITION_LIMITS[product], self.VOUCHER_SOFT_CAP)
        sim_pos = position
        spread = best_ask - best_bid
        price_edge = max(1.5, 0.9 * spread)

        # If current IV is LOW for this strike, the option is cheap -> buy asks.
        if iv_z <= -1.2:
            for ask in sorted(order_depth.sell_orders.keys()):
                if ask > fair_price - price_edge:
                    break
                if sim_pos >= cap:
                    break
                qty = min(-order_depth.sell_orders[ask], cap - sim_pos)
                if qty > 0:
                    orders.append(Order(product, ask, qty))
                    sim_pos += qty

        # If current IV is HIGH for this strike, the option is rich -> sell bids.
        if iv_z >= 1.2:
            for bid in sorted(order_depth.buy_orders.keys(), reverse=True):
                if bid < fair_price + price_edge:
                    break
                if sim_pos <= -cap:
                    break
                qty = min(order_depth.buy_orders[bid], sim_pos + cap)
                if qty > 0:
                    orders.append(Order(product, bid, -qty))
                    sim_pos -= qty

        return orders
