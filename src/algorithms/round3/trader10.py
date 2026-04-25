"""
Round 3 trader, version 10.

This is a surgical fork of trader4, because trader4 / trader1 were the best
behaved versions so far. The goal is to preserve what was working:
  - HYDROGEL with static-fair Resin-style market making
  - VFE with popular-mid Kelp-style market making
  - simple, selective option trading with no hedge

What changes versus trader4:
  1. HYDROGEL flattening starts a bit earlier so inventory recycles sooner.
  2. VFE quote sizes are modestly smaller and skew a bit harder when stretched.
  3. Re-enable a *small* rolling-IV option sleeve on the most active strikes
     only: VEV_5300, VEV_5400, VEV_5500.
  4. Option position caps are kept small to avoid dominating the curve.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Optional
import math
import json


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def implied_vol(price: float, S: float, K: float, T: float) -> Optional[float]:
    intrinsic = max(S - K, 0.0)
    if price <= intrinsic + 1e-4 or price >= S:
        return None
    lo, hi = 1e-4, 3.0
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        if bs_call(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


def best_bid_ask(od: OrderDepth):
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def mid_of(od: OrderDepth):
    b, a = best_bid_ask(od)
    if b is None or a is None:
        return None
    return 0.5 * (b + a)


def popular_mid(od: OrderDepth):
    if not od.buy_orders or not od.sell_orders:
        return None
    pop_bid = max(od.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(od.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


class Trader:
    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"
    VOUCHERS = {
        "VEV_4000": 4000, "VEV_4500": 4500, "VEV_5000": 5000,
        "VEV_5100": 5100, "VEV_5200": 5200, "VEV_5300": 5300,
        "VEV_5400": 5400, "VEV_5500": 5500, "VEV_6000": 6000,
        "VEV_6500": 6500,
    }
    POS_LIMIT = {
        HYDROGEL: 200,
        VFE: 200,
        **{v: 300 for v in VOUCHERS},
    }

    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]
    VOL_VOUCHERS = ["VEV_5300", "VEV_5400", "VEV_5500"]

    HG_FAIR = 9990
    HG_QUOTE_SIZE = 24
    VFE_QUOTE_SIZE = 24

    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    IV_WINDOW = 50
    IV_WARMUP = 25
    VOUCHER_POS_CAP = 40
    VOUCHER_PRICE_EDGE = 2

    def bid(self):
        return 15

    def run(self, state: TradingState):
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}

        positions = state.position
        ticks_done = state.timestamp
        tte_days = max(0.01, self.TTE_DAYS_START - ticks_done / self.TICKS_PER_DAY)
        T = tte_days / 365.0

        result: Dict[str, List[Order]] = {}

        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._resin_strategy(
                self.HYDROGEL,
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
                fair=self.HG_FAIR,
                quote_size=self.HG_QUOTE_SIZE,
            )

        S = None
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            pop = popular_mid(od_vfe)
            S = mid_of(od_vfe)
            if pop is not None:
                result[self.VFE] = self._kelp_strategy(
                    self.VFE,
                    od_vfe,
                    positions.get(self.VFE, 0),
                    fair=pop,
                    quote_size=self.VFE_QUOTE_SIZE,
                )

        if S is not None:
            for prod in self.INTRINSIC_VOUCHERS:
                if prod in state.order_depths:
                    orders = self._intrinsic_arb(
                        prod, self.VOUCHERS[prod], S,
                        state.order_depths[prod],
                        positions.get(prod, 0),
                    )
                    if orders:
                        result[prod] = orders

            for prod in self.VOL_VOUCHERS:
                if prod not in state.order_depths:
                    continue
                orders = self._rolling_iv_strategy(
                    prod, self.VOUCHERS[prod], S, T,
                    state.order_depths[prod],
                    positions.get(prod, 0),
                    mem,
                )
                if orders:
                    result[prod] = orders

        return result, 0, json.dumps(mem)

    def _resin_strategy(self, prod: str, od: OrderDepth, pos: int,
                        fair: int, quote_size: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair:
                break
            if pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(prod, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair:
                break
            if pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(prod, bid, -qty))
                pos -= qty

        if pos > 0 and fair in od.buy_orders:
            # Flatten a little more aggressively than trader4.
            avail = od.buy_orders[fair]
            qty = min(avail, max(0, pos - 10))
            if qty > 0:
                orders.append(Order(prod, fair, -qty))
                pos -= qty

        if pos < 0 and fair in od.sell_orders:
            avail = -od.sell_orders[fair]
            qty = min(avail, max(0, -pos - 10))
            if qty > 0:
                orders.append(Order(prod, fair, qty))
                pos += qty

        cur_bid, cur_ask = best_bid_ask(od)
        if cur_bid is not None and cur_bid < fair - 1:
            our_bid = cur_bid + 1
        else:
            our_bid = fair - 1
        if cur_ask is not None and cur_ask > fair + 1:
            our_ask = cur_ask - 1
        else:
            our_ask = fair + 1

        bid_cap = fair - 1
        ask_floor = fair + 1
        # Start relaxing earlier than trader4 to flatten sooner.
        if pos > limit * 0.35:
            ask_floor = fair
        if pos > limit * 0.65:
            ask_floor = fair - 1
        if pos < -limit * 0.35:
            bid_cap = fair
        if pos < -limit * 0.65:
            bid_cap = fair + 1

        our_bid = min(our_bid, bid_cap)
        our_ask = max(our_ask, ask_floor)
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        # Slightly smaller wrong-way quote when inventory is stretched.
        pos_frac = pos / limit
        bid_mult = max(0.15, 1.0 - pos_frac * 1.0)
        ask_mult = max(0.15, 1.0 + pos_frac * 1.0)
        bid_size = min(buy_capacity, int(quote_size * bid_mult))
        ask_size = min(sell_capacity, int(quote_size * ask_mult))
        if bid_size > 0:
            orders.append(Order(prod, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(prod, our_ask, -ask_size))
        return orders

    def _kelp_strategy(self, prod: str, od: OrderDepth, pos: int,
                       fair: float, quote_size: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair:
                break
            if pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(prod, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair:
                break
            if pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(prod, bid, -qty))
                pos -= qty

        cur_bid, cur_ask = best_bid_ask(od)
        fair_int = int(round(fair))
        if cur_bid is not None and cur_bid < fair_int - 1:
            our_bid = cur_bid + 1
        else:
            our_bid = fair_int - 1
        if cur_ask is not None and cur_ask > fair_int + 1:
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
        bid_mult = max(0.15, 1.0 - pos_frac * 1.0)
        ask_mult = max(0.15, 1.0 + pos_frac * 1.0)
        bid_size = min(buy_capacity, int(quote_size * bid_mult))
        ask_size = min(sell_capacity, int(quote_size * ask_mult))
        if bid_size > 0:
            orders.append(Order(prod, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(prod, our_ask, -ask_size))
        return orders

    def _intrinsic_arb(self, prod: str, K: int, S: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        limit = min(self.POS_LIMIT[prod], 60)
        floor = S - K
        for ask in sorted(od.sell_orders.keys()):
            if ask >= floor - 1 or pos >= limit:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, limit - pos)
            if qty > 0:
                orders.append(Order(prod, ask, qty))
                pos += qty
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= floor + 5 or pos <= -limit:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + limit)
            if qty > 0:
                orders.append(Order(prod, bid, -qty))
                pos -= qty
        return orders

    def _rolling_iv_strategy(self, prod: str, K: int, S: float, T: float,
                             od: OrderDepth, pos: int, mem: dict) -> List[Order]:
        orders: List[Order] = []
        cap = self.VOUCHER_POS_CAP

        m_px = mid_of(od)
        if m_px is None:
            return orders
        iv_now = implied_vol(m_px, S, K, T)
        if iv_now is None or iv_now < 0.05 or iv_now > 1.5:
            return orders

        win_key = f"iv_win_{prod}"
        window = mem.get(win_key, [])
        window.append(iv_now)
        if len(window) > self.IV_WINDOW:
            window = window[-self.IV_WINDOW:]
        mem[win_key] = window

        if len(window) < self.IV_WARMUP:
            return orders

        fair_iv = sum(window) / len(window)
        fair_price = bs_call(S, K, T, fair_iv)

        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair_price - self.VOUCHER_PRICE_EDGE:
                break
            if pos >= cap:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, cap - pos)
            if qty > 0:
                orders.append(Order(prod, ask, qty))
                pos += qty

        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair_price + self.VOUCHER_PRICE_EDGE:
                break
            if pos <= -cap:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + cap)
            if qty > 0:
                orders.append(Order(prod, bid, -qty))
                pos -= qty

        return orders
