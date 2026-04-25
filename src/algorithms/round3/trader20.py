"""
================================================================================
IMC PROSPERITY 4 — ROUND 3 — v20 (spot core + sparse 5300/5400 spread)
================================================================================

WHY THIS VERSION EXISTS:

  The forensic on trader4 vs trader10/11/12 was very clear:

  - VELVETFRUIT_EXTRACT was almost identical across runs
  - option logic barely mattered
  - the whole PnL swing came from HYDROGEL_PACK

  trader4 worked because it got very short Hydrogel while prices were rich,
  then mostly STAYED short during the later selloff.

  Later versions broke that by covering too aggressively below fair and then
  flipping all the way from big short to max long. That destroyed the best
  source of edge we had.

  trader13 fixed the reversal problem, but it still reached max short almost
  immediately. That preserved the big payoff, but it also preserved the ugly
  early drawdown when Hydrogel drifted higher before eventually falling.

  trader14 reduced the later swings, but it over-corrected: it covered too
  much of the profitable Hydrogel short in the weak regime and gave away a
  large part of the payoff.

  trader15 found a better middle ground, but it still hit -200 almost
  immediately at the open. So the early drawdown problem was still there.

  trader16 tightened the rich-side bands, but in practice the opening book was
  rich enough that the bot still max-shorted almost immediately.

  trader17 fixed that with a time-based Hydrogel warm-up cap and gave us the
  best spot-only core so far.

THE STRATEGY HERE:

  1. HYDROGEL_PACK:
     Keep trader17 as the spot core, and add back a SMALL linked voucher
     strategy:
       - still buy obvious gifts below fair
       - still flatten some shorts at fair
       - but if Hydrogel is already trading well below fair, only cover
         shorts slowly and do not allow the bot to run into a large long
         position just because price looks "cheap"
       - if Hydrogel is only a little above fair, do not allow an immediate
         rush to -200; scale into the short much more gradually
       - and, regardless of price bands, cap the maximum Hydrogel short during
         the opening phase while the market is still discovering direction

  4. Linked vouchers:
     trader19 showed that trading every linked spread and butterfly at once
     creates pure spread churn. So this version cuts the option idea down to
     one sparse relative-value trade only:

       - trade just the 5300/5400 spread
       - enter only on tail dislocations relative to the spread's own recent
         history
       - hold until meaningful reversion
       - enforce a cooldown before re-entering
       - no 5500, no butterfly, no VFE hedge
       - if Hydrogel is already below fair, do not let the bot unwind the
         whole short; keep a residual bearish inventory because that was the
         main source of PnL in the better runs

  2. VELVETFRUIT_EXTRACT:
     Keep trader4's popular-mid Kelp-style market making unchanged.

  3. Vouchers:
     Keep only the deep-ITM intrinsic-style arb in VEV_4000 / VEV_4500.
     The smile sleeve looked good in theory but did not produce meaningful
     live edge for us, so it is intentionally removed here.

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
    # Sparse voucher sleeve: only the core 5300/5400 relationship.
    LINK_VOUCHERS = ["VEV_5300", "VEV_5400"]

    # ---- HYDROGEL ----
    HG_FAIR = 9990                # observed historical and live mean
    HG_QUOTE_SIZE = 30
    HG_SKEW_INTENSITY = 2         # kept for documentation; not used directly
    # Below-fair Hydrogel is where later versions got into trouble: they kept
    # buying too eagerly and converted a profitable short into a max long.
    # These settings keep the short-bias alive when the market is already weak.
    HG_CHEAP_BAND = 5             # treat mid <= fair-5 as "market already weak"
    HG_DEEP_CHEAP_BAND = 8        # even weaker regime, cover shorts very slowly
    HG_LONG_CAP_WHEN_CHEAP = 20   # never let cheap Hydrogel become a big long
    HG_COVER_SIZE_WHEN_CHEAP = 6  # tiny short-covering bids in weak market
    HG_CORE_SHORT_WHEN_CHEAP = -80   # keep at least this short in weak regime
    HG_CORE_SHORT_WHEN_DEEP = -120   # keep even more short when very weak
    # Short entry pacing above fair. The old trader4 logic could slam straight
    # to -200 in the opening seconds. These bands keep the same bearish view,
    # but let us scale into it instead of taking the whole timing risk at once.
    HG_RICH_SOFT_BAND = 3
    HG_RICH_MID_BAND = 7
    HG_RICH_HARD_BAND = 12
    HG_SHORT_CAP_SOFT = -20
    HG_SHORT_CAP_MID = -60
    HG_SHORT_CAP_HARD = -120
    # Time-based warm-up caps for the open. These are stricter than the
    # richness bands because the main residual instability is the bot reaching
    # -200 by timestamp 400. We want the opening to earn information first.
    HG_WARMUP_T1 = 2_000
    HG_WARMUP_T2 = 5_000
    HG_WARMUP_CAP1 = -80
    HG_WARMUP_CAP2 = -140

    # ---- VFE ----
    VFE_QUOTE_SIZE = 34
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
    LINK_VOUCHER_CAP = 16
    LINK_WINDOW = 80
    LINK_MIN_SAMPLES = 35
    LINK_SPREAD_EDGE = 2.25
    LINK_EXIT_BAND = 0.5
    LINK_COOLDOWN_TICKS = 4_000

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
        self._ensure_link_memory(mem)

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
                timestamp=state.timestamp,
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

            link_orders = self._linked_voucher_strategy(
                state.order_depths,
                positions,
                mem,
                state.timestamp,
            )
            for prod, orders in link_orders.items():
                if orders:
                    result[prod] = result.get(prod, []) + orders

        return result, 0, json.dumps(mem)

    def _ensure_link_memory(self, mem: dict) -> None:
        mem.setdefault("link_spread_5300_5400", [])
        mem.setdefault("link_state", 0)
        mem.setdefault("link_last_exit_ts", -10**9)

    # =========================================================================
    # RESIN STRATEGY (for HYDROGEL — fixed fair value)
    # =========================================================================
    def _resin_strategy(self, prod: str, od: OrderDepth, pos: int,
                        fair: int, quote_size: int, timestamp: int) -> List[Order]:
        """Three-step strategy from CMU Physics / Ding Crab writeups:
          1. TAKE: any ask < fair, any bid > fair
          2. FLATTEN: if long, sell AT fair (volume there is free unwind);
                      if short, buy AT fair.
          3. MAKE: post bid at max(best_bid+1, fair-something) but
                   bid <= fair - 1 always; ask similarly mirrored.
        """
        orders: List[Order] = []
        limit = self.POS_LIMIT[prod]

        mid = mid_of(od)
        if mid is None:
            mid = float(fair)
        cheap_regime = mid <= fair - self.HG_CHEAP_BAND
        deep_cheap_regime = mid <= fair - self.HG_DEEP_CHEAP_BAND
        richness = mid - fair

        # In a weak Hydrogel regime, we still want to recycle inventory, but
        # we do not want to fully reverse from short to large long.
        if cheap_regime:
            buy_ceiling = self.HG_LONG_CAP_WHEN_CHEAP
        else:
            buy_ceiling = limit
        # In weak Hydrogel we want to keep a residual short, not flatten the
        # whole book. The deeper the weakness, the larger the protected short.
        if deep_cheap_regime:
            buy_ceiling = min(buy_ceiling, self.HG_CORE_SHORT_WHEN_DEEP)
        elif cheap_regime:
            buy_ceiling = min(buy_ceiling, self.HG_CORE_SHORT_WHEN_CHEAP)

        # Stage short entry by how rich Hydrogel is relative to fair.
        # Slightly rich: small short. Moderately rich: medium short.
        # Very rich: allow the full trader4 behavior.
        sell_floor = -limit
        if richness <= self.HG_RICH_SOFT_BAND:
            sell_floor = self.HG_SHORT_CAP_SOFT
        elif richness <= self.HG_RICH_MID_BAND:
            sell_floor = self.HG_SHORT_CAP_MID
        elif richness <= self.HG_RICH_HARD_BAND:
            sell_floor = self.HG_SHORT_CAP_HARD

        # Opening warm-up cap: no matter how rich the book looks, do not let
        # the Hydrogel short reach full size in the very first phase.
        if timestamp < self.HG_WARMUP_T1:
            sell_floor = max(sell_floor, self.HG_WARMUP_CAP1)
        elif timestamp < self.HG_WARMUP_T2:
            sell_floor = max(sell_floor, self.HG_WARMUP_CAP2)

        # === STEP 1: TAKE clear mispricings ===
        # Any ask strictly below fair = guaranteed profit on closeout
        for ask in sorted(od.sell_orders.keys()):
            if ask >= fair:
                break
            if pos >= buy_ceiling:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, buy_ceiling - pos)
            # When Hydrogel is already well below fair and we are still short,
            # cover only in small clips. This preserves trader4's profitable
            # short bias instead of stampeding into a full reversal.
            if cheap_regime and pos < 0:
                qty = min(qty, self.HG_COVER_SIZE_WHEN_CHEAP)
            if qty > 0:
                orders.append(Order(prod, ask, qty))
                pos += qty

        # Any bid strictly above fair = guaranteed profit on closeout
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if bid <= fair:
                break
            if pos <= sell_floor:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos - sell_floor)
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
        if pos < 0 and fair in od.sell_orders and not deep_cheap_regime:
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
        if cheap_regime:
            # Weak Hydrogel should not turn into a meaningful long just because
            # our fair is static. Cap passive long accumulation tightly.
            buy_capacity = max(0, min(buy_capacity, buy_ceiling - pos))
        # Likewise, don't let a barely-rich Hydrogel immediately fill us to the
        # full short limit. We keep leaning short, just more gradually.
        sell_capacity = max(0, min(sell_capacity, pos - sell_floor))
        if buy_capacity > 0:
            bid_size = min(buy_capacity, quote_size)
            if cheap_regime and pos < 0:
                bid_size = min(bid_size, self.HG_COVER_SIZE_WHEN_CHEAP)
            if bid_size > 0:
                orders.append(Order(prod, our_bid, bid_size))
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
    # LINKED VOUCHER STRATEGY
    # =========================================================================
    def _linked_voucher_strategy(
        self,
        order_depths: Dict[str, OrderDepth],
        positions: Dict[str, int],
        mem: dict,
        timestamp: int,
    ) -> Dict[str, List[Order]]:
        """Sparse stateful trading of the 5300/5400 spread only."""
        mids = {}
        for prod in self.LINK_VOUCHERS:
            od = order_depths.get(prod)
            if od is None:
                return {}
            m = mid_of(od)
            if m is None:
                return {}
            mids[prod] = m

        s_53_54 = mids["VEV_5300"] - mids["VEV_5400"]
        self._push_hist(mem["link_spread_5300_5400"], s_53_54)
        if len(mem["link_spread_5300_5400"]) < self.LINK_MIN_SAMPLES:
            return {}

        stats_53_54 = self._mean_std(mem["link_spread_5300_5400"])
        if not stats_53_54:
            return {}

        mean_53_54, std_53_54 = stats_53_54
        z_53_54 = (s_53_54 - mean_53_54) / std_53_54

        results = {"VEV_5300": [], "VEV_5400": []}
        state = mem["link_state"]
        last_exit = mem["link_last_exit_ts"]
        cooldown_done = (timestamp - last_exit) >= self.LINK_COOLDOWN_TICKS

        # Exit existing spread when the dislocation has meaningfully reverted.
        if state != 0 and abs(z_53_54) <= self.LINK_EXIT_BAND:
            self._flatten_one_step("VEV_5300", order_depths, positions, results, 8)
            self._flatten_one_step("VEV_5400", order_depths, positions, results, 8)
            net_5300 = positions.get("VEV_5300", 0) + sum(o.quantity for o in results["VEV_5300"])
            net_5400 = positions.get("VEV_5400", 0) + sum(o.quantity for o in results["VEV_5400"])
            if net_5300 == 0 and net_5400 == 0:
                mem["link_state"] = 0
                mem["link_last_exit_ts"] = timestamp
            return {k: v for k, v in results.items() if v}

        # Enter only when flat, outside cooldown, and on tail z-scores.
        if state == 0 and cooldown_done:
            if z_53_54 >= self.LINK_SPREAD_EDGE:
                self._sell_up_to("VEV_5300", order_depths, positions, results, self.LINK_VOUCHER_CAP, 8)
                self._buy_up_to("VEV_5400", order_depths, positions, results, self.LINK_VOUCHER_CAP, 8)
                if results["VEV_5300"] or results["VEV_5400"]:
                    mem["link_state"] = -1
            elif z_53_54 <= -self.LINK_SPREAD_EDGE:
                self._buy_up_to("VEV_5300", order_depths, positions, results, self.LINK_VOUCHER_CAP, 8)
                self._sell_up_to("VEV_5400", order_depths, positions, results, self.LINK_VOUCHER_CAP, 8)
                if results["VEV_5300"] or results["VEV_5400"]:
                    mem["link_state"] = 1

        return {k: v for k, v in results.items() if v}

    def _push_hist(self, hist: List[float], value: float) -> None:
        hist.append(value)
        if len(hist) > self.LINK_WINDOW:
            del hist[:-self.LINK_WINDOW]

    def _mean_std(self, hist: List[float]):
        if len(hist) < 2:
            return None
        mean = sum(hist) / len(hist)
        var = sum((x - mean) ** 2 for x in hist) / max(1, len(hist) - 1)
        std = math.sqrt(var)
        if std < 1e-6:
            return None
        return mean, std

    def _buy_up_to(
        self,
        prod: str,
        order_depths: Dict[str, OrderDepth],
        positions: Dict[str, int],
        results: Dict[str, List[Order]],
        cap: int,
        max_qty: int,
    ) -> None:
        od = order_depths[prod]
        pos = positions.get(prod, 0) + sum(o.quantity for o in results.get(prod, []) if o.quantity > 0) + sum(o.quantity for o in results.get(prod, []) if o.quantity < 0)
        for ask in sorted(od.sell_orders.keys()):
            if pos >= cap or max_qty <= 0:
                break
            avail = -od.sell_orders[ask]
            qty = min(avail, cap - pos, max_qty)
            if qty > 0:
                results[prod].append(Order(prod, ask, qty))
                pos += qty
                max_qty -= qty

    def _sell_up_to(
        self,
        prod: str,
        order_depths: Dict[str, OrderDepth],
        positions: Dict[str, int],
        results: Dict[str, List[Order]],
        cap: int,
        max_qty: int,
    ) -> None:
        od = order_depths[prod]
        pos = positions.get(prod, 0) + sum(o.quantity for o in results.get(prod, []) if o.quantity > 0) + sum(o.quantity for o in results.get(prod, []) if o.quantity < 0)
        for bid in sorted(od.buy_orders.keys(), reverse=True):
            if pos <= -cap or max_qty <= 0:
                break
            avail = od.buy_orders[bid]
            qty = min(avail, pos + cap, max_qty)
            if qty > 0:
                results[prod].append(Order(prod, bid, -qty))
                pos -= qty
                max_qty -= qty

    def _flatten_one_step(
        self,
        prod: str,
        order_depths: Dict[str, OrderDepth],
        positions: Dict[str, int],
        results: Dict[str, List[Order]],
        max_qty: int,
    ) -> None:
        od = order_depths[prod]
        net = positions.get(prod, 0) + sum(o.quantity for o in results.get(prod, []))
        if net > 0:
            for bid in sorted(od.buy_orders.keys(), reverse=True):
                qty = min(od.buy_orders[bid], net, max_qty)
                if qty > 0:
                    results[prod].append(Order(prod, bid, -qty))
                    return
        elif net < 0:
            for ask in sorted(od.sell_orders.keys()):
                qty = min(-od.sell_orders[ask], -net, max_qty)
                if qty > 0:
                    results[prod].append(Order(prod, ask, qty))
                    return

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
