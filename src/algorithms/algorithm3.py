"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v3 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — deterministic linear price ramp (+0.001/tick)
    2. ASH_COATED_OSMIUM     — mean-reverts around ~10,000

CRITICAL INSIGHT FROM DATA ANALYSIS:
    The theoretical max profit (perfect foresight DP) is ~7,500/day, and 92%
    of it comes from PEPPER's directional drift. The old approach (market-
    making both products equally) only captured 33% of the max.

    There is ZERO free money in the order book — no asks below mid, no bids
    above mid. All profit requires directional risk.

REVISED STRATEGY:
    PEPPER  → DIRECTIONAL. Buy 80 units ASAP, hold, ride the +1,000/day drift.
              This alone should yield ~5,000-7,000/day.
    OSMIUM  → LIGHT MARKET-MAKING. Scrape tiny spread with small positions.
              Cap exposure to avoid blowups. Target: ~100-300/day, don't lose.

Position limits: 80 for both products.
Profit target: 200,000 XIRECs before day 3 (across algo + manual challenge).
=============================================================================
"""

import json
from typing import Any, List, Dict

from datamodel import (
    Listing, Observation, Order, OrderDepth,
    ProsperityEncoder, Symbol, Trade, TradingState,
)


# =============================================================================
# POSITION LIMITS — from the round 1 rules
# =============================================================================
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS (standard IMC Prosperity visualizer helper — not strategy logic)
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
    Round 1 strategy — two very different approaches for two products:

    =========================================================================
    INTARIAN_PEPPER_ROOT — DIRECTIONAL (trend-following)
    =========================================================================
    WHY: Price rises linearly at +0.001 per timestamp unit = +1,000 per day.
         Over a full day, 80 units x 1,000 points = 80,000 of raw drift PnL.
         The theoretical max (DP) is ~7,000/day after trading costs. The order
         book is efficiently priced — no free money, so all profit comes from
         riding the upward drift.

    HOW: Buy aggressively to reach +80 ASAP. Then hold. The position
         appreciates ~1 per 1,000 timestamps per unit. Over the day:
           80 units x ~100 points captured = ~8,000 gross
         After paying the spread to enter (~6.5 per unit x 80 = ~520 cost),
         net profit is ~5,000-7,000.

    WHY NOT MARKET-MAKE: The old algorithm tried to market-make pepper,
         posting bids and asks around fair. Problem: market-making is
         delta-neutral — you profit from spread but don't capture drift.
         With a 13-tick spread and only ~2-tick std around fair, the spread
         profit per tick is tiny (~1-2 per fill). Meanwhile each tick of
         holding 80 units long earns 0.08 (80 * 0.001). Over 1000 ticks
         that's 80 — dwarfing any spread income.

    =========================================================================
    ASH_COATED_OSMIUM — LIGHT MARKET-MAKING
    =========================================================================
    WHY: Price mean-reverts around ~10,000 with zero drift. Range is only
         ~25 points, spread is ~16. Theoretical max is only ~550/day. This
         product is a sideshow — the goal is "don't lose money."

    HOW: Market-make around 10,000 with conservative position limits.
         Take any mispriced orders (below fair on asks, above fair on bids).
         Post passive quotes. Cap position at +/- 30 (not the full 80) to
         avoid the Q4-style drawdown we saw in v2 where a -34 short position
         got crushed.

    WHY CAP AT 30: In v2, the bot accumulated -34 and lost ~200 in a bad
         stretch. With only ~550 theoretical max, a single blowup can wipe
         the entire product's PnL. Keeping positions small (30 vs 80)
         limits max drawdown while still capturing most of the spread.
    =========================================================================
    """

    # -------------------------------------------------------------------------
    # PEPPER CONFIG
    # -------------------------------------------------------------------------

    # The linear drift rate: +0.001 per timestamp unit.
    # This means +1 every 1,000 timestamps, +1,000 over a full day.
    # Confirmed from regression across 3 days of historical data.
    PEPPER_SLOPE = 0.001

    # Maximum price we're willing to pay ABOVE fair to aggressively accumulate.
    # Setting this to 3 means we'll buy asks up to fair+3 while building position.
    # Rationale: paying 3 extra on 80 units = 240 cost. But 80 units x 1000 drift
    # = 80,000 gross over a full day. The 240 is noise. Speed of accumulation
    # matters more than entry precision.
    PEPPER_MAX_OVERPAY = 3

    # Once we're at max position, we can optionally market-make to earn extra.
    # This offset controls how far from fair we post passive quotes.
    # We only do this with a small slice of our capacity (not the full 80).
    PEPPER_MM_OFFSET = 2

    # -------------------------------------------------------------------------
    # OSMIUM CONFIG
    # -------------------------------------------------------------------------

    # Fixed fair value — the true center is ~10,000 and doesn't move.
    # Hardcoding is better than EMA here because:
    #   - EMA with alpha=0.3 was lagging and causing adverse selection in v2
    #   - The actual mean across 3 days is 9998, 10001, 10002 — all ~10,000
    #   - Hardcoding means we never get dragged away by noise
    OSMIUM_FAIR = 10000

    # Soft position cap for osmium. The exchange allows 80, but we self-impose
    # a lower cap to limit drawdown risk. With only ~550 theoretical max/day,
    # a blowup from a large position isn't worth the risk.
    OSMIUM_SOFT_LIMIT = 30

    # Quote offset (half-spread) for osmium passive orders.
    # The natural bot spread is ~16 (half = 8). Quoting at 3 puts us well
    # inside the bots while maintaining a 6-tick round-trip profit per fill.
    OSMIUM_QUOTE_OFFSET = 3

    # Inventory skew factor — how aggressively we lean quotes to unwind.
    # With the soft cap of 30, a skew of 4 means at max position (+30),
    # our bid offset becomes 3 + 4*(30/30) = 7 (very wide, barely buying)
    # and our ask offset becomes 3 - 4*(30/30) = -1 → clamped to 1 (very tight).
    OSMIUM_SKEW_FACTOR = 4

    def run(self, state: TradingState):
        """
        Called once per tick. Returns (orders_dict, conversions, trader_data).
        """

        # =====================================================================
        # STEP 0: RESTORE STATE
        # =====================================================================
        # We persist across ticks via state.traderData (JSON string).
        # pepper_day_start_price: mid-price at first tick of current day
        # pepper_last_timestamp: for detecting day boundaries
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

            # ----- Position bookkeeping -----
            limit = POSITION_LIMITS.get(product, 80)
            position = state.position.get(product, 0)
            buy_room = limit - position    # how much more we CAN buy
            sell_room = limit + position   # how much more we CAN sell

            # ----- Read the order book -----
            # Bids: price → positive volume (people want to buy from us)
            # Asks: price → NEGATIVE volume (IMC convention)
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
                # Timestamp resets to 0 at the start of each new day.
                # We detect this by checking if timestamp decreased.
                is_new_day = False
                if pepper_day_start_price is None:
                    is_new_day = True
                elif pepper_last_timestamp is not None and state.timestamp < pepper_last_timestamp:
                    is_new_day = True

                if is_new_day:
                    pepper_day_start_price = mid_price

                pepper_last_timestamp = state.timestamp

                # --- Compute fair value ---
                # fair(t) = day_start_price + 0.001 * timestamp
                # This is the exact center of where the price should be
                # right now, based on the known linear drift.
                fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp
                fair_int = round(fair)

                # =============================================================
                # PHASE 1: AGGRESSIVE ACCUMULATION (while position < 80)
                # =============================================================
                # The entire strategy for pepper is: GET TO +80 FAST.
                #
                # Every tick we hold 80 units, we earn 80 * 0.001 = 0.08 from
                # the drift. Over 1000 ticks that's 80 points. So the COST of
                # not being at +80 is 0.08 per tick per unit we're short of 80.
                #
                # Example: if we're at position 0 at tick 500, we've already
                # "lost" 80 * 0.001 * 500 * 100 = 4,000 in potential drift PnL.
                #
                # Therefore we're willing to overpay slightly to fill faster.
                # Paying 3 above fair on 80 units = 240 total cost.
                # Missing 100 ticks at +80 = 80 * 0.001 * 100 * 100 = 800 lost.
                # Speed >>> precision for entry.

                if position < limit:
                    # We want to BUY. Sweep all asks up to fair + overpay.
                    # This is aggressive — we're paying above fair to fill.
                    max_buy_price = fair_int + self.PEPPER_MAX_OVERPAY

                    for ask_price, ask_vol in bot_asks:
                        if ask_price <= max_buy_price and buy_room > 0:
                            # ask_vol is negative, negate to get size
                            take_qty = min(-ask_vol, buy_room)
                            orders.append(Order(product, ask_price, take_qty))
                            buy_room -= take_qty
                            position += take_qty
                        else:
                            # Asks sorted ascending — stop once too expensive
                            break

                    # If we still have room after sweeping, post a passive bid
                    # to catch any sells that come in. Bid at fair + small premium
                    # to maximize fill probability.
                    if buy_room > 0:
                        # Bid aggressively: at fair or even fair+1 to get filled
                        # The drift will make this profitable within a few ticks
                        passive_bid = fair_int + 1
                        orders.append(Order(product, passive_bid, buy_room))

                # =============================================================
                # PHASE 2: HOLD + OPTIONAL LIGHT MARKET-MAKING (at +80)
                # =============================================================
                # Once at +80, we just hold and let the drift work.
                # But we can also do a tiny bit of market-making on the side:
                # sell a few units high, buy them back low, repeat.
                #
                # This is OPTIONAL and should be conservative — the main PnL
                # comes from the position, not from spread capture.
                #
                # We only market-make with a small slice (up to 10 units)
                # so we never risk losing our core +80 position significantly.

                else:
                    # We're at +80 (or close). Light market-making:
                    # - Post a small ask above fair to sell a few units high
                    # - Post a bid below fair to buy them back
                    # - Net: earn the spread while staying near +80
                    #
                    # sell_room = limit + position = 80 + 80 = 160 (can sell up to 160)
                    # but we only want to sell a small amount (up to 10 units)
                    mm_size = min(10, sell_room)  # small size for market-making

                    if mm_size > 0:
                        # Post ask a few ticks above fair
                        my_ask = fair_int + self.PEPPER_MM_OFFSET
                        orders.append(Order(product, my_ask, -mm_size))

                    # If we somehow dipped below 80 (from a previous MM sell),
                    # bid aggressively to get back to 80
                    if buy_room > 0:
                        my_bid = fair_int + 1  # aggressive bid to refill
                        orders.append(Order(product, my_bid, buy_room))

                # Also: take any bids significantly above fair (free money)
                # This can happen if some bot is bidding way above fair.
                # We sell into it, then immediately buy back next tick.
                # Only do this if we're at max position (don't sell if building).
                if position >= limit:
                    for bid_price, bid_vol in bot_bids:
                        # Only sell if bid is well above fair (at least +3)
                        # We need a good premium to justify temporarily reducing
                        # our long position.
                        if bid_price >= fair_int + 3 and sell_room > 0:
                            take_qty = min(bid_vol, sell_room, 10)  # cap at 10
                            orders.append(Order(product, bid_price, -take_qty))
                            sell_room -= take_qty
                        else:
                            break

            # =================================================================
            # ASH_COATED_OSMIUM — CONSERVATIVE MARKET-MAKING
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # --- Fair value: hardcoded at 10,000 ---
                # Why not EMA? In v2, the EMA lagged and caused adverse fills.
                # The true value is rock-solid at ~10,000 across all 3 days.
                # Hardcoding eliminates any risk of fair-value drift.
                fair_int = self.OSMIUM_FAIR

                # --- Soft position cap ---
                # We self-limit to +/- OSMIUM_SOFT_LIMIT (30) instead of the
                # exchange's 80. Rationale:
                #   - Theoretical max is only ~550/day
                #   - In v2, a -34 position caused a ~200 drawdown in Q4
                #   - Keeping positions small limits max loss
                #   - 30 is enough to capture most of the ~550 theoretical max
                soft_limit = self.OSMIUM_SOFT_LIMIT
                soft_buy_room = soft_limit - position   # positive = can buy more
                soft_sell_room = soft_limit + position   # positive = can sell more

                # Clamp to exchange limits too (can't exceed 80 regardless)
                soft_buy_room = min(soft_buy_room, buy_room)
                soft_sell_room = min(soft_sell_room, sell_room)

                # --- Inventory-aware quote offsets ---
                # position_ratio: -1.0 at max short, +1.0 at max long
                # When long: widen bid (buy less eagerly), tighten ask (sell more eagerly)
                # When short: tighten bid (buy more eagerly), widen ask (sell less eagerly)
                position_ratio = position / soft_limit if soft_limit > 0 else 0
                # Clamp to [-1, 1] in case actual position exceeds soft limit
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                # Minimum 1 tick from fair to never cross the spread
                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # --- AGGRESSIVE: take mispriced orders ---
                # Sweep asks below fair (bargain buys)
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and position <= 0:
                        # At exactly fair: only buy if short/flat (inventory control)
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                # Sweep bids above fair (expensive sells)
                for bid_price, bid_vol in bot_bids:
                    if bid_price > fair_int and soft_sell_room > 0:
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    elif bid_price == fair_int and soft_sell_room > 0 and position >= 0:
                        take_qty = min(bid_vol, soft_sell_room)
                        orders.append(Order(product, bid_price, -take_qty))
                        soft_sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # --- PASSIVE: post quotes around fair ---
                # Bid below fair, ask above fair. Skew-adjusted offsets
                # push us toward flat when we have inventory.
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
        # STEP 2: PERSIST STATE
        # =====================================================================
        trader_data = json.dumps({
            "pepper_day_start_price": pepper_day_start_price,
            "pepper_last_timestamp": pepper_last_timestamp,
        })

        conversions = 0

        try:
            logger.flush(state, result, conversions, trader_data)
        except Exception:
            pass

        return result, conversions, trader_data
