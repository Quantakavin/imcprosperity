"""
Round 3 trader, version 11.

This version implements the agreed design directly:

1. HYDROGEL_PACK
   - main engine
   - market making around a fair anchor near 9990
   - but with a simple regime filter so we do not blindly keep fading a move

2. VELVETFRUIT_EXTRACT
   - simple Kelp-style market maker
   - fair comes from the current book ("popular mid")
   - small inventory, small directional risk

3. Vouchers
   - Black-Scholes based
   - compute implied volatility per strike
   - maintain rolling IV history per strike
   - trade only a small number of active strikes when price is meaningfully
     away from a fair price implied by the rolling IV mean
   - no hedging and no passive option market making

The goal is to preserve the simple, reactive structure that made trader4 the
best version so far, while adding the missing Hydrogel regime awareness and a
small real option sleeve.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math
import statistics


# ============================================================================
# Black-Scholes helpers
# ============================================================================

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(spot: float, strike: float, time_years: float, sigma: float) -> float:
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0)
    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * norm_cdf(d1) - strike * norm_cdf(d2)


def implied_vol(price: float, spot: float, strike: float, time_years: float) -> Optional[float]:
    intrinsic = max(spot - strike, 0.0)
    if price <= intrinsic + 1e-4 or price >= spot or spot <= 0:
        return None

    low = 1e-4
    high = 3.0
    for _ in range(45):
        mid = 0.5 * (low + high)
        if bs_call(spot, strike, time_years, mid) > price:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


# ============================================================================
# Order book helpers
# ============================================================================

def best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_of(order_depth: OrderDepth) -> Optional[float]:
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def popular_mid(order_depth: OrderDepth) -> Optional[float]:
    if not order_depth.buy_orders or not order_depth.sell_orders:
        return mid_of(order_depth)
    pop_bid = max(order_depth.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(order_depth.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


class Trader:
    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"

    VOUCHERS = {
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

    POS_LIMIT = {
        HYDROGEL: 200,
        VFE: 200,
        **{product: 300 for product in VOUCHERS},
    }

    HG_FAIR = 9990
    HG_QUOTE_SIZE = 24
    VFE_QUOTE_SIZE = 22

    # Hydrogel regime filter settings.
    HG_HISTORY_WINDOW = 80
    HG_REGIME_THRESHOLD = 10.0
    HG_REGIME_CONFIRM = 18

    # VFE fair smoothing.
    VFE_HISTORY_WINDOW = 40

    # Option settings.
    ACTIVE_VOUCHERS = ["VEV_5300", "VEV_5400", "VEV_5500"]
    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]
    IV_WINDOW = 50
    IV_WARMUP = 25
    VOUCHER_POS_CAP = 35
    VOUCHER_PRICE_EDGE = 2.0

    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    def bid(self):
        return 15

    def run(self, state: TradingState):
        memory = self._load_memory(state.traderData)
        self._ensure_memory(memory)
        self._update_histories(memory, state)

        positions = state.position
        result: Dict[str, List[Order]] = {}

        time_days = max(0.01, self.TTE_DAYS_START - state.timestamp / self.TICKS_PER_DAY)
        time_years = time_days / 365.0

        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._trade_hydrogel(
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
                memory,
            )

        spot = None
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            spot = mid_of(od_vfe)
            fair_vfe = popular_mid(od_vfe)
            if fair_vfe is not None:
                result[self.VFE] = self._trade_vfe(
                    od_vfe,
                    positions.get(self.VFE, 0),
                    fair_vfe,
                )

        if spot is not None:
            for product in self.INTRINSIC_VOUCHERS:
                if product in state.order_depths:
                    orders = self._intrinsic_arb(
                        product,
                        self.VOUCHERS[product],
                        spot,
                        state.order_depths[product],
                        positions.get(product, 0),
                    )
                    if orders:
                        result[product] = orders

            for product in self.ACTIVE_VOUCHERS:
                if product not in state.order_depths:
                    continue
                orders = self._rolling_iv_strategy(
                    product,
                    self.VOUCHERS[product],
                    spot,
                    time_years,
                    state.order_depths[product],
                    positions.get(product, 0),
                    memory,
                )
                if orders:
                    result[product] = orders

        return result, 0, json.dumps(memory, separators=(",", ":"))

    # ========================================================================
    # Memory
    # ========================================================================

    def _load_memory(self, raw: str) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _ensure_memory(self, memory: dict) -> None:
        memory.setdefault("hg_mid_hist", [])
        memory.setdefault("vfe_mid_hist", [])
        memory.setdefault("iv_windows", {})
        for product in self.ACTIVE_VOUCHERS:
            memory["iv_windows"].setdefault(product, [])

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        if self.HYDROGEL in state.order_depths:
            mid = mid_of(state.order_depths[self.HYDROGEL])
            if mid is not None:
                memory["hg_mid_hist"].append(mid)
                if len(memory["hg_mid_hist"]) > self.HG_HISTORY_WINDOW:
                    memory["hg_mid_hist"] = memory["hg_mid_hist"][-self.HG_HISTORY_WINDOW:]

        if self.VFE in state.order_depths:
            mid = mid_of(state.order_depths[self.VFE])
            if mid is not None:
                memory["vfe_mid_hist"].append(mid)
                if len(memory["vfe_mid_hist"]) > self.VFE_HISTORY_WINDOW:
                    memory["vfe_mid_hist"] = memory["vfe_mid_hist"][-self.VFE_HISTORY_WINDOW:]

    # ========================================================================
    # Hydrogel
    # ========================================================================

    def _hydrogel_regime_fair(self, memory: dict) -> Tuple[float, str]:
        """Return an effective Hydrogel fair and a small regime label.

        Normal mode:
          fair is near 9990.

        Shifted-anchor down mode:
          if Hydrogel spends enough time well below 9990, move fair lower and
          stop treating every low price as "free money".

        Shifted-anchor up mode is symmetric.
        """
        hist = memory["hg_mid_hist"]
        if len(hist) < self.HG_REGIME_CONFIRM:
            return float(self.HG_FAIR), "normal"

        recent = hist[-self.HG_REGIME_CONFIRM:]
        avg_recent = sum(recent) / len(recent)

        if avg_recent <= self.HG_FAIR - self.HG_REGIME_THRESHOLD:
            # Only partially shift the anchor. We still want some connection to
            # the long-run fair, but not enough to keep blindly buying dips.
            fair = 0.5 * self.HG_FAIR + 0.5 * avg_recent
            return fair, "down"

        if avg_recent >= self.HG_FAIR + self.HG_REGIME_THRESHOLD:
            fair = 0.5 * self.HG_FAIR + 0.5 * avg_recent
            return fair, "up"

        return float(self.HG_FAIR), "normal"

    def _trade_hydrogel(self, od: OrderDepth, pos: int, memory: dict) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[self.HYDROGEL]

        fair, regime = self._hydrogel_regime_fair(memory)
        fair_int = int(round(fair))

        # TAKE logic:
        # In normal mode we fade around fair.
        # In down regime, be less eager to buy below fair.
        # In up regime, be less eager to sell above fair.
        buy_threshold = fair_int
        sell_threshold = fair_int
        if regime == "down":
            buy_threshold = fair_int - 3
        elif regime == "up":
            sell_threshold = fair_int + 3

        for ask in sorted(od.sell_orders.keys()):
            if ask >= buy_threshold:
                break
            if pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= sell_threshold:
                break
            if pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, bid, -qty))
                pos -= qty

        # Flattening:
        # If we are long and the regime is down, flatten even more aggressively.
        if pos > 0:
            if regime == "down":
                flatten_price = fair_int - 1
            else:
                flatten_price = fair_int
            if flatten_price in od.buy_orders:
                avail = od.buy_orders[flatten_price]
                qty = min(avail, max(0, pos - 5))
                if qty > 0:
                    orders.append(Order(self.HYDROGEL, flatten_price, -qty))
                    pos -= qty

        if pos < 0:
            if regime == "up":
                flatten_price = fair_int + 1
            else:
                flatten_price = fair_int
            if flatten_price in od.sell_orders:
                avail = -od.sell_orders[flatten_price]
                qty = min(avail, max(0, -pos - 5))
                if qty > 0:
                    orders.append(Order(self.HYDROGEL, flatten_price, qty))
                    pos += qty

        cur_bid, cur_ask = best_bid_ask(od)
        if cur_bid is None or cur_ask is None:
            return orders

        if cur_bid < fair_int - 1:
            our_bid = cur_bid + 1
        else:
            our_bid = fair_int - 1
        if cur_ask > fair_int + 1:
            our_ask = cur_ask - 1
        else:
            our_ask = fair_int + 1

        bid_cap = fair_int - 1
        ask_floor = fair_int + 1

        # Inventory-aware quoting like trader4, but slightly earlier flattening.
        if pos > limit * 0.35:
            ask_floor = fair_int
        if pos > limit * 0.65:
            ask_floor = fair_int - 1
        if pos < -limit * 0.35:
            bid_cap = fair_int
        if pos < -limit * 0.65:
            bid_cap = fair_int + 1

        # Regime-aware extra caution:
        # if down regime and already long, make bids less aggressive.
        if regime == "down" and pos > 0:
            bid_cap = min(bid_cap, fair_int - 2)
        if regime == "up" and pos < 0:
            ask_floor = max(ask_floor, fair_int + 2)

        our_bid = min(our_bid, bid_cap)
        our_ask = max(our_ask, ask_floor)
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        pos_frac = pos / limit
        bid_mult = max(0.15, 1.0 - pos_frac)
        ask_mult = max(0.15, 1.0 + pos_frac)
        bid_size = min(buy_capacity, int(self.HG_QUOTE_SIZE * bid_mult))
        ask_size = min(sell_capacity, int(self.HG_QUOTE_SIZE * ask_mult))

        if bid_size > 0:
            orders.append(Order(self.HYDROGEL, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(self.HYDROGEL, our_ask, -ask_size))

        return orders

    # ========================================================================
    # Velvetfruit
    # ========================================================================

    def _trade_vfe(self, od: OrderDepth, pos: int, fair: float) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[self.VFE]
        fair_int = int(round(fair))

        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair:
                break
            if pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(self.VFE, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair:
                break
            if pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(self.VFE, bid, -qty))
                pos -= qty

        cur_bid, cur_ask = best_bid_ask(od)
        if cur_bid is None or cur_ask is None:
            return orders

        if cur_bid < fair_int - 1:
            our_bid = cur_bid + 1
        else:
            our_bid = fair_int - 1
        if cur_ask > fair_int + 1:
            our_ask = cur_ask - 1
        else:
            our_ask = fair_int + 1

        bid_cap = fair_int - 1
        ask_floor = fair_int + 1
        if pos > limit * 0.35:
            ask_floor = fair_int
        if pos > limit * 0.65:
            ask_floor = fair_int - 1
        if pos < -limit * 0.35:
            bid_cap = fair_int
        if pos < -limit * 0.65:
            bid_cap = fair_int + 1

        our_bid = min(our_bid, bid_cap)
        our_ask = max(our_ask, ask_floor)
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        pos_frac = pos / limit
        bid_mult = max(0.15, 1.0 - pos_frac)
        ask_mult = max(0.15, 1.0 + pos_frac)
        bid_size = min(buy_capacity, int(self.VFE_QUOTE_SIZE * bid_mult))
        ask_size = min(sell_capacity, int(self.VFE_QUOTE_SIZE * ask_mult))
        if bid_size > 0:
            orders.append(Order(self.VFE, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(self.VFE, our_ask, -ask_size))
        return orders

    # ========================================================================
    # Options
    # ========================================================================

    def _intrinsic_arb(self, product: str, strike: int, spot: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        limit = min(self.POS_LIMIT[product], 60)
        floor = spot - strike
        for ask in sorted(od.sell_orders.keys()):
            if ask >= floor - 1 or pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(product, ask, qty))
                pos += qty
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= floor + 5 or pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(product, bid, -qty))
                pos -= qty
        return orders

    def _rolling_iv_strategy(self, product: str, strike: int, spot: float, time_years: float,
                             od: OrderDepth, pos: int, memory: dict) -> List[Order]:
        orders: List[Order] = []
        cap = self.VOUCHER_POS_CAP
        mid = mid_of(od)
        if mid is None:
            return orders

        iv_now = implied_vol(mid, spot, strike, time_years)
        if iv_now is None or iv_now < 0.05 or iv_now > 1.5:
            return orders

        window = memory["iv_windows"].get(product, [])
        window.append(iv_now)
        if len(window) > self.IV_WINDOW:
            window = window[-self.IV_WINDOW:]
        memory["iv_windows"][product] = window

        if len(window) < self.IV_WARMUP:
            return orders

        fair_iv = sum(window) / len(window)
        fair_price = bs_call(spot, strike, time_years, fair_iv)

        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair_price - self.VOUCHER_PRICE_EDGE:
                break
            if pos >= cap:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, cap - pos)
            if qty > 0:
                orders.append(Order(product, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair_price + self.VOUCHER_PRICE_EDGE:
                break
            if pos <= -cap:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + cap)
            if qty > 0:
                orders.append(Order(product, bid, -qty))
                pos -= qty

        return orders
