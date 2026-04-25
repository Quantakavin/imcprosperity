"""
================================================================================
IMC PROSPERITY 4 — ROUND 3: "GLOVES OFF"  (v2 — fixes from log analysis)
================================================================================

CHANGES FROM v1 (and WHY):

  HYDROGEL_PACK
    v1 used a fixed fair = 9990. Live mean was 9979 (10 ticks below). Result:
    we kept selling at 9992 above the actual market, drifted to -193 short.
    Made +£8376 by luck (price came back) but very unstable.
    v2: use a slow rolling EMA of the mid as our fair, like we already do for
    VFE. Adapts to whatever the actual mean is. Same takes, same makes.

  VELVETFRUIT_EXTRACT
    v1 was fine (+£2213). Slight tweak: tighter passive band on a low-vol
    product to make more spread. Otherwise unchanged.

  VOUCHER SMILE-ARB (the big one)
    v1 fit a parabola to (moneyness, IV) live and traded deviations from it.
    Problem: VEV_5400 sits STRUCTURALLY ~2 vol points below its neighbours,
    every tick, both historically and live. The parabola tried to smooth
    this out and kept yelling "BUY 5400". We bought 300 contracts (max
    position) at avg 17.26, market was 16, lost £349.
    Symmetric problem on VEV_5300 (sat above the curve, sold to -63, lost £128).

    v2: drop the parabola entirely. For each voucher, we know its TYPICAL IV
    from history (median over 3 days x 10k ticks). Trade only when the live
    IV deviates from THAT STRIKE'S OWN baseline by more than 1.5 standard
    deviations. The 5400 smirk gets baked into its baseline (0.2223), so a
    live IV of 0.249 (within 4 std) doesn't trigger a buy.

    Plus: bias-correct the absolute level. Live IVs were ~3 vol points
    HIGHER than historical (different vol regime in round 3). We compute
    a "regime shift" each tick = mean(current IV - baseline IV across
    strikes) and subtract it before z-scoring. So we trade only on
    RELATIVE strike-vs-strike mispricings, not the whole curve being shifted.

  POSITION CAPS
    Cap voucher net position at 100 (not 300) per strike. The smile-arb
    edge per trade is small; sizing into the full 300 in one direction
    means one wrong call destroys the round. Smaller positions = more
    rebalancing = more realised PnL.

================================================================================
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math
import json


# =============================================================================
# SECTION 1: BLACK-SCHOLES MATH (unchanged from v1)
# =============================================================================

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def implied_vol(price: float, S: float, K: float, T: float) -> float:
    """Bisection IV solver. Returns None if no valid IV."""
    intrinsic = max(S - K, 0.0)
    if price <= intrinsic + 1e-4 or price >= S:
        return None
    lo, hi = 1e-4, 3.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if bs_call(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


# =============================================================================
# SECTION 2: ORDER BOOK HELPERS (unchanged)
# =============================================================================

def best_bid_ask(od: OrderDepth):
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def mid_price(od: OrderDepth):
    b, a = best_bid_ask(od)
    if b is None or a is None:
        return None
    return 0.5 * (b + a)


# =============================================================================
# SECTION 3: TRADER CLASS
# =============================================================================

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
    LIVE_VOUCHERS = ["VEV_5000", "VEV_5100", "VEV_5200",
                     "VEV_5300", "VEV_5400", "VEV_5500"]
    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]

    # ---- TTE config ----
    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    # ---- Voucher strategy config (NEW IN v2) ----
    # Calibrated from 3 days of historical data (day 1 / TTE=7d).
    # These are the median IV per strike — the strike's "typical" vol level.
    BASELINE_IV = {
        "VEV_5000": 0.2309,
        "VEV_5100": 0.2269,
        "VEV_5200": 0.2363,
        "VEV_5300": 0.2388,
        "VEV_5400": 0.2223,  # the structural smirk dip
        "VEV_5500": 0.2398,
    }
    # Per-strike historical std of IV (also from 3 days of data)
    BASELINE_IV_STD = {
        "VEV_5000": 0.0093,
        "VEV_5100": 0.0076,
        "VEV_5200": 0.0061,
        "VEV_5300": 0.0031,
        "VEV_5400": 0.0066,
        "VEV_5500": 0.0071,
    }
    # Trade only when |z-score| > IV_Z_ENTRY (z = (iv - baseline) / std)
    IV_Z_ENTRY = 1.5
    IV_Z_EXIT = 0.5

    # Cap voucher position at this many contracts (well below 300 limit).
    # Edge per trade is small; bigger positions just bleed if wrong.
    VOUCHER_POS_CAP = 100
    VOUCHER_TRADE_SIZE = 15  # max contracts per single tick

    # ---- HYDROGEL config (NEW: dynamic fair, not fixed 9990) ----
    HYDROGEL_HALF_SPREAD = 2

    # ---- VFE config ----
    VFE_HALF_SPREAD = 2

    def bid(self):
        return 15

    # =========================================================================
    # run() — main entrypoint
    # =========================================================================
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

        # ---- HYDROGEL: now uses dynamic fair (rolling EMA, like VFE) ----
        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._mm_dynamic(
                self.HYDROGEL,
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
                mem, "hg_fair", self.HYDROGEL_HALF_SPREAD,
            )

        # ---- VFE ----
        S = None
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            S = mid_price(od_vfe)
            result[self.VFE] = self._mm_dynamic(
                self.VFE, od_vfe,
                positions.get(self.VFE, 0),
                mem, "vfe_fair", self.VFE_HALF_SPREAD,
            )

        # ---- Vouchers (need S to value them) ----
        if S is not None:
            # 3a. Intrinsic-floor arb on deep ITM
            for prod in self.INTRINSIC_VOUCHERS:
                if prod in state.order_depths:
                    orders = self._intrinsic_arb(
                        prod, self.VOUCHERS[prod], S,
                        state.order_depths[prod],
                        positions.get(prod, 0),
                    )
                    if orders:
                        result[prod] = orders

            # 3b. Z-SCORE smile arb (replaces parabola-fit from v1)
            self._smile_arb_zscore(state, S, T, positions, result)

        return result, 0, json.dumps(mem)

    # =========================================================================
    # MARKET MAKING with DYNAMIC fair (used for both HYDROGEL and VFE)
    # =========================================================================
    def _mm_dynamic(self, prod: str, od: OrderDepth, pos: int,
                    mem: dict, fair_key: str, half_spread: int) -> List[Order]:
        """Market-make around a slow EMA of the mid price.

        Why dynamic fair (vs hardcoded 9990 like v1)?
          - HYDROGEL's actual mean was 9979 in this run, not 9990.
          - A fixed wrong fair = systematic one-sided fills = drift.
          - EMA adapts in seconds and stays close to actual mean.
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        m = mid_price(od)
        if m is None:
            return orders

        # Update EMA (slow alpha = stable fair, doesn't chase noise)
        prev = mem.get(fair_key, m)
        alpha = 0.02 if prod == self.HYDROGEL else 0.05
        fair = alpha * m + (1 - alpha) * prev
        mem[fair_key] = fair

        # ---- TAKE: hit anything clearly mispriced vs fair ----
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < fair - 0.5 and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fair + 0.5 and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty

        # ---- MAKE: passive quotes either side of fair ----
        # SKEW the quotes by current position to encourage rebalancing:
        #   if we're long, lean the bid down (less likely to add inventory)
        #     and lean the ask down (more likely to dump inventory)
        skew = -pos / limit  # in [-1, +1]
        bid_px = int(math.floor(fair - half_spread + skew))
        ask_px = int(math.ceil(fair + half_spread + skew))
        # Make sure we don't cross
        if ask_px <= bid_px:
            ask_px = bid_px + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        if buy_capacity > 0:
            orders.append(Order(prod, bid_px, min(buy_capacity, 30)))
        if sell_capacity > 0:
            orders.append(Order(prod, ask_px, -min(sell_capacity, 30)))
        return orders

    # =========================================================================
    # INTRINSIC-FLOOR ARB on deep ITM vouchers (unchanged)
    # =========================================================================
    def _intrinsic_arb(self, prod: str, K: int, S: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]
        floor = S - K
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < floor - 1 and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > floor + 5 and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty
        return orders

    # =========================================================================
    # Z-SCORE SMILE ARB (NEW IN v2 — replaces parabola fit)
    # =========================================================================
    def _smile_arb_zscore(self, state, S: float, T: float,
                          positions: dict, result: dict):
        """For each live voucher, compute IV and compare to its own
        historical baseline (with regime-shift correction).

        Two corrections:
          1) BASELINE per strike: VEV_5400's typical IV is 0.2223, not "the
             curve at m=0.21". Each strike has its own characteristic level.
          2) REGIME SHIFT: in this round, all IVs are ~3 vol points higher
             than historical. We compute a real-time "shift" = average
             (live_iv - baseline_iv) across strikes, and subtract it before
             z-scoring. So we only trade RELATIVE strike-vs-strike outliers.
        """
        # Step 1: compute IV for every live voucher this tick
        live_ivs = {}  # {prod: iv}
        for prod in self.LIVE_VOUCHERS:
            if prod not in state.order_depths:
                continue
            m_px = mid_price(state.order_depths[prod])
            if m_px is None:
                continue
            K = self.VOUCHERS[prod]
            iv = implied_vol(m_px, S, K, T)
            if iv is None or iv < 0.05 or iv > 1.5:
                continue
            live_ivs[prod] = iv

        if len(live_ivs) < 4:
            return  # not enough data to compute regime shift reliably

        # Step 2: regime shift (mean of live - baseline across strikes)
        # If average live IV is 0.027 above historical, treat that as the
        # "new normal" and don't trade on it.
        diffs = [live_ivs[p] - self.BASELINE_IV[p] for p in live_ivs]
        regime_shift = sum(diffs) / len(diffs)

        # Step 3: per-strike z-score, decide trade
        for prod, iv in live_ivs.items():
            adjusted = iv - regime_shift  # back to historical scale
            baseline = self.BASELINE_IV[prod]
            std = self.BASELINE_IV_STD[prod]
            z = (adjusted - baseline) / std

            pos = positions.get(prod, 0)
            orders = self._zscore_trade(
                prod, z, state.order_depths[prod], pos,
            )
            if orders:
                result[prod] = orders

    def _zscore_trade(self, prod: str, z: float,
                      od: OrderDepth, pos: int) -> List[Order]:
        """
            z > +Z_ENTRY  -> too rich  -> SELL
            z < -Z_ENTRY  -> too cheap -> BUY
            |z| < Z_EXIT  -> close inventory toward zero
        """
        orders: List[Order] = []
        cap = self.VOUCHER_POS_CAP   # tighter than 300 limit by design
        bid, ask = best_bid_ask(od)
        if bid is None or ask is None:
            return orders

        size = self.VOUCHER_TRADE_SIZE

        if z > self.IV_Z_ENTRY:
            # Too rich — sell at bid, but only if not already at cap short
            if pos > -cap:
                avail = od.buy_orders.get(bid, 0)
                qty = min(avail, pos + cap, size)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))

        elif z < -self.IV_Z_ENTRY:
            # Too cheap — buy at ask, capped
            if pos < cap:
                avail = -od.sell_orders.get(ask, 0)
                qty = min(avail, cap - pos, size)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))

        elif abs(z) < self.IV_Z_EXIT:
            # Reverted — close out
            if pos > 0:
                qty = min(pos, od.buy_orders.get(bid, 0), size)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
            elif pos < 0:
                qty = min(-pos, -od.sell_orders.get(ask, 0), size)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
        return orders
