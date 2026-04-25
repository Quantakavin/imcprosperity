"""
================================================================================
IMC PROSPERITY 4 — ROUND 3: "GLOVES OFF"
================================================================================

WHAT THIS FILE IS:
This is the algorithm we upload to Prosperity. The platform calls our `run()`
method ~10,000 times during the live round. Each call ("iteration") gives us a
TradingState (current market snapshot) and we return the orders we want placed.

WHAT WE TRADE THIS ROUND:
  1. HYDROGEL_PACK              — delta-1 product, position limit 200
  2. VELVETFRUIT_EXTRACT (VFE)  — delta-1 product, position limit 200
  3. 10x VEV_xxxx vouchers      — call options on VFE, position limit 300 each
                                  Strikes: 4000, 4500, 5000, 5100, 5200, 5300,
                                           5400, 5500, 6000, 6500
                                  Expire in 5 days from start of round 3.

OUR STRATEGIES (one per asset class):

  HYDROGEL_PACK
    Market-make around a fixed fair value. Historical mean is ~9990, std ~32,
    very stable -> classic "post tight quotes around fair, take any obvious
    mispricings" play, identical to RAINFOREST_RESIN from previous rounds.

  VELVETFRUIT_EXTRACT
    Market-make around a rolling fair value (EMA of mid-price). It moves a bit
    more than HYDROGEL but still mean-reverts within a tight ~100-tick band.

  VOUCHERS (the real money)
    Two strategies:
      a) INTRINSIC FLOOR ARB (deep ITM: VEV_4000, VEV_4500)
         A call option must be worth at least max(S - K, 0). If anyone offers
         VEV_4000 below S - 4000, we buy instantly — guaranteed profit.

      b) VOL SMILE ARB (live strikes: VEV_5000 through VEV_5500)
         Each tick:
           1. Compute implied volatility (IV) for each voucher.
           2. Fit a parabola through (moneyness, IV) for the live strikes.
              This parabola is our "fair vol curve".
           3. Any voucher whose IV is meaningfully ABOVE the curve is too
              expensive -> we SELL it. BELOW the curve -> we BUY it.
           4. When IV reverts to the curve, close the position.

         VEV_6000 and VEV_6500 are pinned at price 0.5 in all historical data
         (deep OTM with too little time to move ITM) — we ignore them.

REFERENCE (Prosperity API):
  - run() must return (result_dict, conversions_int, traderData_str)
  - Orders use INTEGER prices
  - Negative quantity = sell, positive = buy
  - traderData is a string we get back next iteration (state persistence)
  - bid() method exists for round 2 (ignored otherwise but harmless to keep)
================================================================================
"""

from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import math
import json


# =============================================================================
# SECTION 1: BLACK-SCHOLES MATH
# =============================================================================
# Black-Scholes is the standard model for European call options. It says:
#
#   Call price = S * N(d1) - K * exp(-r*T) * N(d2)
#
# where:
#   S     = current price of the underlying asset
#   K     = strike price of the option
#   T     = time to expiry (in years)
#   r     = risk-free interest rate (we assume 0 in Prosperity)
#   sigma = volatility of the underlying (annualized)
#   N()   = standard normal cumulative distribution function (CDF)
#   d1, d2 = standard formulas (see code below)
#
# We use this for two things:
#   1. Given (S, K, T, sigma), compute fair option price -> bs_call()
#   2. Given an observed market price, find the sigma that makes BS match
#      that price -> implied_vol()  (this is the IMPLIED volatility)
# =============================================================================

def norm_cdf(x: float) -> float:
    """Standard normal CDF using the math.erf function (no scipy in Prosperity).

    erf is the "error function" — it's related to the normal CDF by:
        N(x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: float, T: float, sigma: float) -> float:
    """Black-Scholes price of a European call option (assuming r = 0).

    Args:
        S:     spot price of underlying
        K:     strike price
        T:     time to expiry in YEARS
        sigma: volatility (annualized, e.g. 0.20 = 20%)
    """
    # Edge cases: at expiry or no vol -> option is worth pure intrinsic
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)

    sqrtT = math.sqrt(T)
    # d1 measures how "in the money" the option is, scaled by vol and time
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    # The formula: S * N(d1) - K * N(d2)
    # Intuition: S * N(d1) is "expected payoff if exercised", K * N(d2) is
    # "expected cost of exercising", weighted by probability.
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def implied_vol(price: float, S: float, K: float, T: float) -> float:
    """Find the volatility sigma that makes bs_call() output equal `price`.

    We use BISECTION (binary search): keep halving the [low, high] range
    until bs_call(sigma=mid) is close enough to the observed price.

    Returns None if the price is below intrinsic value (no valid IV exists)
    or the search fails for any reason.
    """
    intrinsic = max(S - K, 0.0)

    # If the option trades at or below intrinsic, IV doesn't exist —
    # any nonneg sigma gives a price >= intrinsic, so we can't match.
    if price <= intrinsic + 1e-4:
        return None
    # Sanity: option can't be worth more than the underlying itself
    if price >= S:
        return None

    lo, hi = 1e-4, 3.0  # search between 0.01% and 300% vol
    for _ in range(60):  # 60 iterations of bisection -> ~1e-18 precision
        mid = 0.5 * (lo + hi)
        if bs_call(S, K, T, mid) > price:
            # BS price too high -> sigma too high -> shrink upper bound
            hi = mid
        else:
            # BS price too low -> sigma too low -> raise lower bound
            lo = mid
        if hi - lo < 1e-6:
            break  # converged
    return 0.5 * (lo + hi)


# =============================================================================
# SECTION 2: ORDER BOOK HELPERS
# =============================================================================

def best_bid_ask(od: OrderDepth):
    """Return (best_bid_price, best_ask_price) from an order book.

    Best bid = highest price someone is willing to BUY at
    Best ask = lowest price someone is willing to SELL at
    Returns (None, None) if either side is empty.
    """
    bid = max(od.buy_orders.keys()) if od.buy_orders else None
    ask = min(od.sell_orders.keys()) if od.sell_orders else None
    return bid, ask


def mid_price(od: OrderDepth):
    """Midpoint between best bid and best ask. Our proxy for "fair price"."""
    b, a = best_bid_ask(od)
    if b is None or a is None:
        return None
    return 0.5 * (b + a)


# =============================================================================
# SECTION 3: THE TRADER CLASS — main entrypoint Prosperity calls
# =============================================================================

class Trader:
    # -------------------------------------------------------------------------
    # CONSTANTS — product names and config in one place so it's easy to tweak
    # -------------------------------------------------------------------------
    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"

    # All 10 vouchers and their strike prices
    VOUCHERS = {
        "VEV_4000": 4000, "VEV_4500": 4500, "VEV_5000": 5000,
        "VEV_5100": 5100, "VEV_5200": 5200, "VEV_5300": 5300,
        "VEV_5400": 5400, "VEV_5500": 5500, "VEV_6000": 6000,
        "VEV_6500": 6500,
    }

    # Position limits per the round 3 wiki page
    # (long position can't exceed +limit, short can't go below -limit)
    POS_LIMIT = {
        HYDROGEL: 200,
        VFE: 200,
        **{v: 300 for v in VOUCHERS},  # 300 per voucher per the spec
    }

    # We split vouchers into 3 groups based on what strategy applies:
    LIVE_VOUCHERS = ["VEV_5000", "VEV_5100", "VEV_5200",
                     "VEV_5300", "VEV_5400", "VEV_5500"]
    INTRINSIC_VOUCHERS = ["VEV_4000", "VEV_4500"]   # too deep ITM for IV
    DEAD_VOUCHERS = ["VEV_6000", "VEV_6500"]        # pinned at 0.5, untraded

    # ---- Time-to-expiry parameters ----
    # Round 3 starts with TTE = 5 days. Each round is 1M timestamp ticks.
    TTE_DAYS_START = 5.0
    TICKS_PER_DAY = 1_000_000

    # ---- Smile-arb thresholds (in vol points, e.g. 0.012 = 1.2% vol) ----
    # If a voucher's IV deviates from the fitted parabola by more than
    # SMILE_EDGE, we trade. If within SMILE_CLOSE, we unwind.
    SMILE_EDGE = 0.012   # entry threshold — tune based on backtest
    SMILE_CLOSE = 0.004  # exit threshold

    # ---- HYDROGEL fair value (calibrate from data) ----
    # Historical mean across 3 days is ~9990. Update if live mean shifts.
    HYDROGEL_FAIR = 9990
    HYDROGEL_HALF_SPREAD = 2  # we quote 2 ticks either side of fair

    # =========================================================================
    # bid() method — required for Round 2 only, harmless to keep here always.
    # =========================================================================
    def bid(self):
        """Round 2 entry — irrelevant for round 3 but kept per spec."""
        return 15

    # =========================================================================
    # run() — THE MAIN METHOD. Called every iteration by the platform.
    # =========================================================================
    # Returns:
    #   result:       {product_name: [Order, Order, ...]}
    #   conversions:  int (used in some rounds for conversion requests; 0 here)
    #   traderData:   str (persisted state; we serialize a small dict to JSON)
    # =========================================================================
    def run(self, state: TradingState):
        # ---- restore persistent state (e.g. our rolling VFE EMA) ----
        # traderData is just a string, so we json-encode/decode a dict.
        try:
            mem = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            mem = {}  # if anything goes wrong, start fresh

        positions = state.position  # {product: signed qty currently held}

        # ---- compute current time-to-expiry in YEARS ----
        # Each round is 1M ticks; we count down from 5 days.
        ticks_done = state.timestamp
        tte_days = max(0.01, self.TTE_DAYS_START - ticks_done / self.TICKS_PER_DAY)
        T = tte_days / 365.0  # convert days to years for Black-Scholes

        # The dict we'll return — one entry per product we want to trade
        result: Dict[str, List[Order]] = {}

        # =====================================================================
        # STRATEGY 1: Market-make HYDROGEL_PACK around a fixed fair value
        # =====================================================================
        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._mm_hydrogel(
                state.order_depths[self.HYDROGEL],
                positions.get(self.HYDROGEL, 0),
            )

        # =====================================================================
        # STRATEGY 2: Market-make VELVETFRUIT_EXTRACT around a rolling EMA
        # We also need its mid-price to value the vouchers (it's S in BS).
        # =====================================================================
        S = None  # spot price of VFE — needed for option pricing
        if self.VFE in state.order_depths:
            od_vfe = state.order_depths[self.VFE]
            S = mid_price(od_vfe)
            result[self.VFE] = self._mm_vfe(
                od_vfe, positions.get(self.VFE, 0), mem,
            )

        # =====================================================================
        # STRATEGY 3: Voucher trading (only meaningful if we know S)
        # =====================================================================
        if S is not None:

            # ------- 3a. Intrinsic-floor arb on deep ITM vouchers -------
            # VEV_4000 must be worth >= S - 4000. If anyone sells it cheaper,
            # we buy. Likewise we sell if someone bids irrationally high.
            for prod in self.INTRINSIC_VOUCHERS:
                if prod in state.order_depths:
                    orders = self._intrinsic_arb(
                        prod, self.VOUCHERS[prod], S,
                        state.order_depths[prod],
                        positions.get(prod, 0),
                    )
                    if orders:
                        result[prod] = orders

            # ------- 3b. Vol-smile arb on the live strikes -------
            # Step 1: compute IV for each live voucher
            ivs = {}  # {prod: (moneyness, iv, mid_price, strike)}
            for prod in self.LIVE_VOUCHERS:
                if prod not in state.order_depths:
                    continue
                m_px = mid_price(state.order_depths[prod])
                if m_px is None:
                    continue
                K = self.VOUCHERS[prod]
                iv = implied_vol(m_px, S, K, T)
                # Reject obvious nonsense IVs
                if iv is None or iv < 0.05 or iv > 1.5:
                    continue
                # Moneyness: standardised "distance from ATM" axis for the smile
                moneyness = math.log(K / S) / math.sqrt(T)
                ivs[prod] = (moneyness, iv, m_px, K)

            # Step 2: fit a parabola IV(m) = a*m^2 + b*m + c through the points
            # We need at least 4 points for a robust fit (3 = exact, no robustness)
            if len(ivs) >= 4:
                fit = self._fit_parabola(
                    [v[0] for v in ivs.values()],
                    [v[1] for v in ivs.values()],
                )
                if fit is not None:
                    a, b, c = fit
                    # Step 3: compare each voucher's IV to the fitted curve
                    for prod, (mny, iv, m_px, K) in ivs.items():
                        fitted_iv = a * mny * mny + b * mny + c
                        diff = iv - fitted_iv  # +ve = expensive, -ve = cheap
                        pos = positions.get(prod, 0)
                        orders = self._smile_trade(
                            prod, diff,
                            state.order_depths[prod], pos,
                        )
                        if orders:
                            result[prod] = orders

        # =====================================================================
        # Return: orders, no conversions this round, persisted state
        # =====================================================================
        conversions = 0
        traderData = json.dumps(mem)
        return result, conversions, traderData

    # =========================================================================
    # STRATEGY HELPER 1: Market-make HYDROGEL_PACK
    # =========================================================================
    def _mm_hydrogel(self, od: OrderDepth, pos: int) -> List[Order]:
        """Two-step market making:
            (1) TAKE: hit any order that's clearly mispriced vs our fair value
            (2) MAKE: post passive bid + ask 2 ticks either side of fair
        """
        orders: List[Order] = []
        prod = self.HYDROGEL
        limit = self.POS_LIMIT[prod]
        fair = self.HYDROGEL_FAIR

        # ---- (1) TAKE side: any sell order BELOW fair is a steal -> buy it ----
        # od.sell_orders is {price: negative_volume}, sorted by price ascending
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < fair and pos < limit:
                # vol is negative; -vol is the actual size available
                # we're capped by our remaining long capacity (limit - pos)
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))  # +qty = buy
                    pos += qty

        # Any buy order ABOVE fair is overpaying -> we sell to them
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > fair and pos > -limit:
                qty = min(vol, pos + limit)  # capped by short capacity
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))  # -qty = sell
                    pos -= qty

        # ---- (2) MAKE side: post passive quotes for residual capacity ----
        # Buy bid: collect spread by buying at fair-2 if anyone hits us
        # Sell ask: collect spread by selling at fair+2 if anyone lifts us
        bid_px = fair - self.HYDROGEL_HALF_SPREAD  # 9988
        ask_px = fair + self.HYDROGEL_HALF_SPREAD  # 9992
        buy_capacity = limit - pos
        sell_capacity = pos + limit
        if buy_capacity > 0:
            # Cap each quote at 30 to avoid one bad fill blowing up our position
            orders.append(Order(prod, bid_px, min(buy_capacity, 30)))
        if sell_capacity > 0:
            orders.append(Order(prod, ask_px, -min(sell_capacity, 30)))
        return orders

    # =========================================================================
    # STRATEGY HELPER 2: Market-make VELVETFRUIT_EXTRACT
    # =========================================================================
    def _mm_vfe(self, od: OrderDepth, pos: int, mem: dict) -> List[Order]:
        """Same idea as HYDROGEL but with a ROLLING fair value (EMA of mid)
        because VFE drifts a bit more. We update the EMA every tick."""
        orders: List[Order] = []
        prod = self.VFE
        limit = self.POS_LIMIT[prod]

        m = mid_price(od)
        if m is None:
            return orders

        # ---- update exponentially weighted moving average of mid ----
        # alpha=0.05 -> slow update, focuses on the long-run mean
        prev_fair = mem.get("vfe_fair", m)
        alpha = 0.05
        fair = alpha * m + (1 - alpha) * prev_fair
        mem["vfe_fair"] = fair  # persist for next iteration

        # ---- TAKE side: aggressive prices outside our fair-1 / fair+1 band ----
        for ask, vol in sorted(od.sell_orders.items()):
            if ask <= fair - 1 and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid >= fair + 1 and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty

        # ---- MAKE side: passive quotes 2 ticks wide around fair ----
        # Note: prices in Prosperity must be INTEGERS, so we floor/ceil.
        bid_px = int(math.floor(fair - 2))
        ask_px = int(math.ceil(fair + 2))
        buy_capacity = limit - pos
        sell_capacity = pos + limit
        if buy_capacity > 0:
            orders.append(Order(prod, bid_px, min(buy_capacity, 30)))
        if sell_capacity > 0:
            orders.append(Order(prod, ask_px, -min(sell_capacity, 30)))
        return orders

    # =========================================================================
    # STRATEGY HELPER 3: Intrinsic-floor arbitrage for deep ITM vouchers
    # =========================================================================
    def _intrinsic_arb(self, prod: str, K: int, S: float,
                       od: OrderDepth, pos: int) -> List[Order]:
        """A call must be worth at least max(S - K, 0). For deep ITM vouchers
        (4000, 4500), time value is tiny, so the option trades very close
        to the intrinsic floor.

        BUY: if anyone offers below S - K - 1 (strictly below intrinsic).
        SELL: if anyone bids above S - K + 5 (paying way over intrinsic).
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]
        floor = S - K  # minimum option value at expiry given current S

        # Buy any ask that's mispriced low
        for ask, vol in sorted(od.sell_orders.items()):
            if ask < floor - 1 and pos < limit:
                qty = min(-vol, limit - pos)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))
                    pos += qty

        # Sell any bid that's mispriced high (someone overpaying for time value)
        for bid, vol in sorted(od.buy_orders.items(), reverse=True):
            if bid > floor + 5 and pos > -limit:
                qty = min(vol, pos + limit)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))
                    pos -= qty
        return orders

    # =========================================================================
    # STRATEGY HELPER 4: Vol-smile arbitrage for live vouchers
    # =========================================================================
    def _smile_trade(self, prod: str, iv_diff: float,
                     od: OrderDepth, pos: int) -> List[Order]:
        """Trade based on how far this voucher's IV is from the fitted smile.

            iv_diff > +SMILE_EDGE  -> too expensive  -> SELL at best bid
            iv_diff < -SMILE_EDGE  -> too cheap      -> BUY at best ask
            |iv_diff| < SMILE_CLOSE -> near fair     -> unwind toward zero
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]
        bid, ask = best_bid_ask(od)
        if bid is None or ask is None:
            return orders  # can't trade if a side of the book is empty

        if iv_diff > self.SMILE_EDGE:
            # OVERPRICED: hit the bid (sell)
            if pos > -limit:
                # Cap at 20 per tick to avoid getting too short too fast
                avail = od.buy_orders.get(bid, 0)
                qty = min(avail, pos + limit, 20)
                if qty > 0:
                    orders.append(Order(prod, bid, -qty))

        elif iv_diff < -self.SMILE_EDGE:
            # UNDERPRICED: lift the ask (buy)
            if pos < limit:
                avail = -od.sell_orders.get(ask, 0)
                qty = min(avail, limit - pos, 20)
                if qty > 0:
                    orders.append(Order(prod, ask, qty))

        else:
            # FAIRLY PRICED: if we're holding inventory, scale it down
            if abs(iv_diff) < self.SMILE_CLOSE:
                if pos > 0:
                    # long -> sell back to flat
                    qty = min(pos, od.buy_orders.get(bid, 0), 20)
                    if qty > 0:
                        orders.append(Order(prod, bid, -qty))
                elif pos < 0:
                    # short -> buy back to flat
                    qty = min(-pos, -od.sell_orders.get(ask, 0), 20)
                    if qty > 0:
                        orders.append(Order(prod, ask, qty))
        return orders

    # =========================================================================
    # MATH HELPER: least-squares parabola fit (no numpy needed)
    # =========================================================================
    def _fit_parabola(self, xs: List[float], ys: List[float]):
        """Fit y = a*x^2 + b*x + c via the normal equations of least squares.

        We build a 3x3 linear system and solve it with Cramer's rule
        (determinant ratios). Returns (a, b, c) or None if singular.

        Why no numpy? It's fine to use here, but a tiny pure-python solver
        avoids any risk of timeout on large problems.
        """
        n = len(xs)
        if n < 3:
            return None

        # Sums needed for the normal equations
        s0 = n
        s1 = sum(xs)
        s2 = sum(x * x for x in xs)
        s3 = sum(x ** 3 for x in xs)
        s4 = sum(x ** 4 for x in xs)
        sy = sum(ys)
        sxy = sum(x * y for x, y in zip(xs, ys))
        sx2y = sum(x * x * y for x, y in zip(xs, ys))

        # The system: A @ [a,b,c]^T = B
        A = [[s4, s3, s2],
             [s3, s2, s1],
             [s2, s1, s0]]
        B = [sx2y, sxy, sy]
        try:
            return self._solve3x3(A, B)
        except Exception:
            return None

    @staticmethod
    def _solve3x3(A, B):
        """Solve a 3x3 linear system with Cramer's rule (determinants)."""
        def det3(M):
            return (M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1])
                    - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
                    + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]))
        D = det3(A)
        if abs(D) < 1e-12:
            return None  # singular -> can't solve
        out = []
        for i in range(3):
            # Replace column i of A with B, then take ratio of determinants
            M = [row[:] for row in A]
            for r in range(3):
                M[r][i] = B[r]
            out.append(det3(M) / D)
        return tuple(out)
