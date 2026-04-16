"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v6 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — sinusoidal wave around ~10,000

HISTORY OF RESULTS:
    v3: 6,294 PnL  — PEPPER 5,089 + OSMIUM 1,205
    v4: 2,900 PnL  — REGRESSION (posted asks during accumulation, tightened OSMIUM)
    v5: ~6,500 est  — v3 base + faster PEPPER accumulation (fair+4, bid+2)

V6 CHANGES — TWO KEY IMPROVEMENTS:

    1. PEPPER FAIR VALUE BIAS CORRECTION (+1.5)
       =========================================
       Data analysis revealed our fair value model (day_start + 0.001*t)
       UNDERESTIMATES the actual mid-price by +1.51 on average (max +8.1).
       This means:
         - During accumulation, our "fair+4" sweep is actually only
           sweeping asks up to true_fair+2.5 → missing fills
         - Our passive bid at "fair+2" is actually at true_fair+0.5 →
           sitting too low in the queue
         - Our take-profit threshold of "fair+3" triggers on bids that
           are only at true_fair+1.5 → selling too eagerly at +80

       FIX: Add +1.5 bias to fair value.
         fair = day_start + 0.001*t + 1.5
       This makes all our thresholds align with the actual market:
         - Sweep at true_fair+4 (was true_fair+2.5) → more fills
         - Passive bid at true_fair+2 (was true_fair+0.5) → better queue position
         - Take-profit at true_fair+3 (was true_fair+1.5) → only sell truly overpriced

    2. OSMIUM WAVE-RIDING (the "hidden pattern")
       ==========================================
       Data analysis revealed OSMIUM is NOT just noise around 10,000.
       There is a SINUSOIDAL WAVE:
         - Amplitude: ~5 points (range 9995 to 10005)
         - Cycle period: ~300 ticks (~30,000 timestamps)
         - Autocorrelation: 0.72 at lag 1 — highly persistent
         - Caused by a second market-maker (MM2) that alternates
           between buy and sell phases

       OLD APPROACH (v3-v5): Static fair=10000, soft limit 30, inventory
       skew only. Earned 1,205/day. Completely ignored the wave.

       NEW APPROACH: Track the wave with an EMA and lean INTO it:
         - EMA of mid-prices (alpha=0.05, ~20-tick smoothing)
         - Compute a "target position" based on where the wave is:
           when EMA > 10000 → target is long (ride the wave up)
           when EMA < 10000 → target is short (ride the wave down)
         - Skew quotes around the TARGET, not around zero
         - Increase soft limit from 30 → 50 (more capacity for wave-riding)
         - Still take all mispriced orders vs 10000 (the long-term anchor)

       WHY THIS WORKS: The wave is persistent (autocorr 0.72). When the
       price is above 10000, it tends to STAY above 10000 for 40-70 ticks.
       By leaning long during up-phases and short during down-phases, we
       capture ~80% of the wave amplitude with passive fills only.
       With 50 units and 5-point amplitude: 50 * 5 * 3 cycles/day = ~750
       Plus the existing MM spread income (~1,200) = ~2,000-3,000 total.

EXPECTED PnL:
    PEPPER: ~6,000-6,500 (bias correction → faster accumulation + better MM)
    OSMIUM: ~2,500-3,500 (wave-riding + larger positions)
    TOTAL:  ~8,500-10,000

Position limits: 80 for both products.
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# =============================================================================
# POSITION LIMITS — from the round 1 rules, both products capped at 80
# =============================================================================
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS — standard IMC Prosperity visualizer boilerplate
# =============================================================================
class Logger:
    def __init__(self) -> None:
        self.logs = ""
        self.max_log_length = 3750

    def print(self, *objects: Any, sep: str = " ", end: str = "\n") -> None:
        self.logs += sep.join(map(str, objects)) + end

    def flush(self, state: TradingState, orders: dict[Symbol, list[Order]],
              conversions: int, trader_data: str) -> None:
        base_length = len(
            self.to_json([
                self.compress_state(state, ""),
                self.compress_orders(orders),
                conversions, "", "",
            ])
        )
        max_item_length = (self.max_log_length - base_length) // 3
        print(
            self.to_json([
                self.compress_state(state, self.truncate(state.traderData, max_item_length)),
                self.compress_orders(orders),
                conversions,
                self.truncate(trader_data, max_item_length),
                self.truncate(self.logs, max_item_length),
            ])
        )
        self.logs = ""

    def compress_state(self, state: TradingState, trader_data: str) -> list[Any]:
        return [
            state.timestamp, trader_data,
            self.compress_listings(state.listings),
            self.compress_order_depths(state.order_depths),
            self.compress_trades(state.own_trades),
            self.compress_trades(state.market_trades),
            state.position,
            self.compress_observations(state.observations),
        ]

    def compress_listings(self, listings: dict[Symbol, Listing]) -> list[list[Any]]:
        return [[l.symbol, l.product, l.denomination] for l in listings.values()]

    def compress_order_depths(self, order_depths: dict[Symbol, OrderDepth]) -> dict[Symbol, list[Any]]:
        return {s: [od.buy_orders, od.sell_orders] for s, od in order_depths.items()}

    def compress_trades(self, trades: dict[Symbol, list[Trade]]) -> list[list[Any]]:
        compressed = []
        for arr in trades.values():
            for t in arr:
                compressed.append([t.symbol, t.price, t.quantity, t.buyer, t.seller, t.timestamp])
        return compressed

    def compress_observations(self, observations: Observation) -> list[Any]:
        conversion_observations = {}
        try:
            for product, obs in observations.conversionObservations.items():
                conversion_observations[product] = [
                    obs.bidPrice, obs.askPrice, obs.transportFees,
                    obs.exportTariff, obs.importTariff,
                    getattr(obs, "sugarPrice", 0),
                    getattr(obs, "sunlightIndex", 0),
                ]
        except Exception:
            pass
        return [getattr(observations, "plainValueObservations", {}), conversion_observations]

    def compress_orders(self, orders: dict[Symbol, list[Order]]) -> list[list[Any]]:
        compressed = []
        for arr in orders.values():
            for o in arr:
                compressed.append([o.symbol, o.price, o.quantity])
        return compressed

    def to_json(self, value: Any) -> str:
        return json.dumps(value, cls=ProsperityEncoder, separators=(",", ":"))

    def truncate(self, value: str, max_length: int) -> str:
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
# TRADER CLASS
# =============================================================================
class Trader:
    """
    Round 1 v6 strategy:
        PEPPER  → Directional accumulation with bias-corrected fair value
        OSMIUM  → Wave-riding market-making with EMA-tracked dynamic fair
    """

    # =========================================================================
    # PEPPER CONFIG
    # =========================================================================

    # Linear drift rate: +0.001 per timestamp = +1,000 per day.
    PEPPER_SLOPE = 0.001

    # NEW IN V6: Fair value bias correction.
    #
    # Our linear model (day_start + 0.001*t) underestimates the actual
    # mid-price by +1.51 on average (measured across 1,000 ticks of v3 data).
    # The max deviation was +8.1.
    #
    # This bias means all our price thresholds are 1.5 too low:
    #   - We think "fair+4" but the market sees it as "true_fair+2.5"
    #   - We think "fair+2 passive bid" but it's really "true_fair+0.5"
    #
    # By adding 1.5 to our fair, all thresholds align with reality.
    # We round to 2 (integer prices) to be slightly aggressive rather
    # than slightly conservative — accumulation speed matters more.
    PEPPER_FAIR_BIAS = 2

    # Maximum overpay above corrected fair to sweep asks.
    # With the bias fix, this is now truly fair+4 (not fair+2.5).
    PEPPER_MAX_OVERPAY = 4

    # Passive bid premium during accumulation.
    # With bias fix, this is now truly fair+2 (not fair+0.5).
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # Market-making config once at +80 (unchanged from v5).
    PEPPER_MM_OFFSET = 2
    PEPPER_MM_SIZE = 10
    PEPPER_TAKE_PROFIT_THRESHOLD = 3

    # =========================================================================
    # OSMIUM CONFIG — MAJOR OVERHAUL IN V6
    # =========================================================================

    # Long-term anchor: the true mean of OSMIUM across all days.
    # We still use this for taking mispriced orders (asks < 10000, bids > 10000).
    # These are "free money" regardless of the wave phase.
    OSMIUM_ANCHOR = 10000

    # EMA smoothing factor for tracking the wave.
    #
    # alpha = 0.05 → effective window of ~20 ticks (1/0.05 = 20).
    # The wave cycle is ~300 ticks, so a 20-tick EMA:
    #   - Smooths out tick-to-tick noise (std ~2.5)
    #   - Tracks the wave with ~13 ticks of lag (half-life = ln2/ln(1/0.95) ≈ 13.5)
    #   - 13-tick lag on a 150-tick half-cycle = 9% lag → acceptable
    #
    # Why not faster (alpha=0.1)?  Too noisy, would whipsaw our quotes.
    # Why not slower (alpha=0.02)? Too much lag, would miss the wave peaks.
    OSMIUM_EMA_ALPHA = 0.05

    # Soft position cap — increased from 30 (v5) to 50 (v6).
    #
    # v5 used 30 because it treated OSMIUM as low-alpha noise.
    # v6 uses 50 because the wave gives us a directional edge:
    #   - At ±50 with 5-point wave amplitude: 50 * 5 = 250 per half-cycle
    #   - ~3 full cycles per day: 250 * 2 * 3 = 1,500 from wave-riding alone
    #   - Plus ~1,200 from spread capture
    #   - Not going full ±80 because the wave can be noisy and we don't
    #     want a blowup in a bad cycle to wipe all gains.
    OSMIUM_SOFT_LIMIT = 50

    # Target position as a fraction of soft limit, based on wave signal.
    #
    # When EMA is 3+ points above 10000, we want our target position to be
    # at MAX_WAVE_TARGET_RATIO * SOFT_LIMIT = 0.6 * 50 = 30 units long.
    # The wave amplitude is ~5, and we normalize by WAVE_SCALE (3.0),
    # so at EMA=10003 we're at full wave target.
    #
    # Why 0.6 and not 1.0? We want to leave room for the inventory skew
    # to work. If target = soft_limit, the skew has no room to mean-revert.
    # At 0.6, the skew operates in the remaining 40% of capacity.
    OSMIUM_MAX_WAVE_TARGET_RATIO = 0.6
    OSMIUM_WAVE_SCALE = 3.0  # EMA deviation at which we're at full wave target

    # Quote offset (half-spread) — unchanged from v5.
    # Still 3 ticks from dynamic fair. v4 proved 2 is worse.
    OSMIUM_QUOTE_OFFSET = 3

    # Inventory skew factor — now operates around TARGET POSITION, not zero.
    #
    # In v5: skew was based on (position / soft_limit)
    #   → always tried to push position toward 0
    #
    # In v6: skew is based on (position - target) / soft_limit
    #   → pushes position toward the wave-derived target
    #   → when wave says "be long 30", we quote as if 30 is neutral
    #
    # Example at EMA=10002 (target=+20), position=+10:
    #   deviation = (10 - 20) / 50 = -0.2
    #   bid_offset = 3 + 4*(-0.2) = 2.2 → tight bid (eager to buy toward target)
    #   ask_offset = 3 - 4*(-0.2) = 3.8 → wide ask (reluctant to sell away from target)
    OSMIUM_SKEW_FACTOR = 4

    # =========================================================================
    # RUN — called once per tick by the exchange
    # =========================================================================
    def run(self, state: TradingState):

        # =====================================================================
        # STEP 0: RESTORE PERSISTED STATE
        # =====================================================================
        # We persist across ticks via state.traderData (JSON string).
        #
        # Stored variables:
        #   pepper_day_start_price — mid-price at first tick of day (PEPPER intercept)
        #   pepper_last_timestamp  — for detecting day boundaries
        #   osmium_ema             — EMA of OSMIUM mid-prices (tracks the wave)
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)
        osmium_ema = stored.get("osmium_ema", None)

        # =====================================================================
        # STEP 1: PROCESS EACH PRODUCT
        # =====================================================================
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []

            # ----- Position bookkeeping -----
            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position      # max additional units we can buy
            sell_room = limit + position      # max additional units we can sell

            # ----- Read the order book -----
            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            if best_bid is None or best_ask is None:
                result[product] = orders
                continue

            mid_price = (best_bid + best_ask) / 2

            # =================================================================
            # INTARIAN_PEPPER_ROOT — DIRECTIONAL ACCUMULATION
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                # --- Day boundary detection ---
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True

                if is_new_day:
                    pepper_day_start_price = mid_price

                pepper_last_timestamp = state.timestamp

                # --- Fair value: linear ramp + BIAS CORRECTION (NEW IN V6) ---
                #
                # Base model:  fair = day_start + 0.001 * timestamp
                # Bias fix:    fair += 2
                #
                # The base model underestimates by +1.51 on average because the
                # bots' mid-price consistently runs above our linear projection.
                # We add +2 (rounded up from 1.51) to correct this.
                #
                # Impact on each phase:
                #   Accumulation sweep: max_buy = (fair+2) + 4 = true_fair+6 → more fills
                #   Accumulation bid:   (fair+2) + 2 = true_fair+4 → better queue position
                #   Hold MM ask:        (fair+2) + 2 = true_fair+4 → fewer but better fills
                #   Take-profit:        (fair+2) + 3 = true_fair+5 → only truly overpriced
                fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp + self.PEPPER_FAIR_BIAS
                fair_int = round(fair)

                # =============================================================
                # PHASE 1: AGGRESSIVE ACCUMULATION (position < 80)
                # =============================================================
                # GOAL: Reach +80 as fast as possible.
                # RULE: NEVER post an ask during accumulation (v4 lesson).
                #
                # The bias correction makes this phase more aggressive:
                # we now sweep asks up to true_fair+6 and bid at true_fair+4.
                # This sounds expensive, but the drift pays for it quickly:
                #   Extra cost: ~2 per unit * 80 = 160
                #   1,000 ticks at +80 earns: 80 * 0.001 * 1000 = 80
                #   Breakeven: ~2,000 ticks (2% of day)

                if position < limit:
                    # Sweep all asks up to fair + overpay
                    max_buy_price = fair_int + self.PEPPER_MAX_OVERPAY

                    for ask_price, ask_vol in bot_asks:
                        if ask_price <= max_buy_price and buy_room > 0:
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            break

                    # Post passive bid to catch incoming sells
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                    # NO ASKS. EVER. DURING ACCUMULATION.

                # =============================================================
                # PHASE 2: HOLD + LIGHT MM (position = 80)
                # =============================================================
                else:
                    # Light market-making with 10-unit slice
                    mm_size = min(self.PEPPER_MM_SIZE, sell_room)
                    if mm_size > 0:
                        my_ask = fair_int + self.PEPPER_MM_OFFSET
                        orders.append(Order(product, my_ask, -mm_size))

                    # Bid to refill if we dipped below 80
                    if buy_room > 0:
                        my_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, my_bid, buy_room))

                # Take-profit: sell into very overpriced bids (only at +80)
                if position >= limit:
                    for bid_price, bid_vol in bot_bids:
                        if bid_price >= fair_int + self.PEPPER_TAKE_PROFIT_THRESHOLD and sell_room > 0:
                            take_qty = min(bid_vol, sell_room, self.PEPPER_MM_SIZE)
                            orders.append(Order(product, bid_price, -take_qty))
                            sell_room -= take_qty
                        else:
                            break

            # =================================================================
            # ASH_COATED_OSMIUM — WAVE-RIDING MARKET-MAKING (NEW IN V6)
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # =============================================================
                # STEP A: UPDATE THE EMA (track the wave)
                # =============================================================
                # The EMA of mid-prices is our "dynamic fair value."
                # It smooths out tick-to-tick noise while tracking the
                # slow sinusoidal wave that cycles every ~300 ticks.
                #
                # EMA formula: ema = alpha * new_value + (1 - alpha) * old_ema
                #   alpha = 0.05 → responds to ~5% of each new data point
                #   Half-life: ~13 ticks → smooth enough to ignore noise,
                #   fast enough to track 300-tick wave
                #
                # On the very first tick, we initialize the EMA to mid_price.
                if osmium_ema is None:
                    osmium_ema = mid_price
                else:
                    osmium_ema = self.OSMIUM_EMA_ALPHA * mid_price + (1.0 - self.OSMIUM_EMA_ALPHA) * osmium_ema

                # =============================================================
                # STEP B: COMPUTE WAVE-DERIVED TARGET POSITION
                # =============================================================
                # The wave signal tells us which phase we're in:
                #   ema > 10000 → price trending above anchor → we want to be LONG
                #   ema < 10000 → price trending below anchor → we want to be SHORT
                #
                # wave_signal: ranges from -1.0 to +1.0
                #   +1.0 when EMA is 3+ points above 10000 (strong uptrend)
                #   -1.0 when EMA is 3+ points below 10000 (strong downtrend)
                #    0.0 when EMA is exactly at 10000 (no trend)
                #
                # We normalize by WAVE_SCALE (3.0) because the wave amplitude
                # is ~5 points, and we want to be at full target before the peak.
                # At ±3 points we're already ~60% through the half-cycle.
                wave_deviation = osmium_ema - self.OSMIUM_ANCHOR
                wave_signal = max(-1.0, min(1.0, wave_deviation / self.OSMIUM_WAVE_SCALE))

                # target_position: where we WANT to be, based on the wave.
                #   At wave_signal = +1.0: target = +0.6 * 50 = +30 (long)
                #   At wave_signal = -1.0: target = -0.6 * 50 = -30 (short)
                #   At wave_signal =  0.0: target = 0 (neutral)
                #
                # We cap at 60% of soft_limit to leave room for the inventory
                # skew to operate. If target = soft_limit, we'd be fighting
                # the skew instead of working with it.
                target_position = wave_signal * self.OSMIUM_MAX_WAVE_TARGET_RATIO * self.OSMIUM_SOFT_LIMIT

                # =============================================================
                # STEP C: COMPUTE INVENTORY-SKEWED QUOTE OFFSETS
                # =============================================================
                # The skew pushes our position TOWARD the target (not toward zero).
                #
                # deviation_ratio: how far we are from target, normalized to [-1, 1]
                #   Positive = we're ABOVE target → sell more eagerly
                #   Negative = we're BELOW target → buy more eagerly
                #   Zero = we're AT target → symmetric quotes
                soft_limit = self.OSMIUM_SOFT_LIMIT
                deviation = position - target_position
                deviation_ratio = deviation / soft_limit if soft_limit > 0 else 0
                deviation_ratio = max(-1.0, min(1.0, deviation_ratio))

                # bid_offset: how far below dynamic_fair we bid
                #   When above target (deviation_ratio > 0): widen bid → buy less
                #   When below target (deviation_ratio < 0): tighten bid → buy more
                #
                # ask_offset: how far above dynamic_fair we ask
                #   When above target (deviation_ratio > 0): tighten ask → sell more
                #   When below target (deviation_ratio < 0): widen ask → sell less
                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * deviation_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * deviation_ratio

                # Minimum offset of 1: never bid at/above fair or ask at/below fair
                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # =============================================================
                # STEP D: DYNAMIC FAIR VALUE FOR QUOTING
                # =============================================================
                # We use the EMA as our quoting center — this is the key change.
                #
                # v5 quoted around 10000 always. v6 quotes around the EMA.
                # When the wave pushes mid to 10003, our EMA follows to ~10002,
                # and we quote around 10002 instead of 10000. This means:
                #   - Our bid sits at ~10002-3 = 9999 (inside the wave)
                #   - Our ask sits at ~10002+3 = 10005 (at the wave peak)
                # vs v5 which would bid at 9997 and ask at 10003 → misaligned.
                #
                # We round the EMA to get integer prices.
                dynamic_fair = round(osmium_ema)

                # =============================================================
                # STEP E: TAKE MISPRICED ORDERS (vs the ANCHOR, not the EMA)
                # =============================================================
                # We use the long-term anchor (10000) for taking, not the EMA.
                #
                # WHY: An ask at 9998 is ALWAYS cheap, regardless of the wave.
                # The wave is a short-term phenomenon — the price WILL return
                # to 10000 eventually. Taking at 9998 is 2 points of guaranteed
                # profit once it mean-reverts.
                #
                # If we used the EMA for taking (e.g., EMA=9998, take asks < 9998),
                # we'd miss the 9998 ask because it's AT our dynamic fair.
                # That's leaving free money on the table.
                anchor = self.OSMIUM_ANCHOR
                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                # Sweep asks below 10000 (guaranteed cheap)
                for ask_price, ask_vol in bot_asks:
                    if ask_price < anchor and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == anchor and soft_buy_room > 0 and position < target_position:
                        # At exactly 10000: only buy if we're BELOW our wave target.
                        # This lets the wave bias influence even the "at-fair" decisions.
                        # If wave says "be long" and we're not long enough, buy at 10000.
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                # Sweep bids above 10000 (guaranteed expensive)
                for bid_price, bid_vol in bot_bids:
                    if bid_price > anchor and soft_sell_room > 0:
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == anchor and soft_sell_room > 0 and position > target_position:
                        # At exactly 10000: only sell if we're ABOVE our wave target.
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # =============================================================
                # STEP F: POST PASSIVE QUOTES (around DYNAMIC FAIR)
                # =============================================================
                # Quotes are centered on the EMA (dynamic_fair), not on 10000.
                # The skew-adjusted offsets push us toward the wave target.
                my_bid = dynamic_fair - bid_offset
                my_ask = dynamic_fair + ask_offset

                # Safety: never bid at/above dynamic fair, never ask at/below
                my_bid = min(my_bid, dynamic_fair - 1)
                my_ask = max(my_ask, dynamic_fair + 1)

                # Recalculate room after taking (position may have changed)
                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                if soft_buy_room > 0:
                    orders.append(Order(product, my_bid, soft_buy_room))
                if soft_sell_room > 0:
                    orders.append(Order(product, my_ask, -soft_sell_room))

            # =================================================================
            # UNKNOWN PRODUCT — safe fallback
            # =================================================================
            else:
                fair_int = round(mid_price)
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and buy_room > 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                    else:
                        break
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and sell_room > 0:
                        take_qty = min(bid_vol, sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                    else:
                        break
                my_bid = min(best_bid + 1, fair_int - 1)
                my_ask = max(best_ask - 1, fair_int + 1)
                if buy_room > 0:
                    orders.append(Order(product, my_bid, buy_room))
                if sell_room > 0:
                    orders.append(Order(product, my_ask, -sell_room))

            result[product] = orders

        # =====================================================================
        # STEP 2: PERSIST STATE FOR NEXT TICK
        # =====================================================================
        trader_data = json.dumps({
            "pepper_day_start_price": pepper_day_start_price,
            "pepper_last_timestamp": pepper_last_timestamp,
            "osmium_ema": osmium_ema,  # NEW: persisted EMA for wave tracking
        })

        conversions = 0

        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
