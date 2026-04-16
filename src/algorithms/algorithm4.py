"""
=============================================================================
PROSPERITY 4 — ROUND 1 TRADER v4 (heavily annotated)
=============================================================================
Products traded this round:
    1. INTARIAN_PEPPER_ROOT  — "steady" asset with linear drift (+0.001/tick)
    2. ASH_COATED_OSMIUM     — "volatile" asset, mean-reverts around ~10,000

V3 RESULTS (6,294 PnL):
    PEPPER: 5,089 — but took until ts=65,700 (66% of day!) to reach +80.
            Only 3.9% fill rate. Massive missed drift PnL.
    OSMIUM: 1,205 — clean MM, steady growth. 8.9% fill rate.

V4 CHANGES:
    PEPPER: Switch from "aggressive directional buy" to ASYMMETRIC MARKET-MAKING.
            - Tight bid (fair-1) to passively accumulate longs on every tick
            - Wide ask (fair+5) so sells rarely fill while building position
            - Take any ask <= fair aggressively
            - This gives us fills on MORE ticks (higher fill rate) at BETTER
              prices (buying at fair-1 instead of paying fair+3)
            - Natural long bias builds position through volume, not overpaying
            - Once at +80: symmetric MM to earn spread while holding

    OSMIUM: Minor tuning from v3 (already good at 1,205/day).
            - Tighter quotes (offset 2 instead of 3) for more fills
            - Everything else stays the same

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
# POSITION LIMITS — from round 1 rules, both capped at 80
# =============================================================================
POSITION_LIMITS = {
    "INTARIAN_PEPPER_ROOT": 80,
    "ASH_COATED_OSMIUM": 80,
}


# =============================================================================
# LOGGER CLASS (standard visualizer helper — not strategy logic)
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
    Round 1 v4 strategy:

    =========================================================================
    INTARIAN_PEPPER_ROOT — ASYMMETRIC MARKET-MAKING
    =========================================================================
    The "steady" product. Price drifts linearly at +0.001 per timestamp unit
    (+1,000 per day). The spread is ~13 ticks. Std around fair is ~2.

    KEY CHANGE FROM V3:
        v3 tried to aggressively buy to +80 by paying fair+3 and posting
        bids at fair+1. This only filled on 3.9% of ticks and took 66% of
        the day to reach +80. The entry was cheap (underpaid by 0.72/unit
        on average), but the SPEED was terrible — we missed ~4,900 in
        drift PnL waiting to accumulate.

        v4 uses asymmetric market-making instead:
        - Tight bid at (fair - 1): sits just below fair value so any
          downward noise fills us. Since the price oscillates ±2 around
          fair, we get filled frequently on natural mean-reversion dips.
        - Wide ask at (fair + 5): rarely fills while we're building, so
          we naturally accumulate a long position through the bid side.
        - Take any ask <= fair: aggressively sweep cheap asks.

        This gives us:
        1. BETTER PRICES: buying at fair-1 instead of fair+1 or fair+3
        2. MORE FILLS: passive bids fill on every dip, not just when
           there's an ask to sweep
        3. NATURAL ACCUMULATION: the bid/ask asymmetry means we buy more
           than we sell, building toward +80 organically
        4. STRAIGHT-LINE PNL: we earn spread on each fill while also
           building the position that captures drift

    Once at +80, we switch to symmetric MM (equal bid/ask offsets) to earn
    spread while holding. The drift PnL continues in the background.

    =========================================================================
    ASH_COATED_OSMIUM — CONSERVATIVE MARKET-MAKING (tuned from v3)
    =========================================================================
    Mean-reverts around ~10,000. Zero drift. Spread ~16. Range ~25.

    v3 already did well here (1,205/day). Minor changes:
    - Tighter quotes (offset 2 vs 3) to increase fill rate
    - Same soft position cap of 30, same skew factor of 4
    - Same hardcoded fair = 10,000
    =========================================================================
    """

    # -------------------------------------------------------------------------
    # PEPPER CONFIG
    # -------------------------------------------------------------------------

    # Linear drift rate. +0.001/timestamp = +1/1000ts = +1000/day.
    PEPPER_SLOPE = 0.001

    # ACCUMULATION PHASE quote offsets (while position < 80):
    # Tight bid to attract fills, wide ask to avoid selling.
    #
    # Bid at (fair - 1): just 1 tick below fair. Since price oscillates ±2
    # around fair with autocorrelation -0.50, the price dips to fair-1 or
    # lower on roughly half the ticks. This means we get filled ~50% of
    # the time — a massive improvement over v3's 3.9%.
    #
    # Ask at (fair + 5): well above the ±2 oscillation range, so sells
    # almost never fill. We're not trying to sell during accumulation —
    # we WANT to build a long position. The wide ask is there for the rare
    # case where price spikes way above fair (free money if it fills).
    PEPPER_ACCUM_BID_OFFSET = 1   # bid at fair - 1
    PEPPER_ACCUM_ASK_OFFSET = 5   # ask at fair + 5 (rarely fills)

    # HOLDING PHASE quote offsets (once at +80):
    # Symmetric market-making to earn spread while holding.
    # Offset of 2 means bid at (fair-2), ask at (fair+2) → 4-tick spread.
    # With ±2 oscillation, we get filled on both sides regularly.
    PEPPER_HOLD_BID_OFFSET = 2
    PEPPER_HOLD_ASK_OFFSET = 2

    # How many units to market-make with once at +80.
    # We only trade a small slice so we stay near max position.
    # If we sell 10 and buy 10, we earn the spread without losing
    # significant drift exposure (10/80 = 12.5% of position at risk).
    PEPPER_MM_SIZE = 10

    # Inventory skew for the holding phase.
    # When we dip below 80 (from an MM sell), the skew makes us
    # more eager to buy back. Factor of 2 is mild — we're already
    # biased toward buying via the accumulation logic.
    PEPPER_HOLD_SKEW = 2

    # -------------------------------------------------------------------------
    # OSMIUM CONFIG
    # -------------------------------------------------------------------------

    # Hardcoded fair value. Rock-solid at ~10,000 across all historical days.
    OSMIUM_FAIR = 10000

    # Soft position cap. Exchange allows 80, we self-impose 30 to limit risk.
    # v3 proved this works: stayed within ±30, PnL was steady, max drawdown 56.
    OSMIUM_SOFT_LIMIT = 30

    # Quote offset (half-spread).
    # v3 used 3, giving 6-tick round-trip. Tightening to 2 gives 4-tick
    # round-trip — less profit per fill but MORE fills. With mean-reversion
    # this should net out positive (volume increase > margin decrease).
    OSMIUM_QUOTE_OFFSET = 2

    # Inventory skew — same as v3. At max position the offset grows to
    # 2 + 4*(1.0) = 6, effectively pulling back from that side.
    OSMIUM_SKEW_FACTOR = 4

    def run(self, state: TradingState):
        """
        Called once per tick. Returns (orders_dict, conversions, trader_data).
        """

        # =====================================================================
        # STEP 0: RESTORE PERSISTED STATE
        # =====================================================================
        # state.traderData is the only way to carry info between ticks.
        # We store:
        #   pepper_day_start_price — mid-price at tick 0 of current day
        #   pepper_last_timestamp  — for detecting day transitions
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
            buy_room = limit - position
            sell_room = limit + position

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
            # INTARIAN_PEPPER_ROOT — ASYMMETRIC MARKET-MAKING
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

                # --- Fair value: linear ramp ---
                # fair(t) = day_start + 0.001 * timestamp
                fair = pepper_day_start_price + self.PEPPER_SLOPE * state.timestamp
                fair_int = round(fair)

                # =============================================================
                # AGGRESSIVE TAKING: sweep any asks at or below fair
                # =============================================================
                # If anyone is selling at fair or cheaper, we take it immediately.
                # These are "free" buys — at fair value or better.
                # We do this regardless of whether we're accumulating or holding,
                # because buying at/below fair is always good when we want to be long.
                for ask_price, ask_vol in bot_asks:
                    if ask_price <= fair_int and buy_room > 0:
                        take_qty = min(-ask_vol, buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        buy_room -= take_qty
                        position += take_qty
                    else:
                        break

                # Also take any bids way above fair (selling into overpriced bids).
                # Only do this with small size — we don't want to reduce our long.
                for bid_price, bid_vol in bot_bids:
                    if bid_price >= fair_int + 4 and sell_room > 0:
                        # Only sell small amounts into very overpriced bids
                        take_qty = min(bid_vol, sell_room, self.PEPPER_MM_SIZE)
                        orders.append(Order(product, bid_price, -take_qty))
                        sell_room -= take_qty
                        position -= take_qty
                    else:
                        break

                # =============================================================
                # PASSIVE QUOTING: depends on whether we're accumulating or holding
                # =============================================================

                if position < limit:
                    # ---------------------------------------------------------
                    # ACCUMULATION PHASE: asymmetric quotes to build long
                    # ---------------------------------------------------------
                    # Tight bid to catch every dip, wide ask to avoid selling.
                    #
                    # The tighter our bid, the more often it fills. At fair-1,
                    # we fill on any tick where the price dips just 1 below fair.
                    # With ±2 std and -0.50 autocorrelation, this happens often.
                    #
                    # The wide ask at fair+5 means we almost never sell, so our
                    # position grows steadily through bid fills alone.
                    #
                    # Inventory skew: as position grows, we could widen the bid
                    # to slow accumulation. But we DON'T want to slow down —
                    # every tick without max position is lost drift PnL. So we
                    # keep the bid tight regardless of position during accumulation.

                    my_bid = fair_int - self.PEPPER_ACCUM_BID_OFFSET
                    my_ask = fair_int + self.PEPPER_ACCUM_ASK_OFFSET

                    # Safety floors
                    my_bid = min(my_bid, fair_int - 1)
                    my_ask = max(my_ask, fair_int + 1)

                    # Post full remaining buy capacity on the bid
                    if buy_room > 0:
                        orders.append(Order(product, my_bid, buy_room))

                    # Post a small ask (wide) — just in case price spikes
                    # Use remaining sell room but cap at MM_SIZE to not
                    # accidentally dump our whole position
                    ask_size = min(self.PEPPER_MM_SIZE, sell_room)
                    if ask_size > 0:
                        orders.append(Order(product, my_ask, -ask_size))

                else:
                    # ---------------------------------------------------------
                    # HOLDING PHASE: symmetric MM to earn spread at +80
                    # ---------------------------------------------------------
                    # We're at max position (+80). The drift PnL is automatic.
                    # Now we add spread income by market-making with a small
                    # slice (PEPPER_MM_SIZE = 10 units).
                    #
                    # The idea: sell 10 at fair+2, buy them back at fair-2,
                    # earn 4 ticks per round-trip on 10 units = 40 per cycle.
                    # Meanwhile the other 70+ units keep riding the drift.

                    # Skew: if we sold some units (position < 80), make the
                    # bid tighter to buy them back faster.
                    units_below_max = limit - position  # 0 when at exactly 80
                    position_ratio = units_below_max / self.PEPPER_MM_SIZE if self.PEPPER_MM_SIZE > 0 else 0
                    position_ratio = min(1.0, position_ratio)  # cap at 1.0

                    bid_offset = max(1, round(self.PEPPER_HOLD_BID_OFFSET - self.PEPPER_HOLD_SKEW * position_ratio))
                    ask_offset = max(1, round(self.PEPPER_HOLD_ASK_OFFSET + self.PEPPER_HOLD_SKEW * position_ratio))

                    my_bid = fair_int - bid_offset
                    my_ask = fair_int + ask_offset

                    my_bid = min(my_bid, fair_int - 1)
                    my_ask = max(my_ask, fair_int + 1)

                    # Bid: buy back anything we've sold + refill to 80
                    if buy_room > 0:
                        orders.append(Order(product, my_bid, buy_room))

                    # Ask: only sell a small slice
                    ask_size = min(self.PEPPER_MM_SIZE, sell_room)
                    if ask_size > 0:
                        orders.append(Order(product, my_ask, -ask_size))

            # =================================================================
            # ASH_COATED_OSMIUM — CONSERVATIVE MARKET-MAKING
            # =================================================================
            elif product == "ASH_COATED_OSMIUM":

                # --- Fair value: hardcoded 10,000 ---
                fair_int = self.OSMIUM_FAIR

                # --- Soft position cap ---
                soft_limit = self.OSMIUM_SOFT_LIMIT
                soft_buy_room = min(soft_limit - position, buy_room)
                soft_sell_room = min(soft_limit + position, sell_room)

                # --- Inventory-aware offsets ---
                position_ratio = position / soft_limit if soft_limit > 0 else 0
                position_ratio = max(-1.0, min(1.0, position_ratio))

                bid_offset = self.OSMIUM_QUOTE_OFFSET + self.OSMIUM_SKEW_FACTOR * position_ratio
                ask_offset = self.OSMIUM_QUOTE_OFFSET - self.OSMIUM_SKEW_FACTOR * position_ratio

                bid_offset = max(1, round(bid_offset))
                ask_offset = max(1, round(ask_offset))

                # --- AGGRESSIVE: sweep mispriced orders ---
                for ask_price, ask_vol in bot_asks:
                    if ask_price < fair_int and soft_buy_room > 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    elif ask_price == fair_int and soft_buy_room > 0 and position <= 0:
                        take_qty = min(-ask_vol, soft_buy_room)
                        orders.append(Order(product, ask_price, take_qty))
                        soft_buy_room -= take_qty
                        position += take_qty
                    else:
                        break

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

                # --- PASSIVE: post quotes ---
                my_bid = fair_int - bid_offset
                my_ask = fair_int + ask_offset

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
