"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v5 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000

HISTORY OF RESULTS:
    v3: 6,294 PnL  — PEPPER 5,089 + OSMIUM 1,205
        Good: correct directional strategy for PEPPER, solid OSMIUM MM
        Bad:  took until ts=65,700 (66% of day!) to reach +80 PEPPER position
              because sweep threshold was fair+3 and passive bid at fair+1

    v4: 2,900 PnL  — MASSIVE REGRESSION
        Fatal mistake: posted an ask at fair+5 during accumulation phase.
        The ask filled CONSTANTLY (bots bid 4-6 above our fair model),
        causing v4 to go SHORT (-13) early and never recover.
        Time-weighted avg position: 10.9 vs v3's 48.3.
        Also tightened OSMIUM offset 3→2 which lost price quality with
        zero fill-rate improvement. Pure downside.
        LESSON: NEVER post sells during accumulation on a trending asset.

V5 CHANGES (minimal, surgical improvements to proven v3 base):
    PEPPER:
        - Increase sweep threshold from fair+3 → fair+4
          Cost: 1 extra per unit × 80 = 80 more in entry cost (noise)
          Benefit: fills more asks per tick → reaches +80 faster
          Reaching +80 even 10,000 ticks sooner = 80 * 0.001 * 10,000 = 800 extra PnL

        - Increase passive bid from fair+1 → fair+2
          When there are no asks to sweep, we post a passive bid.
          At fair+2 instead of fair+1, we sit higher in the queue and
          catch more incoming sells. Again: speed of accumulation matters
          more than saving 1 tick on entry price.

        - Everything else IDENTICAL to v3:
          * NO sells during accumulation (the v4 lesson)
          * Light MM at +80 with 10-unit slice
          * Same fair value model (linear ramp from day start)

    OSMIUM:
        - IDENTICAL to v3. Offset 3, soft limit 30, skew 4.
        - v4 proved that tightening to offset 2 was pure downside.
        - Don't fix what isn't broken.

EXPECTED PnL: v3 baseline 6,294 + faster accumulation ~800-1,200 = ~7,000-7,500

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
# LOGGER CLASS
# =============================================================================
# Standard IMC Prosperity visualizer helper. This is boilerplate — it formats
# our state/orders/logs into compressed JSON that the web visualizer can parse.
# Not strategy logic. Every team uses this verbatim.
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
    Round 1 v5 strategy — two products, two approaches:

    =========================================================================
    INTARIAN_PEPPER_ROOT — DIRECTIONAL ACCUMULATION (improved from v3)
    =========================================================================
    WHY DIRECTIONAL:
        Price drifts linearly at +0.001 per timestamp = +1,000 per day.
        Theoretical max (perfect-foresight DP) is ~7,500/day.
        92% of all available profit comes from this product's drift.
        The order book is efficiently priced — no free money sitting
        in the book. All profit requires holding a long position and
        riding the upward trend.

    HOW:
        Phase 1 — ACCUMULATE (position < 80):
            - Aggressively sweep all asks up to fair + 4
              (v3 used fair+3, we pay 1 more per unit for faster fills)
            - Post a passive bid at fair + 2 to catch incoming sells
              (v3 used fair+1, we bid 1 higher for more fill probability)
            - ABSOLUTELY NO ASKS during this phase. v4 proved that
              posting even a "wide" ask (fair+5) gets filled constantly
              by bots that bid above our fair model, causing us to go
              short and miss the entire upward drift. Never again.
            - Goal: reach +80 as fast as possible. Every 100 ticks
              below +80 costs us 80 * 0.001 * 100 = 8 in missed drift.

        Phase 2 — HOLD + LIGHT MM (position = 80):
            - The drift earns us ~0.08 per tick automatically (80 * 0.001).
            - We add a tiny market-making overlay with 10 units:
              sell 10 at fair+2, buy back at fair+1. Earn ~1-2 per cycle.
            - If an MM sell fills and we drop below 80, bid aggressively
              at fair+2 to refill ASAP.
            - Also take any bids at fair+3 or higher (rare free money).
            - Cap sell size to 10 to never significantly reduce our long.

    FAIR VALUE MODEL:
        fair(t) = day_start_mid + 0.001 * timestamp
        We record the mid-price at the first tick of each day, then
        project linearly. This tracks within ±2 of actual mid-price.

    =========================================================================
    ASH_COATED_OSMIUM — CONSERVATIVE MARKET-MAKING (identical to v3)
    =========================================================================
    WHY CONSERVATIVE:
        Mean-reverts around ~10,000 with zero drift. Range is only ~25
        points, spread ~16. Theoretical max is ~550/day. This is a
        sideshow product — we want steady small gains, not blowups.

    HOW:
        - Hardcoded fair = 10,000 (stable across all historical days)
        - Soft position cap at ±30 (not the full ±80) to limit risk
        - Take any mispriced orders (asks < 10k, bids > 10k)
        - Post passive quotes at fair ± 3, skewed by inventory
        - At max position (+30), skew widens the overloaded side to
          offset 7 and tightens the unwind side to offset 1
        - v4 tried tightening to offset 2 — same fill rate, worse
          prices. Offset 3 is the sweet spot. Don't change it.
    =========================================================================
    """

    # -------------------------------------------------------------------------
    # PEPPER CONFIG
    # -------------------------------------------------------------------------

    # The linear drift rate: +0.001 per timestamp unit.
    # +1 every 1,000 timestamps, +1,000 over a full 100,000-tick day.
    # Confirmed via linear regression across 3 days of historical data.
    PEPPER_SLOPE = 0.001

    # Maximum overpay above fair to sweep asks during accumulation.
    #
    # v3 used 3 — reached +80 by ts=65,700 (66% of day).
    # v5 uses 4 — we pay 1 extra per unit for faster fills.
    #
    # Cost of overpay:  4 extra per unit * 80 units = 320 total entry cost
    # Cost of slowness:  if we reach +80 10,000 ticks sooner,
    #                    we gain 80 * 0.001 * 10,000 = 800 in drift PnL
    #
    # The math is clear: paying 320 to gain 800 is a 2.5x return.
    # Speed of accumulation >> precision of entry.
    PEPPER_MAX_OVERPAY = 4

    # Passive bid premium during accumulation.
    # When there are no asks to sweep, we post a resting bid.
    # At fair+2 (vs v3's fair+1), we sit higher in the queue and
    # intercept more incoming sells.
    #
    # The 1-tick cost (buying at fair+2 vs fair+1) is negligible
    # compared to the drift benefit of filling 1 tick sooner.
    PEPPER_PASSIVE_BID_PREMIUM = 2

    # Market-making offset once at +80. Post ask at fair+2.
    # When we sell 10 units at fair+2 and buy them back at fair+1,
    # we earn 1 tick per unit per round-trip = 10 per cycle.
    # Meanwhile the other 70 units keep riding the drift.
    PEPPER_MM_OFFSET = 2

    # How many units to market-make with at +80.
    # 10 is small enough that even if we sell and can't rebuy for
    # a few ticks, we only miss 10 * 0.001 = 0.01 per tick of drift.
    # That's noise compared to the 70 * 0.001 = 0.07 the held units earn.
    PEPPER_MM_SIZE = 10

    # Minimum bid premium above fair to sell into (only at max position).
    # At +80, if someone bids at fair+3 or higher, that's rare free money.
    # We sell a small amount into it and rebuy next tick.
    PEPPER_TAKE_PROFIT_THRESHOLD = 3

    # -------------------------------------------------------------------------
    # OSMIUM CONFIG — IDENTICAL TO V3, proven to work (1,205/day)
    # -------------------------------------------------------------------------

    # Hardcoded fair value. The true center is ~10,000 across all days.
    # Hardcoding is better than EMA because:
    #   - EMA lagged in v2, causing adverse selection
    #   - Historical means: 9998, 10001, 10002 — all effectively 10,000
    #   - No risk of the fair estimate drifting with noise
    OSMIUM_FAIR = 10000

    # Soft position cap. Exchange allows 80, we self-impose 30.
    # With only ~550 theoretical max/day, a blowup from a large
    # position can wipe all OSMIUM PnL. Cap keeps risk bounded.
    OSMIUM_SOFT_LIMIT = 30

    # Quote offset (half-spread) for passive orders.
    # Bot spread is ~16 (half=8). Our offset of 3 puts us well
    # inside the bots while maintaining a 6-tick round-trip profit.
    # v4 tried offset 2 — same fill rate, worse prices. Don't touch.
    OSMIUM_QUOTE_OFFSET = 3

    # Inventory skew factor.
    # At max long (+30): bid offset = 3 + 4*1.0 = 7 (barely buying)
    #                     ask offset = 3 - 4*1.0 = -1 → clamped to 1 (eager to sell)
    # At neutral (0):    bid offset = 3, ask offset = 3 (symmetric)
    # At max short (-30): mirror of max long
    OSMIUM_SKEW_FACTOR = 4

    # =========================================================================
    # RUN — called once per tick by the exchange
    # =========================================================================
    def run(self, state: TradingState):
        """
        Main entry point. Called every tick with the current TradingState.

        Returns:
            result      — dict of product → list of Orders to submit
            conversions — number of conversion requests (0 for round 1)
            trader_data — JSON string persisted to next tick's state.traderData
        """

        # =====================================================================
        # STEP 0: RESTORE PERSISTED STATE
        # =====================================================================
        # state.traderData is the ONLY way to carry information between ticks.
        # It's a JSON string we wrote on the previous tick. We store:
        #
        #   pepper_day_start_price — the mid-price at tick 0 of the current day.
        #       Used as the intercept for our fair value model:
        #       fair(t) = pepper_day_start_price + 0.001 * timestamp
        #
        #   pepper_last_timestamp — the timestamp of the previous tick.
        #       Used to detect day boundaries (timestamp resets to 0 on new day).
        #
        stored = {}
        if state.traderData and state.traderData != "":
            try:
                stored = json.loads(state.traderData)
            except json.JSONDecodeError:
                stored = {}

        pepper_day_start_price = stored.get("pepper_day_start_price", None)
        pepper_last_timestamp = stored.get("pepper_last_timestamp", None)

        # =====================================================================
        # STEP 1: PROCESS EACH PRODUCT
        # =====================================================================
        result: Dict[Symbol, List[Order]] = {}

        for product in state.order_depths:
            order_depth: OrderDepth = state.order_depths[product]
            orders: List[Order] = []

            # -----------------------------------------------------------------
            # Position bookkeeping
            # -----------------------------------------------------------------
            # limit: max position (80 for both products)
            # position: our current holdings (positive = long, negative = short)
            # buy_room: how many more units we can BUY before hitting +80
            # sell_room: how many more units we can SELL before hitting -80
            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position    # e.g. at +60: buy_room = 20
            sell_room = limit + position   # e.g. at +60: sell_room = 140

            # -----------------------------------------------------------------
            # Read the order book
            # -----------------------------------------------------------------
            # buy_orders (bids): price → POSITIVE volume (others want to buy)
            # sell_orders (asks): price → NEGATIVE volume (IMC convention)
            #
            # We sort bids descending (best bid first) and asks ascending
            # (best ask first) so we can iterate from best to worst.
            bot_bids = sorted(order_depth.buy_orders.items(), reverse=True)
            bot_asks = sorted(order_depth.sell_orders.items())

            best_bid = bot_bids[0][0] if bot_bids else None
            best_ask = bot_asks[0][0] if bot_asks else None

            # If either side of the book is empty, skip this product.
            # Can't compute mid-price or trade meaningfully.
            if best_bid is None or best_ask is None:
                result[product] = orders
                continue

            # Mid-price: simple average of best bid and best ask.
            # Used as the starting point for fair value on day 1 tick 0.
            mid_price = (best_bid + best_ask) / 2

            # =================================================================
            # INTARIAN_PEPPER_ROOT — DIRECTIONAL ACCUMULATION
            # =================================================================
            if product == "INTARIAN_PEPPER_ROOT":

                # -------------------------------------------------------------
                # Day boundary detection
                # -------------------------------------------------------------
                # The exchange runs multiple "days" (each 100,000 ticks).
                # At the start of a new day, timestamp resets to 0 (or a
                # small number). We detect this by checking:
                #   1. First tick ever (no stored start price)
                #   2. Timestamp went backwards (new day started)
                #
                # When a new day starts, we record the current mid-price
                # as the intercept for our linear fair value model.
                is_new_day = False
                if pepper_day_start_price is None:
                    # Very first tick of the entire run
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    # Timestamp decreased = new day
                    is_new_day = True

                if is_new_day:
                    pepper_day_start_price = mid_price

                pepper_last_timestamp = state.timestamp

                # -------------------------------------------------------------
                # Fair value: linear ramp
                # -------------------------------------------------------------
                # The price of PEPPER rises at exactly +0.001 per timestamp.
                # So at any point in the day:
                #   fair(t) = starting_price + 0.001 * t
                #
                # Example: if day starts at mid=15,000 and we're at ts=50,000:
                #   fair = 15,000 + 0.001 * 50,000 = 15,050
                #
                # We round to int because prices on the exchange are integers.
                fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp
                fair_int = round(fair)

                # =============================================================
                # PHASE 1: AGGRESSIVE ACCUMULATION (position < 80)
                # =============================================================
                # GOAL: Get to +80 as fast as humanly possible.
                #
                # WHY: Every tick we're below +80, we're losing drift PnL.
                # At +80, we earn 80 * 0.001 = 0.08 per tick = 8 per 100 ticks.
                # At +0, we earn nothing. The "opportunity cost" of a slow
                # accumulation dwarfs any savings from better entry prices.
                #
                # HOW:
                # 1. Sweep all asks priced at fair+4 or below (aggressive take)
                # 2. Post passive bid at fair+2 to catch incoming sells
                # 3. NEVER post an ask. Not even a "wide" one.
                #
                # WHY NO ASKS: v4 proved this is fatal. Even an ask at fair+5
                # gets filled constantly by bots that bid above our fair model.
                # Each sell during accumulation delays reaching +80 and costs
                # drift PnL. The v4 disaster: went short -13 early, TWAP of
                # only 10.9 units vs v3's 48.3. Total loss of ~3,400 PnL.
                # NEVER SELL DURING ACCUMULATION.

                if position < limit:

                    # ---------------------------------------------------------
                    # Step 1A: Sweep cheap asks (aggressive buying)
                    # ---------------------------------------------------------
                    # We iterate through the ask side of the book (cheapest
                    # first) and buy everything priced at fair+4 or below.
                    #
                    # fair+4 means we're "overpaying" by 4 per unit at most.
                    # On 80 units that's 320 total overpay. But reaching +80
                    # even 4,000 ticks sooner earns 80*0.001*4000 = 320 in
                    # drift — the breakeven is just 4,000 ticks (~4% of day).
                    # Anything beyond that is pure profit.
                    max_buy_price = fair_int + self.PEPPER_MAX_OVERPAY

                    for ask_price, ask_vol in bot_asks:
                        if ask_price <= max_buy_price and buy_room > 0:
                            # ask_vol is NEGATIVE (IMC convention), negate it
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            # Asks are sorted ascending — once too expensive, stop
                            break

                    # ---------------------------------------------------------
                    # Step 1B: Post passive bid (catch incoming sells)
                    # ---------------------------------------------------------
                    # After sweeping, if we still have room (didn't fill to 80),
                    # post a resting bid to catch any sells that come in.
                    #
                    # We bid at fair+2 (v3 used fair+1). The extra 1 tick costs
                    # us 1 per unit but puts us higher in the queue, meaning
                    # we fill more often. Since the drift earns 0.001 per tick
                    # per unit, the 1-tick overpay is recovered in 1,000 ticks.
                    # Meanwhile we might fill 5-10% more often.
                    if buy_room > 0:
                        passive_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, passive_bid, buy_room))

                    # NO ASK HERE. See "WHY NO ASKS" above.

                # =============================================================
                # PHASE 2: HOLD + LIGHT MARKET-MAKING (position = 80)
                # =============================================================
                # We've reached +80. The drift is now earning us ~0.08/tick
                # automatically. We add a small market-making overlay to
                # earn extra spread income on top.
                #
                # The overlay trades only 10 units (PEPPER_MM_SIZE) so we
                # never significantly reduce our drift exposure. If we sell
                # 10 and the price rises 1 before we rebuy, we only miss
                # 10 * 0.001 * 1000 = 10 — while the other 70 units earned
                # 70 * 0.001 * 1000 = 70. The drift dominates.
                else:
                    # Post a small ask above fair to sell a few units high
                    mm_size = min(self.PEPPER_MM_SIZE, sell_room)
                    if mm_size > 0:
                        my_ask = fair_int + self.PEPPER_MM_OFFSET
                        orders.append(Order(product, my_ask, -mm_size))

                    # If we dipped below 80 (from a previous MM sell filling),
                    # bid aggressively to refill. Every tick below 80 is lost
                    # drift on those missing units.
                    if buy_room > 0:
                        my_bid = fair_int + self.PEPPER_PASSIVE_BID_PREMIUM
                        orders.append(Order(product, my_bid, buy_room))

                # ---------------------------------------------------------
                # Take-profit: sell into very overpriced bids (only at +80)
                # ---------------------------------------------------------
                # If we're at max position and someone is bidding fair+3 or
                # higher, that's rare free money. We sell a small amount
                # into it and rebuy next tick via the holding-phase bid.
                #
                # We only do this at +80 (not during accumulation!) and
                # cap the sell size to PEPPER_MM_SIZE (10 units).
                if position >= limit:
                    for bid_price, bid_vol in bot_bids:
                        if bid_price >= fair_int + self.PEPPER_TAKE_PROFIT_THRESHOLD and sell_room > 0:
                            take_qty = min(bid_vol, sell_room, self.PEPPER_MM_SIZE)
                            orders.append(Order(product, bid_price, -take_qty))
                            sell_room -= take_qty
                        else:
                            break

            # =================================================================
            # ASH_COATED_OSMIUM — CONSERVATIVE MARKET-MAKING
            # =================================================================
            # This section is IDENTICAL to v3. v4 proved that tightening
            # the quote offset from 3→2 was pure downside (same fills,
            # worse prices, lost 193 PnL). We keep v3's proven settings.
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # --- Fair value: hardcoded at 10,000 ---
                # The true center across all historical days is ~10,000.
                # Day 1: 9998, Day 2: 10001, Day 3: 10002.
                # Hardcoding eliminates EMA lag and noise-driven drift.
                fair_int = self.OSMIUM_FAIR

                # --- Soft position cap ---
                # We restrict ourselves to ±30 instead of ±80.
                # With only ~550 theoretical max/day, a large position
                # that goes against us can easily wipe all OSMIUM profit.
                # At ±30 we still capture most available spread while
                # keeping max drawdown manageable.
                soft_limit = self.OSMIUM_SOFT_LIMIT
                soft_buy_room = soft_limit - position     # how much more to buy within ±30
                soft_sell_room = soft_limit + position     # how much more to sell within ±30

                # Clamp to exchange limits (can't exceed ±80 regardless)
                soft_buy_room = min(soft_buy_room, buy_room)
                soft_sell_room = min(soft_sell_room, sell_room)

                # --- Inventory-aware quote offsets ---
                # position_ratio: ranges from -1.0 (max short) to +1.0 (max long)
                #
                # When we're LONG, we want to:
                #   - Widen bid (buy less eagerly) → bid_offset increases
                #   - Tighten ask (sell more eagerly) → ask_offset decreases
                # This naturally pushes us back toward flat.
                #
                # When we're SHORT, the opposite happens.
                #
                # Formula:
                #   bid_offset = base + skew * ratio
                #   ask_offset = base - skew * ratio
                #
                # At neutral (ratio=0): symmetric quotes at fair±3
                # At +30 (ratio=1.0): bid at fair-7, ask at fair+1
                # At -30 (ratio=-1.0): bid at fair+1, ask at fair-7
                position_ratio = position / soft_limit if soft_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                # Minimum offset of 1 — never bid at or above fair, never
                # ask at or below fair. Crossing the spread = instant loss.
                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # --- AGGRESSIVE: take mispriced orders ---
                # If anyone is offering below fair (ask < 10,000), buy it.
                # If anyone is bidding above fair (bid > 10,000), sell to them.
                # These are "free" trades — buying below or selling above
                # true value with no directional risk.

                # Sweep cheap asks (below fair)
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and position <= 0:
                        # At exactly fair: only buy if we're short or flat.
                        # This avoids building unnecessary long inventory at
                        # no-edge prices. If we're already long, skip it.
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                # Sweep expensive bids (above fair)
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and soft_sell_room > 0:
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and soft_sell_room > 0 and position >= 0:
                        # At exactly fair: only sell if we're long or flat.
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # --- PASSIVE: post resting quotes around fair ---
                # These sit in the book waiting for someone to trade with us.
                # The skew-adjusted offsets push us toward flat inventory.
                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

                # Safety: never bid at/above fair or ask at/below fair
                my_bid = min(my_bid, fair_int - 1)
                my_ask = max(my_ask, fair_int + 1)

                if soft_buy_room > 0:
                    orders.append(Order(product, my_bid, soft_buy_room))
                if soft_sell_room > 0:
                    orders.append(Order(product, my_ask, -soft_sell_room))

            # =================================================================
            # UNKNOWN PRODUCT — safe fallback
            # =================================================================
            # If a new product appears that we haven't coded for, do basic
            # market-making around mid-price. Better than crashing.
            else:
                fair_int = round(mid_price)
                # Take mispriced orders
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
                # Post passive quotes
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
        # Write our state variables to JSON so the next tick can read them.
        # This is the only way to maintain memory across ticks.
        trader_data = json.dumps({
            "pepper_day_start_price": pepper_day_start_price,
            "pepper_last_timestamp": pepper_last_timestamp,
        })

        # No conversions in round 1 (that mechanic isn't used for these products)
        conversions = 0

        # Flush logs for the visualizer
        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
