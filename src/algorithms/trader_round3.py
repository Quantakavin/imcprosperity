"""
=============================================================================
PROSPERITY 4 — ROUND 3 — FOCUSED MARKET-MAKING + DEEP-ITM LEAD SIGNAL
=============================================================================
Two structural edges this version exploits:

(1) WIDE-SPREAD MARKET MAKING
    HYDROGEL_PACK     median spread 16  → quote ±1, ~7 ticks of room each side
    VEV_4500          median spread 16  → mid pinned at S-4500 (stdev 0.75)
    VEV_4000          median spread 21  → mid pinned at S-4000 (stdev 0.82)
    The deep-ITM vouchers have NO time value — their fair is exactly
    intrinsic (S-K) — yet bots quote them ±8…±10 around intrinsic.

(2) SYNTHETIC-VEV LEAD INDICATOR
    Tested across 30k ticks of historical data:
        corr(VEV_4500_mid + 4500 − VEV_mid,  ΔVEV next tick)  =  +0.26
        corr(VEV_4000_mid + 4000 − VEV_mid,  ΔVEV next tick)  =  +0.18
    The deep-ITM book updates faster than the underlying book.  When
    synthetic > VEV_mid, VEV catches up.  A simple "trade when |signal|>1"
    strategy nets +2k seashells/3-day at 1-share size — scales linearly.

    User originally hypothesised "active option spikes lead VEV".  Tested:
    that correlation is ~0 at every lag (active options follow VEV
    contemporaneously, not lead).  The real lead is in the deep-ITM mids.

Skipped products (no edge or hostile spreads):
    VEV_5000, 5100, 5200, 5300, 5400, 5500   1-3 tick spreads, dominated by
                                             other algos, lost money on every
                                             previous iteration.
    VEV_6000, 6500                           pinned at 0.5, no fair edge.

Position limits (per challenge text)
------------------------------------
    HYDROGEL_PACK            ±200
    VELVETFRUIT_EXTRACT      ±200
    VEV_xxxx (each)          ±300

Position-skew quoting
---------------------
Quoted fair shifts by  skew · position  ticks.  Long inventory pulls quotes
down — less likely to buy, more likely to sell.  Auto-rebalance.

Delta safety
------------
VEV, VEV_4000, VEV_4500 are all delta-1.  Combined signed Δ is capped at
±DELTA_BUDGET — quotes on the breaching side are suppressed when the budget
would be exceeded.  No active option positions → no separate hedge ladder.
=============================================================================
"""

import json
import math
from typing import Any, Dict, List, Tuple, Optional

from datamodel import (
    Order, OrderDepth, ProsperityEncoder, Symbol, TradingState,
)


# =============================================================================
# CONSTANTS
# =============================================================================

# Products we actively trade
HYDRO = "HYDROGEL_PACK"
VEV = "VELVETFRUIT_EXTRACT"
VEV_4000 = "VEV_4000"
VEV_4500 = "VEV_4500"
# disabled — taking on entry was net negative
ACTIVE_STRIKES: tuple = ()

TRADED = (HYDRO, VEV_4500, VEV_4000, VEV)

# Position limits (challenge text)
POS_LIMIT = {
    HYDRO: 200,
    VEV:   200,
    VEV_4000: 300,
    VEV_4500: 300,
}
# Soft caps.  HYDRO at full limit captures the most mean-reversion edge; the
# parameter sweep showed +12 k by going from 180 → 200.
SOFT_CAP = {
    HYDRO:    200,
    VEV:      200,
    VEV_4000: 280,
    VEV_4500: 280,
}

# Position-skew strength (ticks of fair shift per share of inventory)
SKEW = {
    HYDRO:    0.06,
    VEV:      0.08,
    VEV_4000: 0.04,
    VEV_4500: 0.04,
}

MAKE_EDGE = {HYDRO: 1, VEV: 1, VEV_4000: 1, VEV_4500: 1}

# Resting size per side — bumped on wide-spread products where adverse-selection
# cost is small relative to the spread we capture.
QUOTE_SIZE = {HYDRO: 50, VEV: 40, VEV_4000: 40, VEV_4500: 40}

# Combined Δ budget across the three delta-1 products.
DELTA_BUDGET = 200

# HYDROGEL anchor — empirical mean of historical mids; we use vw-mid as
# primary fair but blend with this anchor (30 %) for stability.
HYDRO_ANCHOR = 9990

# VEV synthetic-lead weight.  Backtested corr at lag 1 is +0.26 for the
# 4500-synthetic and +0.18 for the 4000-synthetic.
SYNTH_WEIGHT = 0.55
SIGNAL_TAKE = 1.0        # ticks — lowered from 1.5 to capture more signal moves

# Black-Scholes parameters for active-voucher fair value.
# σ = 0.193 (market IV baseline, Magritte 1929).  Per-strike smile σ overrides:
SIGMA_FAIR = 0.1929
SMILE = {5200: 0.193, 5300: 0.195, 5400: 0.183}

# T-to-expiry by effective game-day.  For backtest day 0 = 8d, ..., live R3
# day 0 = 5d → set LIVE_DAY_OFFSET=3 on submit.
LIVE_DAY_OFFSET = 3
TTE_DAYS = {0: 8, 1: 7, 2: 6, 3: 5, 4: 4, 5: 3, 6: 2, 7: 1}


# =============================================================================
# LOGGER (Prosperity boilerplate)
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state, orders, conversions, trader_data):
        base_length = len(self.to_json([
            self.compress_state(state, ""),
            self.compress_orders(orders),
            conversions, "", "",
        ]))
        max_item_length = (self.max_log_length - base_length) // 3
        print(self.to_json([
            self.compress_state(state, self.truncate(
                state.traderData, max_item_length)),
            self.compress_orders(orders),
            conversions,
            self.truncate(trader_data, max_item_length),
            self.truncate(self.logs, max_item_length),
        ]))
        self.logs = ""

    def compress_state(self, state, trader_data):
        return [
            state.timestamp, trader_data,
            [[l.symbol, l.product, l.denomination]
                for l in state.listings.values()],
            {s: [od.buy_orders, od.sell_orders]
                for s, od in state.order_depths.items()},
            [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
             for arr in state.own_trades.values() for t in arr],
            [[t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp]
             for arr in state.market_trades.values() for t in arr],
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_observations(self, observations):
        conv = {}
        try:
            for p, o in observations.conversionObservations.items():
                conv[p] = [o.bidPrice, o.askPrice, o.transportFees,
                           o.exportTariff, o.importTariff,
                           getattr(o, "sugarPrice", 0),
                           getattr(o, "sunlightIndex", 0)]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conv]

    def compress_orders(self, orders):
        return [[o.symbol, o.price, o.quantity]
                for arr in orders.values() for o in arr]

    def to_json(self, v):
        return json.dumps(v, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value, max_length):
        lo, hi = 0, min(len(value), max_length)
        out = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = value[:mid]
            if len(candidate) < len(value):
                candidate += "..."
            if len(json.dumps(candidate)) <= max_length:
                out = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return out


logger = Logger()


# =============================================================================
# BOOK HELPERS
# =============================================================================
def best_bid_ask(od: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    bid = max(od.buy_orders) if od.buy_orders else None
    ask = min(od.sell_orders) if od.sell_orders else None
    return bid, ask


def vw_mid(od: OrderDepth) -> Optional[float]:
    """Volume-weighted mid using top-of-book sizes; falls back to plain mid."""
    bid, ask = best_bid_ask(od)
    if bid is None or ask is None:
        return None
    bv = od.buy_orders[bid]
    av = -od.sell_orders[ask]
    if bv + av <= 0:
        return (bid + ask) / 2.0
    return (bid * av + ask * bv) / (bv + av)


# =============================================================================
# FAIR-VALUE ESTIMATORS
# =============================================================================
def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S: float, K: int, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K)
    sd = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sd
    return S * _N(d1) - K * _N(d1 - sd)


def bs_delta(S: float, K: int, T: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if S >= K else 0.0
    sd = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / sd
    return _N(d1)


def t_for_run(game_day: int) -> float:
    days = TTE_DAYS.get(LIVE_DAY_OFFSET + game_day, 1)
    return max(0.5, days) / 250


def synthetic_S(state: TradingState) -> Optional[float]:
    """
    Synthetic VEV from deep-ITM mids: average of (VEV_4000+4000) and
    (VEV_4500+4500).  Empirically leads VEV's own mid by 1-5 ticks
    (corr +0.26 at lag 1).
    """
    parts = []
    for K, sym in ((4000, VEV_4000), (4500, VEV_4500)):
        od = state.order_depths.get(sym)
        if od is not None:
            m = vw_mid(od)
            if m is not None:
                parts.append(m + K)
    if not parts:
        return None
    return sum(parts) / len(parts)


def estimate_S(state: TradingState) -> Optional[float]:
    """Best estimate of the underlying VEV price (blended)."""
    od = state.order_depths.get(VEV)
    vev_mid = vw_mid(od) if od is not None else None
    syn = synthetic_S(state)

    if vev_mid is not None and syn is not None:
        # Blend toward synthetic (the leading indicator)
        return SYNTH_WEIGHT * syn + (1 - SYNTH_WEIGHT) * vev_mid
    if vev_mid is not None:
        return vev_mid
    return syn


def fair_value(sym: str, state: TradingState, S: Optional[float],
               T: float) -> Optional[float]:
    od = state.order_depths.get(sym)
    if od is None:
        return None

    if sym == HYDRO:
        m = vw_mid(od)
        if m is None:
            return float(HYDRO_ANCHOR)
        return 0.7 * m + 0.3 * HYDRO_ANCHOR

    if sym == VEV:
        return S if S is not None else vw_mid(od)

    if sym == VEV_4000:
        raw_S = vw_mid(state.order_depths.get(VEV))
        return (raw_S - 4000) if raw_S is not None else vw_mid(od)
    if sym == VEV_4500:
        raw_S = vw_mid(state.order_depths.get(VEV))
        return (raw_S - 4500) if raw_S is not None else vw_mid(od)

    # Active vouchers: BS at smile-σ vs blended S
    if sym.startswith("VEV_"):
        K = int(sym.split("_")[1])
        if K in ACTIVE_STRIKES and S is not None:
            sigma = SMILE.get(K, SIGMA_FAIR)
            return bs_call(S, K, T, sigma)
    return None


# =============================================================================
# QUOTE GENERATION (skew MM + delta-budget gate)
# =============================================================================
def quote_product(sym: str, state: TradingState, S: Optional[float],
                  T: float, combined_delta: int) -> List[Order]:
    """
    Skew-aware market-making for one product.  Suppresses the side that would
    breach the combined-Δ budget for delta-1 / call-option products.
    """
    od = state.order_depths.get(sym)
    if od is None:
        return []

    bids = sorted(od.buy_orders.items(),  reverse=True)
    asks = sorted(od.sell_orders.items())
    if not bids or not asks:
        return []

    best_bid, best_ask = bids[0][0], asks[0][0]

    fair = fair_value(sym, state, S, T)
    if fair is None:
        return []

    position = state.position.get(sym, 0)
    limit = POS_LIMIT[sym]
    soft = SOFT_CAP[sym]
    edge = MAKE_EDGE[sym]
    skew = SKEW[sym] * position
    size = QUOTE_SIZE[sym]

    skewed_fair = fair - skew
    skewed_int = int(round(skewed_fair))

    # ── TAKE: cross when book is on the wrong side of the *un-skewed* fair ──
    fair_int = int(round(fair))
    orders: List[Order] = []
    buy_room = max(0, min(limit, soft) - position)
    sell_room = max(0, min(limit, soft) + position)

    # Suppress directional add if it would breach the combined Δ budget.
    # Delta-1 instruments and active calls both contribute long delta when held.
    is_delta_contrib = (
        sym in (VEV, VEV_4000, VEV_4500)
        or (sym.startswith("VEV_")
            and int(sym.split("_")[1]) in ACTIVE_STRIKES)
    )
    if is_delta_contrib:
        if combined_delta >= DELTA_BUDGET:
            buy_room = 0
        if combined_delta <= -DELTA_BUDGET:
            sell_room = 0

    # Standard fair-edge takes (1-tick edge)
    for ask_price, ask_vol in asks:
        if buy_room <= 0:
            break
        if ask_price <= fair_int - 1:
            qty = min(-ask_vol, buy_room)
            orders.append(Order(sym, ask_price, qty))
            buy_room -= qty

    for bid_price, bid_vol in bids:
        if sell_room <= 0:
            break
        if bid_price >= fair_int + 1:
            qty = min(bid_vol, sell_room)
            orders.append(Order(sym, bid_price, -qty))
            sell_room -= qty

    # NOTE: signal-take used to fire here, but it swept full book sizes at
    # full spread cost (~3-5 ticks) for a predicted ~1.2-tick move.  Negative
    # EV in cross-fill conditions.  The synthetic still informs fair_value
    # via the SYNTH_WEIGHT blend in estimate_S, which biases our passive
    # quotes in the right direction without paying spread on entry.

    # ── MAKE: post strictly inside the book around skewed fair ──────────────
    bid_size = min(size, buy_room)
    ask_size = min(size, sell_room)

    my_bid = skewed_int - edge
    my_ask = skewed_int + edge

    # Stay inside the existing book
    if my_bid >= best_ask:
        my_bid = best_ask - 1
    if my_ask <= best_bid:
        my_ask = best_bid + 1
    # Never quote bid > skewed fair (would self-pick our own asks on the next
    # tick) or ask < skewed fair
    if my_bid > skewed_int:
        my_bid = skewed_int
    if my_ask < skewed_int + 1:
        my_ask = skewed_int + 1

    if bid_size > 0 and my_bid > 0:
        orders.append(Order(sym, my_bid, bid_size))
    if ask_size > 0:
        orders.append(Order(sym, my_ask, -ask_size))

    return orders


# =============================================================================
# TRADER ENTRY
# =============================================================================
class Trader:
    def run(self, state: TradingState):
        # ── Persist game-day across timestamp resets ─────────────────────────
        try:
            td = json.loads(state.traderData) if state.traderData else {}
        except Exception:
            td = {}
        prev_ts = td.get("prev_ts", -1)
        game_day = td.get("game_day", 0)
        if state.timestamp == 0 and prev_ts > 0:
            game_day += 1
        td["prev_ts"] = state.timestamp
        td["game_day"] = game_day
        T = t_for_run(game_day)

        S = estimate_S(state)

        # ── Combined Δ: delta-1 positions + active-option Δ from BS ──────────
        combined_delta = (
            state.position.get(VEV, 0)
            + state.position.get(VEV_4000, 0)
            + state.position.get(VEV_4500, 0)
        )
        if S is not None:
            for K in ACTIVE_STRIKES:
                pos = state.position.get(f"VEV_{K}", 0)
                if pos:
                    sigma = SMILE.get(K, SIGMA_FAIR)
                    combined_delta += int(round(pos *
                                          bs_delta(S, K, T, sigma)))

        # ── Generate orders for every traded product ─────────────────────────
        result: Dict[Symbol, List[Order]] = {}
        for sym in TRADED:
            ords = quote_product(sym, state, S, T, combined_delta)
            if ords:
                result[sym] = ords

        trader_data = json.dumps(td)
        try:
            logger.flush(state, result, 0, trader_data)
        except Exception:
            pass

        return result, 0, trader_data
