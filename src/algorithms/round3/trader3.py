"""
================================================================================
IMC PROSPERITY 4 — ROUND 3: "GLOVES OFF" — v3
================================================================================

REDESIGN RATIONALE (informed by top-team writeups + deep data analysis):

WHY v2 WAS WRONG:
  - Per-strike z-score still relied on a fixed historical baseline.
    But this round's IV regime is DIFFERENT to historical. We tried to
    correct with regime-shift, but it was a band-aid.
  - chrispyroberts (7th global Prosperity 3) explicitly says: "Using the
    mean of a rolling window instead of the quadratic fit as the fair IV
    model made our backtester PNL shoot up from 80k to 200k per day."
  - We confirmed this empirically: corr(IV_dev_from_rolling_ema_200,
    fwd_IV_change_50) = -0.55 for VEV_5500. This is a HUGE edge. The
    parabola fit / fixed baseline destroys it.

KEY DATA FINDINGS:
  1. HYDROGEL has bot quotes at mid±8 ALWAYS (spread 16). VFE has bot quotes
     at mid±2 (spread 4-5). When we quote at mid±1 we're way INSIDE the bot,
     get filled on ambient flow. Trades cluster at the bot's price levels.
  2. Both HYDROGEL & VFE MEAN-REVERT to their EMAs — corr(dev_500ema,
     fwd_ret_100) = -0.21 for HYDROGEL, -0.16 for VFE. Tradeable!
  3. Voucher trade volumes (3 days):
        VEV_4000: 464  (deep ITM, traded actively)
        VEV_5500: 267  (the OTM action)
        VEV_5400: 225
        VEV_5300: 121
        VEV_5200: 18, VEV_5100: 1, VEV_5000: 1   ← essentially DEAD
        VEV_6000/6500: 284 each but at price 0.5 (worthless, fixed)
     So the "live ATM" strikes I targeted in v1/v2 actually have NO flow.
     The flow is at 5300/5400/5500 (slightly OTM) and at 4000 (deep ITM).
  4. Per-strike IV time-series MEAN-REVERT to their own rolling means.

v3 STRATEGY:

  HYDROGEL_PACK
    Tight market-making at mid±1 (way inside bot's mid±8 spread).
    PLUS: directional bias from EMA-500 deviation (mean-reversion signal).
    Aggressive position-skewing in quotes to self-rebalance.

  VELVETFRUIT_EXTRACT
    Same idea but quotes at mid±1, EMA-500 mean reversion.
    Note: vouchers correlate ~0.5-0.7 with VFE returns, so VFE drift hurts
    voucher PnL. Stay close to flat.

  VEV_4000, VEV_4500 (deep ITM)
    Intrinsic-floor arb: must trade ≥ S - K. Buy if cheaper, sell if richer
    than floor + small cushion.

  VEV_5300, VEV_5400, VEV_5500 (the live strikes with real flow)
    chrispyroberts approach:
      1. Each tick, compute IV from mid using Black-Scholes.
      2. Maintain a rolling EMA of IV (window ~200 ticks).
      3. When current IV is meaningfully ABOVE rolling mean -> SELL the
         option (it'll mean-revert down).
      4. When BELOW rolling mean -> BUY.
    Per-strike caps at 60 (well under 300 limit). The edge is real but
    finite — sizing-up turns wins into losses fast.

  VEV_5000, 5100, 5200, 6000, 6500
    NOT TRADED. No flow at 5000/5100/5200, and 6000/6500 are pinned at 0.5.
    Don't quote into something you can't get out of.

  NO DELTA HEDGING (chrispyroberts conclusion)
    Their analysis: hedging cost ~40k of spread per day, max realistic
    unhedged loss ~16k. Spread cost > expected loss. Same applies here:
    voucher caps of 60 mean max delta exposure ~ 3 * 60 * 1 = 180 units of
    VFE worth. Per-tick VFE move is ~1 tick, so per-tick delta P&L is small.
    Skip the complexity.

================================================================================
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math
import json


# =============================================================================
# SECTION 1: BLACK-SCHOLES (only used for vouchers now)
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
    """Bisection IV solver. Returns None if invalid."""
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


# =============================================================================
# SECTION 2: ORDER BOOK HELPERS
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
# SECTION 3: TRADER
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

    # The ONLY voucher strikes we trade (had real flow in 3-day data):
    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]
    # Vol-MR vouchers: kept in code but DISABLED via empty list until we
    # confirm in Prosperity's real backtester that passive fills make this
    # profitable. In the naive take-only sim, the spread cost dominates the
    # IV mean-reversion edge and we lose money on every day.
    # To re-enable: ["VEV_5300", "VEV_5400", "VEV_5500"]
    VOL_VOUCHERS = []

    # ---- Time-to-expiry config ----
    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    # ---- HYDROGEL config ----
    # Bot's spread is 16 ticks (mid±8) ALWAYS. We sit at mid±1.
    # EMA window 500 captures mean reversion (corr -0.21 with fwd returns)
    HG_EMA_ALPHA = 2 / (500 + 1)  # span ~500
    HG_QUOTE_HALF = 1             # bid at fair-1, ask at fair+1
    HG_QUOTE_SIZE = 25            # per tick
    # TAKE only if very far from EMA — HYDROGEL std is ~32, so 8+ is meaningful
    HG_TAKE_EDGE = 8              # must be >= 8 ticks past EMA

    # ---- VFE config ----
    VFE_EMA_ALPHA = 2 / (500 + 1)
    VFE_QUOTE_HALF = 1
    VFE_QUOTE_SIZE = 25
    VFE_TAKE_EDGE = 4             # std is ~16, so 4+ ticks is meaningful

    # Position skew: when long, lower both quotes; when short, raise both
    # SKEW_INTENSITY * (pos/limit) = ticks of shift on each quote
    SKEW_INTENSITY = 4            # max ±4 ticks shift at full position

    # ---- Voucher vol-MR config ----
    # Rolling EMA of IV per strike; trade deviations from that mean.
    # Per-strike thresholds based on observed IV-dev distribution:
    #   VEV_5300/5400: dev std ~0.0017, 95% percentile ~0.0026 -> use 0.0030
    #   VEV_5500:      dev std ~0.0026, 95% percentile ~0.0048 -> use 0.0050
    IV_EMA_ALPHA = 2 / (300 + 1)  # span ~300
    IV_THRESHOLDS = {  # per-strike entry threshold
        "VEV_5300": 0.0030,
        "VEV_5400": 0.0030,
        "VEV_5500": 0.0050,
    }
    IV_DEV_EXIT = 0.0010          # exit when within 0.10 vol points
    VOUCHER_POS_CAP = 50          # tighter cap
    VOUCHER_TRADE_SIZE = 8        # per tick

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

        # ---- HYDROGEL_PACK: tight MM + EMA mean reversion ----
        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._mm_with_mr(
                self.HYDROGEL,
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
                mem, "hg_ema",
                self.HG_EMA_ALPHA,
                self.HG_QUOTE_HALF,
                self.HG_QUOTE_SIZE,
                self.HG_TAKE_EDGE,
            )

        # ---- VFE: same approach, also gives us S for vouchers ----
        S = None
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            S = mid_price(od_vfe)
            result[self.VFE] = self._mm_with_mr(
                self.VFE, od_vfe,
                positions.get(self.VFE, 0),
                mem, "vfe_ema",
                self.VFE_EMA_ALPHA,
                self.VFE_QUOTE_HALF,
                self.VFE_QUOTE_SIZE,
                self.VFE_TAKE_EDGE,
            )

        # ---- Vouchers (need S) ----
        if S is not None:
            # Intrinsic-floor arb on deep ITM
            for prod in self.INTRINSIC_VOUCHERS:
                if prod in state.order_depths:
                    orders = self._intrinsic_arb(
                        prod, self.VOUCHERS[prod], S,
                        state.order_depths[prod],
                        positions.get(prod, 0),
                    )
                    if orders:
                        result[prod] = orders

            # Vol-mean-reversion on live strikes
            for prod in self.VOL_VOUCHERS:
                if prod not in state.order_depths:
                    continue
                orders = self._vol_mr(
                    prod, self.VOUCHERS[prod], S, T,
                    state.order_depths[prod],
                    positions.get(prod, 0),
                    mem,
                )
                if orders:
                    result[prod] = orders

        return result, 0, json.dumps(mem)

    # =========================================================================
    # MM + MEAN-REVERSION for HYDROGEL & VFE
    # =========================================================================
    def _mm_with_mr(self, prod: str, od: OrderDepth, pos: int,
                    mem: dict, ema_key: str, alpha: float,
                    quote_half: int, quote_size: int,
                    take_edge: float) -> List[Order]:
        """Strategy:
          1. Update EMA of mid (slow rolling fair).
          2. CONSERVATIVE TAKE: only take if order is more than `take_edge`
             past fair. Without this, we keep buying every ask within the
             16-wide HYDROGEL spread and max out long fast.
          3. PASSIVE MAKE: quote at fair±quote_half, skewed by current pos.
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        m = mid_price(od)
        if m is None:
            return orders

        # Update EMA slowly (long memory = stable fair value estimate)
        prev_ema = mem.get(ema_key, m)
        ema = alpha * m + (1 - alpha) * prev_ema
        mem[ema_key] = ema

        # ---- TAKE side: only when there's REAL edge ----
        # ask < ema - take_edge: someone selling well below fair -> buy
        # bid > ema + take_edge: someone buying well above fair -> sell
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < ema - take_edge and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty

        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > ema + take_edge and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty

        # ---- MAKE side: passive quotes inside the bot spread ----
        # Position skew: when long, lean both quotes DOWN (sell easier, buy harder)
        # When short, lean both quotes UP
        skew = -self.SKEW_INTENSITY * (pos / limit)
        bid_px = int(math.floor(ema + skew - quote_half))
        ask_px = int(math.ceil(ema + skew + quote_half))
        if ask_px <= bid_px:
            ask_px = bid_px + 1

        # Don't quote on top of book if it would just cross with bot
        cur_bid, cur_ask = best_bid_ask(od)
        if cur_ask is not None and bid_px >= cur_ask:
            bid_px = cur_ask - 1
        if cur_bid is not None and ask_px <= cur_bid:
            ask_px = cur_bid + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        if buy_capacity > 0:
            orders.append(Order(prod, bid_px, min(buy_capacity, quote_size)))
        if sell_capacity > 0:
            orders.append(Order(prod, ask_px, -min(sell_capacity, quote_size)))
        return orders

    # =========================================================================
    # INTRINSIC-FLOOR ARB on deep ITM vouchers
    # =========================================================================
    def _intrinsic_arb(self, prod: str, K: int, S: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]
        floor = S - K

        # Buy any ask below floor (free intrinsic)
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < floor - 1 and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty

        # Sell any bid well above floor (overpaying for tiny time value)
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > floor + 5 and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty
        return orders

    # =========================================================================
    # VOL MEAN REVERSION (chrispyroberts approach)
    # =========================================================================
    def _vol_mr(self, prod: str, K: int, S: float, T: float,
                od: OrderDepth, pos: int, mem: dict) -> List[Order]:
        """For a given strike:
          1. Compute current IV from mid price.
          2. Update a rolling EMA of IV for this strike.
          3. If current IV >> EMA -> option is rich -> SELL.
             If current IV << EMA -> option is cheap -> BUY.
          4. Capped at VOUCHER_POS_CAP per direction.
        """
        orders: List[Order] = []
        cap = self.VOUCHER_POS_CAP
        size = self.VOUCHER_TRADE_SIZE

        m_px = mid_price(od)
        if m_px is None:
            return orders

        iv = implied_vol(m_px, S, K, T)
        if iv is None or iv < 0.05 or iv > 1.5:
            return orders

        # Update IV EMA for this strike
        ema_key = f"iv_ema_{prod}"
        prev_iv_ema = mem.get(ema_key, iv)
        iv_ema = self.IV_EMA_ALPHA * iv + (1 - self.IV_EMA_ALPHA) * prev_iv_ema
        mem[ema_key] = iv_ema

        # Need a few ticks of history before trusting the EMA
        warmup_key = f"iv_n_{prod}"
        n = mem.get(warmup_key, 0) + 1
        mem[warmup_key] = n
        if n < 30:
            return orders  # warmup period

        dev = iv - iv_ema  # positive = IV is high vs recent average
        entry_threshold = self.IV_THRESHOLDS.get(prod, 0.0030)

        bid, ask = best_bid_ask(od)
        if bid is None or ask is None:
            return orders

        if dev > entry_threshold:
            # IV is high — option is rich — SELL at bid
            if pos > -cap:
                avail = od.buy_orders.get(bid, 0)
                qty = min(avail, pos + cap, size)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))

        elif dev < -entry_threshold:
            # IV is low — option is cheap — BUY at ask
            if pos < cap:
                avail = -od.sell_orders.get(ask, 0)
                qty = min(avail, cap - pos, size)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))

        elif abs(dev) < self.IV_DEV_EXIT:
            # IV reverted — close any open position
            if pos > 0:
                qty = min(pos, od.buy_orders.get(bid, 0), size)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
            elif pos < 0:
                qty = min(-pos, -od.sell_orders.get(ask, 0), size)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
        return orders
