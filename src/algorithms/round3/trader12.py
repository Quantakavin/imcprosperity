"""
Round 3 trader, version 12.

This version is built from the best parts of trader4 plus the missing ideas we
identified afterward:

1. HYDROGEL_PACK
   - still Resin-style market making around a stable anchor
   - but the fair value can shift a little with recent price action so we do
     not blindly fight persistent moves

2. VELVETFRUIT_EXTRACT
   - simple Kelp-style popular-mid market making
   - small role, mainly for smoother support PnL

3. Vouchers
   - real smile-based option strategy again
   - Black-Scholes pricing
   - implied volatility per strike
   - fit a simple quadratic smile across active strikes
   - trade strikes that are rich/cheap versus the fitted curve
   - no hedge and no passive option quoting

This is the first version meant to combine:
  - a real spot edge
  - a real option edge
  - enough inventory control to avoid the worst curve blowups
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math


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
# Book helpers
# ============================================================================

def best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def mid_of(od: OrderDepth) -> Optional[float]:
    bid, ask = best_bid_ask(od)
    if bid is None or ask is None:
        return None
    return 0.5 * (bid + ask)


def popular_mid(od: OrderDepth) -> Optional[float]:
    if not od.buy_orders or not od.sell_orders:
        return mid_of(od)
    pop_bid = max(od.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(od.sell_orders.items(), key=lambda x: -x[1])[0]
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

    ACTIVE_VOUCHERS = ["VEV_5000", "VEV_5100", "VEV_5200", "VEV_5300", "VEV_5400", "VEV_5500"]
    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]

    HG_BASE_FAIR = 9990.0
    HG_QUOTE_SIZE = 26
    VFE_QUOTE_SIZE = 22

    HG_HISTORY_WINDOW = 40
    VFE_HISTORY_WINDOW = 30

    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    VOUCHER_POS_CAP = 60
    SMILE_EDGE = 0.010
    PRICE_EDGE = 2.0

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

            smile_orders = self._trade_smile_options(state, spot, time_years, positions)
            result.update(smile_orders)

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

    def _hydrogel_fair(self, memory: dict) -> float:
        """Hydrogel fair = static anchor + mild dynamic adjustment.

        We do not want the full laggy EMA behavior that hurt earlier bots.
        But we also do not want to keep treating 9990 as sacred if recent price
        has clearly shifted for a while.
        """
        hist = memory["hg_mid_hist"]
        if not hist:
            return self.HG_BASE_FAIR

        recent = hist[-12:] if len(hist) >= 12 else hist
        recent_mean = sum(recent) / len(recent)

        # Move only part-way toward recent price.
        return 0.7 * self.HG_BASE_FAIR + 0.3 * recent_mean

    def _trade_hydrogel(self, od: OrderDepth, pos: int, memory: dict) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[self.HYDROGEL]
        fair = self._hydrogel_fair(memory)
        fair_int = int(round(fair))

        # TAKE obvious gifts.
        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair_int:
                break
            if pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair_int:
                break
            if pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(self.HYDROGEL, bid, -qty))
                pos -= qty

        # Flatten around fair if inventory gets large.
        if pos > 0 and fair_int in od.buy_orders:
            avail = od.buy_orders[fair_int]
            qty = min(avail, max(0, pos - 15))
            if qty > 0:
                orders.append(Order(self.HYDROGEL, fair_int, -qty))
                pos -= qty
        if pos < 0 and fair_int in od.sell_orders:
            avail = -od.sell_orders[fair_int]
            qty = min(avail, max(0, -pos - 15))
            if qty > 0:
                orders.append(Order(self.HYDROGEL, fair_int, qty))
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
        if pos > limit * 0.4:
            ask_floor = fair_int
        if pos > limit * 0.7:
            ask_floor = fair_int - 1
        if pos < -limit * 0.4:
            bid_cap = fair_int
        if pos < -limit * 0.7:
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
        bid_size = min(buy_capacity, int(self.HG_QUOTE_SIZE * bid_mult))
        ask_size = min(sell_capacity, int(self.HG_QUOTE_SIZE * ask_mult))
        if bid_size > 0:
            orders.append(Order(self.HYDROGEL, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(self.HYDROGEL, our_ask, -ask_size))
        return orders

    # ========================================================================
    # VFE
    # ========================================================================

    def _trade_vfe(self, od: OrderDepth, pos: int, fair: float) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[self.VFE]

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
        fair_int = int(round(fair))

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

    def _fit_parabola(self, xs: List[float], ys: List[float]) -> Optional[Tuple[float, float, float]]:
        """Fit y = a*x^2 + b*x + c by least squares using normal equations."""
        n = len(xs)
        if n < 3:
            return None

        s1 = n
        sx = sum(xs)
        sx2 = sum(x * x for x in xs)
        sx3 = sum(x * x * x for x in xs)
        sx4 = sum(x * x * x * x for x in xs)
        sy = sum(ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        sx2y = sum((x * x) * y for x, y in zip(xs, ys))

        # Solve 3x3 system with Cramer's rule / explicit determinant.
        det = (
            sx4 * (sx2 * s1 - sx * sx)
            - sx3 * (sx3 * s1 - sx * sx2)
            + sx2 * (sx3 * sx - sx2 * sx2)
        )
        if abs(det) < 1e-12:
            return None

        det_a = (
            sx2y * (sx2 * s1 - sx * sx)
            - sxy * (sx3 * s1 - sx * sx2)
            + sy * (sx3 * sx - sx2 * sx2)
        )
        det_b = (
            sx4 * (sxy * s1 - sy * sx)
            - sx3 * (sx2y * s1 - sy * sx2)
            + sx2 * (sx2y * sx - sxy * sx2)
        )
        det_c = (
            sx4 * (sx2 * sy - sx * sxy)
            - sx3 * (sx3 * sy - sx * sx2y)
            + sx2 * (sx3 * sxy - sx2 * sx2y)
        )

        return det_a / det, det_b / det, det_c / det

    def _trade_smile_options(self, state: TradingState, spot: float, time_years: float,
                             positions: Dict[str, int]) -> Dict[str, List[Order]]:
        result: Dict[str, List[Order]] = {}

        xs: List[float] = []
        ys: List[float] = []
        info = {}

        for product in self.ACTIVE_VOUCHERS:
            if product not in state.order_depths:
                continue
            od = state.order_depths[product]
            mid = mid_of(od)
            if mid is None:
                continue
            strike = self.VOUCHERS[product]
            iv = implied_vol(mid, spot, strike, time_years)
            if iv is None or iv < 0.05 or iv > 1.5:
                continue
            x = math.log(strike / spot) / max(math.sqrt(time_years), 1e-6)
            xs.append(x)
            ys.append(iv)
            info[product] = (x, iv, strike, od)

        fit = self._fit_parabola(xs, ys)
        if fit is None:
            return result

        a, b, c = fit

        for product, (x, iv_now, strike, od) in info.items():
            fair_iv = a * x * x + b * x + c
            iv_gap = iv_now - fair_iv
            fair_price = bs_call(spot, strike, time_years, max(0.01, fair_iv))
            pos = positions.get(product, 0)
            cap = self.VOUCHER_POS_CAP
            orders: List[Order] = []

            # Option is cheap vs smile: buy asks.
            if iv_gap <= -self.SMILE_EDGE:
                for ask in sorted(od.sell_orders.keys()):
                    if ask >= fair_price - self.PRICE_EDGE:
                        break
                    if pos >= cap:
                        break
                    avail = -od.sell_orders[ask]
                    qty = min(avail, cap - pos)
                    if qty > 0:
                        orders.append(Order(product, ask, qty))
                        pos += qty

            # Option is rich vs smile: sell bids.
            if iv_gap >= self.SMILE_EDGE:
                for bid in sorted(od.buy_orders.keys(), reverse=True):
                    if bid <= fair_price + self.PRICE_EDGE:
                        break
                    if pos <= -cap:
                        break
                    avail = od.buy_orders[bid]
                    qty = min(avail, pos + cap)
                    if qty > 0:
                        orders.append(Order(product, bid, -qty))
                        pos -= qty

            if orders:
                result[product] = orders

        return result
