"""
================================================================================
IMC PROSPERITY 4 — ROUND 3 — v4 (back-to-basics, copying top teams' approach)
================================================================================

POSTMORTEM OF v1, v2, v3:

  v1 scored +£10,420 with parabola-fit smile arb. Won mostly via HYDROGEL MM
  at fair±2 with hardcoded fair=9990. Lost £349 on VEV_5400 (parabola lied).

  v2 ($z$-score per strike, regime correction, dynamic fair) — lost money.
  Over-engineered.

  v3 (rolling EMA fair + position skewing + take-edge thresholds)
     scored -£2,008. EMA fair lags real moves so quotes drift one-sided.
     The "TAKE only if 8 past EMA" rule meant we never took good edges
     while passive quotes kept getting filled into one-sided positions.

WHAT TOP TEAMS ACTUALLY DID (verified from their writeups):

  CMU Physics (chrispyroberts, 7th global, 1st USA):
   - Round 1 Resin (= our HYDROGEL): "We market took anytime bids were
     above 10,000 or asks below 10,000, and market made inside the spread.
     Additionally, we exploited standing orders exactly at fair value to
     better balance our long/short positions."
   - Round 3 vouchers (post-mortem): "Using the mean of [a] rolling window
     of the mid IV instead of the quadratic fit as the fair IV model made
     our backtester PNL shoot up from 80k to 200k per day."
   - "We decided that going UNHEDGED was a risk worth taking" (hedging
     cost more in spread than the risk it saved).

  Ding Crab (Angus, 28th global): For Resin: bid+1, ask-1, BUT bid <= 9999
  and ask >= 10001 always. Then "offload excessive inventory at the fair
  price if possible" (place 0-edge orders AT fair to flatten when biased).

THE STRATEGY HERE — DEAD SIMPLE:

  HYDROGEL_PACK (treat like Resin):
    Fair = 9990 (the historical mean across 3 days; per the data the price
    centre moves by ±3 day-to-day, so 9990 is fine as a static reference).
    1. TAKE any ask < 9990 (free buy at sub-fair price)
    2. TAKE any bid > 9990 (free sell at above-fair price)
    3. POSITION-FLATTENING: if long (>0), sell at 9990 to anyone bidding
       there. If short (<0), buy at 9990 from anyone asking there.
    4. MAKE: passive bid at 9989 (or best_bid+1 if best_bid >= 9988),
            passive ask at 9991 (or best_ask-1 if best_ask <= 9992).
    5. NEVER bid >= 9990, NEVER ask <= 9990 (don't quote across fair).

  VELVETFRUIT_EXTRACT:
    Same approach but with FAIR = popular mid (the bid_volume-weighted mid
    of bots' deepest quotes) — VFE drifts more than HYDROGEL so we need
    a real-time fair value, not a constant. Use the order-book volume-
    weighted mid as the fair (chrispyroberts/Ding Crab's "popular mid"
    approach for Kelp).

  VEV_4000, VEV_4500: intrinsic floor arb. Buy any ask below S - K + small
    cushion. (These vouchers trade ≈ S - K + 17 historically, where 17 is
    the time value premium.)

  VEV_5300, VEV_5400, VEV_5500: rolling-window mid IV (chrispyroberts):
    1. Per strike, maintain rolling window of last 50 mid IVs.
    2. Fair IV = mean of that window.
    3. Compute fair option price from fair IV via Black-Scholes.
    4. TAKE any ask < fair_price - threshold (option is cheap). Buy.
    5. TAKE any bid > fair_price + threshold (option is rich). Sell.
    6. NO market making on options (the spread is 1-3 ticks, can't quote
       inside profitably).
    7. NO delta hedging.

  VEV_5000, 5100, 5200, 6000, 6500: don't trade. Empirically dead in
  historical data (1, 1, 18 trades over 3 days for the first three;
  6000/6500 are pinned at 0.5).

KEY DIFFERENCE FROM v3:
  v3 used EMA-500 fair → lagged → drifted into bad positions.
  v4 uses static fair (HYDROGEL) or popular-mid (VFE) → reactive, doesn't
  lag, takes opportunities the moment they appear.

================================================================================
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math
import json


# =============================================================================
# Black-Scholes (only for vouchers)
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
    """Bisection IV solver."""
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
    """Volume-weighted mid using the popular bid (highest-volume bid level)
    and popular ask (highest-volume ask level). Filters out small wisps that
    skew the simple mid. Used by Ding Crab and CMU Physics for Kelp."""
    if not od.buy_orders or not od.sell_orders:
        return None
    pop_bid = max(od.buy_orders.items(), key=lambda x: x[1])[0]
    pop_ask = min(od.sell_orders.items(), key=lambda x: -x[1])[0]
    return 0.5 * (pop_bid + pop_ask)


# =============================================================================
# Trader
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

    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]
    # Vol vouchers DISABLED: empirically the rolling-IV-mean approach gave
    # only 0.06-0.26 seashells of edge per take in our data — not enough
    # to overcome the bid-ask spread cost. Top teams' approach worked for
    # them (Volcanic Rock had more underlying vol); doesn't translate here.
    VOL_VOUCHERS = []

    # ---- HYDROGEL ----
    HG_FAIR = 9990                # observed historical and live mean
    HG_QUOTE_SIZE = 30
    HG_SKEW_INTENSITY = 2         # ±2 ticks shift at full position

    # ---- VFE ----
    VFE_QUOTE_SIZE = 30
    VFE_SKEW_INTENSITY = 2

    # ---- TTE ----
    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    # ---- Vouchers ----
    IV_WINDOW = 50                # rolling window for fair IV
    IV_WARMUP = 30                # need this many ticks before trading
    VOUCHER_POS_CAP = 100         # under 300 limit
    # Take threshold in seashells (not vol points) — the price gap from
    # fair_price beyond which we take. Spread of voucher ~1-3 ticks, so
    # we need >2 to overcome spread costs.
    VOUCHER_PRICE_EDGE = 2

    def bid(self):
        return 15

    # =========================================================================
    # run()
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

        # ---- HYDROGEL: Resin-style strategy with static fair=9990 ----
        # Backtests show static fair beats popular-mid significantly for this
        # product. HYDROGEL is anchored at ~9990 across all 3 days. The
        # day-to-day drift (9989/9992/9979) is small enough that a static fair
        # is robust. Position drift handled by extreme-pos quote relaxation.
        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._resin_strategy(
                self.HYDROGEL,
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
                fair=self.HG_FAIR,
                quote_size=self.HG_QUOTE_SIZE,
            )

        # ---- VFE: Kelp-style strategy (popular-mid as fair) ----
        S = None
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            pop = popular_mid(od_vfe)
            S = mid_of(od_vfe)  # for vouchers, use the simple mid
            if pop is not None:
                result[self.VFE] = self._kelp_strategy(
                    self.VFE, od_vfe,
                    positions.get(self.VFE, 0),
                    fair=pop,
                    quote_size=self.VFE_QUOTE_SIZE,
                )

        # ---- Vouchers ----
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

            # Rolling-IV-mean voucher trading (chrispyroberts approach)
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

    # =========================================================================
    # RESIN STRATEGY (for HYDROGEL — fixed fair value)
    # =========================================================================
    def _resin_strategy(self, prod: str, od: OrderDepth, pos: int,
                        fair: int, quote_size: int) -> List[Order]:
        """Three-step strategy from CMU Physics / Ding Crab writeups:
          1. TAKE: any ask < fair, any bid > fair
          2. FLATTEN: if long, sell AT fair (volume there is free unwind);
                      if short, buy AT fair.
          3. MAKE: post bid at max(best_bid+1, fair-something) but
                   bid <= fair - 1 always; ask similarly mirrored.
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        # === STEP 1: TAKE clear mispricings ===
        # Any ask strictly below fair = guaranteed profit on closeout
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

        # Any bid strictly above fair = guaranteed profit on closeout
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

        # === STEP 2: FLATTEN AT FAIR ===
        # If long, sell at fair to any standing bid AT fair (zero edge but
        # reduces position so future MM can be more profitable)
        if pos > 0 and fair in od.buy_orders:
            avail = od.buy_orders[fair]
            qty = min(avail, pos)  # only flatten as much as we're long
            if qty > 0:
                orders.append(Order(prod, fair, -qty))
                pos -= qty

        # If short, buy at fair from any standing ask AT fair
        if pos < 0 and fair in od.sell_orders:
            avail = -od.sell_orders[fair]
            qty = min(avail, -pos)
            if qty > 0:
                orders.append(Order(prod, fair, qty))
                pos += qty

        # === STEP 3: MAKE inside the spread ===
        cur_bid, cur_ask = best_bid_ask(od)

        # Default quotes: just inside the bot quotes, but never crossing fair
        if cur_bid is not None and cur_bid < fair - 1:
            our_bid = cur_bid + 1
        else:
            our_bid = fair - 1
        if cur_ask is not None and cur_ask > fair + 1:
            our_ask = cur_ask - 1
        else:
            our_ask = fair + 1

        # Default cap: bid <= fair-1, ask >= fair+1 (collect spread)
        bid_cap = fair - 1
        ask_floor = fair + 1

        # When very long (>50% of limit), allow asking AT fair to dump inventory
        # When extremely long (>80% of limit), allow asking BELOW fair (give up
        # 1 tick of theoretical edge to clear position; it's still profitable
        # if we bought below fair earlier, and prevents disaster if fair shifts)
        if pos > limit * 0.5:
            ask_floor = fair  # ask at fair OK
        if pos > limit * 0.8:
            ask_floor = fair - 1  # ask 1 below fair OK

        # Symmetric for short
        if pos < -limit * 0.5:
            bid_cap = fair
        if pos < -limit * 0.8:
            bid_cap = fair + 1

        # Apply caps
        our_bid = min(our_bid, bid_cap)
        our_ask = max(our_ask, ask_floor)
        # Ensure ask > bid
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        buy_capacity = limit - pos
        sell_capacity = pos + limit
        if buy_capacity > 0:
            orders.append(Order(prod, our_bid, min(buy_capacity, quote_size)))
        if sell_capacity > 0:
            orders.append(Order(prod, our_ask, -min(sell_capacity, quote_size)))
        return orders

    # =========================================================================
    # KELP STRATEGY (for VFE — popular mid as fair)
    # =========================================================================
    def _kelp_strategy(self, prod: str, od: OrderDepth, pos: int,
                       fair: float, quote_size: int) -> List[Order]:
        """Like resin but fair is dynamic (popular mid). The fair *moves*
        each tick, so we don't try to flatten AT fair (no fixed level).
        Just take + make.
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        # === TAKE ===
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

        # === MAKE ===
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

        # Position-based caps: at extreme positions, allow crossing fair to unwind
        bid_cap = fair_int - 1
        ask_floor = fair_int + 1
        if pos > limit * 0.5:
            ask_floor = fair_int
        if pos > limit * 0.8:
            ask_floor = fair_int - 1
        if pos < -limit * 0.5:
            bid_cap = fair_int
        if pos < -limit * 0.8:
            bid_cap = fair_int + 1

        our_bid = min(our_bid, bid_cap)
        our_ask = max(our_ask, ask_floor)
        if our_ask <= our_bid:
            our_ask = our_bid + 1

        # ASYMMETRIC SIZING by position:
        # When LONG, want to UNWIND -> bigger ask, smaller bid.
        # When SHORT, want to UNWIND -> bigger bid, smaller ask.
        # When flat, normal size on both.
        # Specifically: sizing scales linearly with |pos|/limit.
        buy_capacity = limit - pos
        sell_capacity = pos + limit

        pos_frac = pos / limit  # in [-1, +1]
        # When pos_frac=+1 (max long): bid_size_mult=0.2, ask_size_mult=1.5
        # When pos_frac=-1 (max short): bid_size_mult=1.5, ask_size_mult=0.2
        # When pos_frac=0: both 1.0
        bid_mult = max(0.2, 1.0 - pos_frac * 0.8)
        ask_mult = max(0.2, 1.0 + pos_frac * 0.8)

        bid_size = min(buy_capacity, int(quote_size * bid_mult))
        ask_size = min(sell_capacity, int(quote_size * ask_mult))

        if bid_size > 0:
            orders.append(Order(prod, our_bid, bid_size))
        if ask_size > 0:
            orders.append(Order(prod, our_ask, -ask_size))
        return orders

    # =========================================================================
    # INTRINSIC FLOOR ARB (deep ITM vouchers)
    # =========================================================================
    def _intrinsic_arb(self, prod: str, K: int, S: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]
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

    # =========================================================================
    # ROLLING-IV-MEAN VOUCHER STRATEGY (chrispyroberts approach)
    # =========================================================================
    def _rolling_iv_strategy(self, prod: str, K: int, S: float, T: float,
                             od: OrderDepth, pos: int, mem: dict) -> List[Order]:
        """For each tick:
          1. Compute mid IV.
          2. Maintain rolling window of last N mid IVs.
          3. Fair IV = mean of window.
          4. Fair price = bs_call(S, K, T, fair_IV).
          5. TAKE any ask < fair_price - VOUCHER_PRICE_EDGE.
          6. TAKE any bid > fair_price + VOUCHER_PRICE_EDGE.
          7. No market making on options.
        """
        orders: List[Order] = []
        cap = self.VOUCHER_POS_CAP

        m_px = mid_of(od)
        if m_px is None:
            return orders

        iv_now = implied_vol(m_px, S, K, T)
        if iv_now is None or iv_now < 0.05 or iv_now > 1.5:
            return orders

        # Roll the window
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

        # TAKE: ask too cheap
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

        # TAKE: bid too rich
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
