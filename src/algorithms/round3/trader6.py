"""
Round 3 trader, version 6.

This is the follow-up to trader5 after reviewing the backtest logs.

Main lessons from trader5
=========================
1. HYDROGEL_PACK market making was directionally okay, but inventory swings
   were larger than necessary.
2. VELVETFRUIT_EXTRACT became the main loss driver because we let it do too
   many jobs at once: spot alpha, market making, and option hedging.
3. The option model itself was not disastrous, but passive option quoting
   created one-sided inventories and unnecessary hedge pressure.

What trader6 changes
====================
1. HYDROGEL_PACK:
   - keep simple fair-value market making
   - keep taking obvious gifts
   - flatten inventory earlier and more aggressively

2. VELVETFRUIT_EXTRACT:
   - much smaller directional risk budget
   - mostly a hedge / low-risk MM instrument
   - weaker standalone alpha and wider thresholds for aggressive taking

3. Vouchers:
   - focus only on the most relevant middle strikes
   - no passive option quoting
   - take-only mispricing trades
   - smaller inventory caps
   - only partial hedge through VFE

The file is heavily commented so the logic stays easy to follow.
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
    """Black-Scholes price for a European call, with zero interest rate."""
    intrinsic = max(spot - strike, 0.0)
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return intrinsic

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * norm_cdf(d1) - strike * norm_cdf(d2)


def bs_call_delta(spot: float, strike: float, time_years: float, sigma: float) -> float:
    """Black-Scholes delta for a call option."""
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot > strike else 0.0

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    return norm_cdf(d1)


def implied_volatility(price: float, spot: float, strike: float, time_years: float) -> Optional[float]:
    """Invert Black-Scholes by bisection."""
    intrinsic = max(spot - strike, 0.0)
    if price <= intrinsic + 1e-6 or price >= spot or spot <= 0:
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
# Order book helpers
# ============================================================================

def best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    """Return the best bid and best ask."""
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_price(order_depth: OrderDepth) -> Optional[float]:
    """Return the simple midpoint between best bid and ask."""
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def top_level_imbalance(order_depth: OrderDepth) -> float:
    """Simple order-book imbalance in [-1, 1]."""
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
    """Prosperity-compatible trader class."""

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

    # Focus only on the middle strikes where the option actually has useful
    # time value and where mispricings should matter most.
    ACTIVE_VOUCHERS = {"VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400"}

    START_TTE_DAYS = 5.0
    TICKS_PER_DAY = 1_000_000

    SPOT_HISTORY_WINDOW = 60
    IV_HISTORY_WINDOW = 80

    HYDROGEL_QUOTE_SIZE = 14
    VFE_QUOTE_SIZE = 12

    # Trader5's VFE exposure was too large. We now cap our intended VFE target
    # much more tightly even though the exchange permits 200.
    VFE_SOFT_CAP = 60
    HYDROGEL_SOFT_CAP = 140

    # Options are also capped more tightly than the exchange limit.
    VOUCHER_SOFT_CAP = 60

    def bid(self):
        return 15

    def run(self, state: TradingState):
        """Main method required by the Prosperity API."""
        memory = self._load_memory(state.traderData)
        self._ensure_memory_shape(memory)
        self._update_histories(memory, state)

        result: Dict[str, List[Order]] = {}

        voucher_context = self._build_voucher_context(memory, state)
        hedge_target_vfe = voucher_context["hedge_target_vfe"]

        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._trade_spot_product(
                product=self.HYDROGEL,
                order_depth=state.order_depths[self.HYDROGEL],
                position=state.position.get(self.HYDROGEL, 0),
                price_history=memory["spot_history"][self.HYDROGEL],
                base_fair=9990.0,
                quote_size=self.HYDROGEL_QUOTE_SIZE,
                target_position=0,
                soft_cap=self.HYDROGEL_SOFT_CAP,
                mean_reversion_strength=0.25,
                imbalance_strength=0.90,
                take_edge_mult=0.60,
                flatten_aggression=1.40,
            )

        if self.VFE in state.order_depths:
            result[self.VFE] = self._trade_spot_product(
                product=self.VFE,
                order_depth=state.order_depths[self.VFE],
                position=state.position.get(self.VFE, 0),
                price_history=memory["spot_history"][self.VFE],
                base_fair=None,
                quote_size=self.VFE_QUOTE_SIZE,
                target_position=hedge_target_vfe,
                soft_cap=self.VFE_SOFT_CAP,
                mean_reversion_strength=0.15,
                imbalance_strength=0.55,
                take_edge_mult=0.95,
                flatten_aggression=1.80,
            )

        for product, strike in self.VOUCHERS.items():
            if product not in state.order_depths:
                continue
            orders = self._trade_voucher(
                product=product,
                strike=strike,
                order_depth=state.order_depths[product],
                position=state.position.get(product, 0),
                spot=voucher_context["spot"],
                sigma=voucher_context["fair_sigma"],
                time_years=voucher_context["time_years"],
            )
            if orders:
                result[product] = orders

        trader_data = json.dumps(memory, separators=(",", ":"))
        return result, 0, trader_data

    # ========================================================================
    # Memory helpers
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
        memory.setdefault("iv_history", [])

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        for product in [self.HYDROGEL, self.VFE]:
            if product not in state.order_depths:
                continue
            mid = mid_price(state.order_depths[product])
            if mid is None:
                continue

            hist = memory["spot_history"][product]
            hist.append(mid)
            if len(hist) > self.SPOT_HISTORY_WINDOW:
                del hist[:-self.SPOT_HISTORY_WINDOW]

    # ========================================================================
    # Spot trading
    # ========================================================================

    def _trade_spot_product(
        self,
        product: str,
        order_depth: OrderDepth,
        position: int,
        price_history: List[float],
        base_fair: Optional[float],
        quote_size: int,
        target_position: int,
        soft_cap: int,
        mean_reversion_strength: float,
        imbalance_strength: float,
        take_edge_mult: float,
        flatten_aggression: float,
    ) -> List[Order]:
        """Conservative market making with stronger inventory control.

        The key differences versus trader5:
        - we trade around a target position, not around "whatever happens"
        - we use a soft cap that is tighter than the exchange limit
        - if inventory drifts too far, flattening takes priority
        """
        orders: List[Order] = []
        exchange_limit = self.POSITION_LIMITS[product]

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        current_mid = 0.5 * (best_bid + best_ask)
        rolling_mean = statistics.mean(price_history) if price_history else current_mid
        rolling_std = statistics.pstdev(price_history) if len(price_history) >= 2 else 1.0
        rolling_std = max(rolling_std, 1.0)

        # Hydrogel gets a stable anchor. VFE uses rolling fair because its
        # center drifts a bit more.
        if base_fair is None:
            fair = rolling_mean
        else:
            fair = 0.7 * base_fair + 0.3 * rolling_mean

        z_score = (current_mid - rolling_mean) / rolling_std
        imbalance = top_level_imbalance(order_depth)

        adjusted_fair = fair - mean_reversion_strength * z_score + imbalance_strength * imbalance
        fair_int = int(round(adjusted_fair))

        # Inventory error is measured relative to a target, not just zero.
        inventory_error = position - target_position
        inventory_frac = inventory_error / max(soft_cap, 1)

        # We only take aggressive trades when the market is clearly away from
        # our fair. This is wider than trader5, especially for VFE.
        spread = best_ask - best_bid
        take_edge = max(1.0, take_edge_mult * spread)

        # Use a simulated position while building today's order list so we do
        # not accidentally exceed our intended soft cap.
        sim_pos = position
        long_cap = min(exchange_limit, target_position + soft_cap)
        short_cap = max(-exchange_limit, target_position - soft_cap)

        # If we are already too far long, do not aggressively buy more even if
        # the book looks cheap. Likewise for shorts.
        can_buy = sim_pos < long_cap
        can_sell = sim_pos > short_cap

        if can_buy:
            for ask_price in sorted(order_depth.sell_orders.keys()):
                if ask_price > adjusted_fair - take_edge:
                    break
                if sim_pos >= long_cap:
                    break
                ask_volume = -order_depth.sell_orders[ask_price]
                buy_qty = min(ask_volume, long_cap - sim_pos)
                if buy_qty > 0:
                    orders.append(Order(product, ask_price, buy_qty))
                    sim_pos += buy_qty

        if can_sell:
            for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
                if bid_price < adjusted_fair + take_edge:
                    break
                if sim_pos <= short_cap:
                    break
                bid_volume = order_depth.buy_orders[bid_price]
                sell_qty = min(bid_volume, sim_pos - short_cap)
                if sell_qty > 0:
                    orders.append(Order(product, bid_price, -sell_qty))
                    sim_pos -= sell_qty

        # Passive quotes:
        # - if we are too long relative to target, shift both quotes lower so
        #   the ask gets filled sooner and the bid becomes less appealing
        # - if too short, shift them higher
        quote_shift = int(round(flatten_aggression * inventory_frac))

        buy_quote = min(best_bid + 1, fair_int - 1 - quote_shift)
        sell_quote = max(best_ask - 1, fair_int + 1 - quote_shift)
        if sell_quote <= buy_quote:
            sell_quote = buy_quote + 1

        # If inventory is already heavily offside, stop quoting the wrong side.
        buy_capacity = max(0, long_cap - sim_pos)
        sell_capacity = max(0, sim_pos - short_cap)

        if inventory_frac > 0.70:
            buy_size = 0
            sell_size = min(sell_capacity, quote_size * 2)
        elif inventory_frac < -0.70:
            buy_size = min(buy_capacity, quote_size * 2)
            sell_size = 0
        else:
            buy_mult = max(0.0, 1.0 - inventory_frac)
            sell_mult = max(0.0, 1.0 + inventory_frac)
            buy_size = min(buy_capacity, int(round(quote_size * buy_mult)))
            sell_size = min(sell_capacity, int(round(quote_size * sell_mult)))

        if buy_size > 0:
            orders.append(Order(product, buy_quote, buy_size))
        if sell_size > 0:
            orders.append(Order(product, sell_quote, -sell_size))

        return orders

    # ========================================================================
    # Voucher context / option hedge
    # ========================================================================

    def _build_voucher_context(self, memory: dict, state: TradingState) -> dict:
        """Build shared option state: spot, TTE, fair sigma, hedge target."""
        spot = None
        if self.VFE in state.order_depths:
            spot = mid_price(state.order_depths[self.VFE])

        if spot is None:
            return {
                "spot": None,
                "time_years": 0.0,
                "fair_sigma": None,
                "hedge_target_vfe": 0,
            }

        time_days = max(0.05, self.START_TTE_DAYS - state.timestamp / self.TICKS_PER_DAY)
        time_years = time_days / 365.0

        current_ivs: List[float] = []
        for product in self.ACTIVE_VOUCHERS:
            if product not in state.order_depths:
                continue
            strike = self.VOUCHERS[product]
            opt_mid = mid_price(state.order_depths[product])
            if opt_mid is None:
                continue

            iv = implied_volatility(opt_mid, spot, strike, time_years)
            if iv is not None and 0.001 <= iv <= 0.50:
                current_ivs.append(iv)

        if current_ivs:
            sigma_now = statistics.mean(current_ivs)
            memory["iv_history"].append(sigma_now)
            if len(memory["iv_history"]) > self.IV_HISTORY_WINDOW:
                del memory["iv_history"][:-self.IV_HISTORY_WINDOW]
        elif memory["iv_history"]:
            sigma_now = memory["iv_history"][-1]
        else:
            sigma_now = 0.012

        fair_sigma = statistics.mean(memory["iv_history"]) if memory["iv_history"] else sigma_now

        # Partial hedge only.
        # Trader5's full hedge intent helped turn VFE into the main PnL sink.
        option_delta = 0.0
        for product in self.ACTIVE_VOUCHERS:
            strike = self.VOUCHERS[product]
            pos = state.position.get(product, 0)
            if pos == 0:
                continue
            option_delta += pos * bs_call_delta(spot, strike, time_years, fair_sigma)

        partial_hedge = -0.35 * option_delta
        hedge_target_vfe = int(round(max(-40, min(40, partial_hedge))))

        return {
            "spot": spot,
            "time_years": time_years,
            "fair_sigma": fair_sigma,
            "hedge_target_vfe": hedge_target_vfe,
        }

    # ========================================================================
    # Voucher trading
    # ========================================================================

    def _trade_voucher(
        self,
        product: str,
        strike: int,
        order_depth: OrderDepth,
        position: int,
        spot: Optional[float],
        sigma: Optional[float],
        time_years: float,
    ) -> List[Order]:
        """Selective, take-only voucher trading.

        This is intentionally much simpler than trader5:
        - no passive option market making
        - only middle strikes
        - only clear model-vs-market dislocations
        """
        orders: List[Order] = []

        if product not in self.ACTIVE_VOUCHERS:
            return orders
        if spot is None or sigma is None:
            return orders

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        fair_price = bs_call_price(spot, strike, time_years, sigma)
        spread = best_ask - best_bid

        # Make the edge threshold wider than trader5 so we only trade
        # stronger discrepancies.
        model_edge = max(2.0, 1.20 * spread)

        cap = min(self.POSITION_LIMITS[product], self.VOUCHER_SOFT_CAP)
        sim_pos = position

        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > fair_price - model_edge:
                break
            if sim_pos >= cap:
                break
            ask_volume = -order_depth.sell_orders[ask_price]
            buy_qty = min(ask_volume, cap - sim_pos)
            if buy_qty > 0:
                orders.append(Order(product, ask_price, buy_qty))
                sim_pos += buy_qty

        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price < fair_price + model_edge:
                break
            if sim_pos <= -cap:
                break
            bid_volume = order_depth.buy_orders[bid_price]
            sell_qty = min(bid_volume, sim_pos + cap)
            if sell_qty > 0:
                orders.append(Order(product, bid_price, -sell_qty))
                sim_pos -= sell_qty

        return orders
