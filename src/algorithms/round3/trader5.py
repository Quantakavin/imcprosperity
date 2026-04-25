"""
Round 3 trader for IMC Prosperity-style simulations.

This file is intentionally written as a teaching version:
  - the logic is split into small helper methods
  - comments explain both the Python mechanics and the trading idea
  - state is stored in `traderData` exactly the way the wiki describes

High-level strategy
===================

We trade two different asset classes this round:

1. Spot / delta-1 products
   - HYDROGEL_PACK
   - VELVETFRUIT_EXTRACT

   For these, we use a simple combination of:
   - fair-value market making
   - mild mean reversion
   - order-book imbalance
   - inventory-aware quoting

2. Options on VELVETFRUIT_EXTRACT
   - VEV_4000, VEV_4500, ..., VEV_6500

   For these, we:
   - estimate a single "fair volatility" for the whole option chain
   - use Black-Scholes to compute a fair price for each voucher
   - buy cheap vouchers and sell rich vouchers
   - use VELVETFRUIT_EXTRACT as a light hedge for total option delta

The overall design tries to be robust and easy to reason about rather than
ultra-optimized or overfit to only a few historical files.
"""

from datamodel import OrderDepth, TradingState, Order
from typing import Dict, List, Optional, Tuple
import json
import math
import statistics


# ============================================================================
# Option math helpers
# ============================================================================

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function.

    We use `math.erf` because Prosperity supports the Python standard library
    but not SciPy. This is the usual closed-form relationship:

        N(x) = 0.5 * (1 + erf(x / sqrt(2)))
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call_price(spot: float, strike: float, time_years: float, sigma: float) -> float:
    """Black-Scholes price of a European call option with zero interest rate."""
    intrinsic = max(spot - strike, 0.0)
    if time_years <= 0 or sigma <= 0:
        return intrinsic
    if spot <= 0 or strike <= 0:
        return intrinsic

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return spot * norm_cdf(d1) - strike * norm_cdf(d2)


def bs_call_delta(spot: float, strike: float, time_years: float, sigma: float) -> float:
    """Black-Scholes delta of a call option.

    Delta is the option's approximate sensitivity to a 1-unit move in the
    underlying. A delta of 0.60 means the option price behaves like about
    0.60 units of the underlying, locally.
    """
    if time_years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 1.0 if spot > strike else 0.0

    sqrt_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * time_years) / (sigma * sqrt_t)
    return norm_cdf(d1)


def implied_volatility(price: float, spot: float, strike: float, time_years: float) -> Optional[float]:
    """Invert Black-Scholes with a simple bisection search.

    We only need a stable, lightweight solver; bisection is slower than
    Newton's method but much safer for competition code.
    """
    intrinsic = max(spot - strike, 0.0)

    # If price is below intrinsic, there is no valid nonnegative volatility.
    if price <= intrinsic + 1e-6:
        return None

    # An option cannot reasonably be worth more than spot itself.
    if price >= spot:
        return None

    low = 1e-4
    high = 2.0

    for _ in range(50):
        mid = 0.5 * (low + high)
        model = bs_call_price(spot, strike, time_years, mid)
        if model > price:
            high = mid
        else:
            low = mid

    return 0.5 * (low + high)


# ============================================================================
# Order book helpers
# ============================================================================

def best_bid_ask(order_depth: OrderDepth) -> Tuple[Optional[int], Optional[int]]:
    """Return the best bid and best ask in the book, if both exist."""
    best_bid = max(order_depth.buy_orders.keys()) if order_depth.buy_orders else None
    best_ask = min(order_depth.sell_orders.keys()) if order_depth.sell_orders else None
    return best_bid, best_ask


def mid_price(order_depth: OrderDepth) -> Optional[float]:
    """Simple midpoint between best bid and best ask."""
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return None
    return 0.5 * (best_bid + best_ask)


def top_level_imbalance(order_depth: OrderDepth) -> float:
    """Return a simple top-of-book imbalance in [-1, 1].

    Positive means more size is sitting on the bid than on the ask, which often
    gives a small short-horizon upward nudge. Negative means the opposite.
    """
    best_bid, best_ask = best_bid_ask(order_depth)
    if best_bid is None or best_ask is None:
        return 0.0

    bid_qty = order_depth.buy_orders.get(best_bid, 0)
    ask_qty = -order_depth.sell_orders.get(best_ask, 0)
    total = bid_qty + ask_qty
    if total <= 0:
        return 0.0
    return (bid_qty - ask_qty) / total


# ============================================================================
# Trader
# ============================================================================

class Trader:
    """Prosperity-compatible trader class.

    The wiki says the platform calls `run(state)` every iteration and expects
    back:

        (result_dict, conversions, traderData)

    `result_dict` maps product -> list[Order]
    `conversions` is unused here, so we always return 0
    `traderData` is a string used to persist lightweight state
    """

    HYDROGEL = "HYDROGEL_PACK"
    VFE = "VELVETFRUIT_EXTRACT"

    VOUCHERS: Dict[str, int] = {
        "VEV_4000": 4000,
        "VEV_4500": 4500,
        "VEV_5000": 5000,
        "VEV_5100": 5100,
        "VEV_5200": 5200,
        "VEV_5300": 5300,
        "VEV_5400": 5400,
        "VEV_5500": 5500,
        "VEV_6000": 6000,
        "VEV_6500": 6500,
    }

    POSITION_LIMITS: Dict[str, int] = {
        HYDROGEL: 200,
        VFE: 200,
        **{name: 300 for name in VOUCHERS},
    }

    # Round 3 starts with 5 days to expiry according to the wiki text the user
    # provided. Historical files map to higher TTE, but in live round we care
    # about TTE=5 at timestamp 0 and then slightly lower as time passes.
    START_TTE_DAYS = 5.0
    TICKS_PER_DAY = 1_000_000

    # State windows for rolling mean / z-score style logic.
    SPOT_HISTORY_WINDOW = 60
    IV_HISTORY_WINDOW = 80

    # Quote sizing for the two spot products.
    HYDROGEL_QUOTE_SIZE = 20
    VFE_QUOTE_SIZE = 25

    # Soft cap for per-voucher directional inventory.
    # The exchange allows 300, but we stay inside that to reduce blow-up risk.
    VOUCHER_SOFT_CAP = 120

    # Round 2 manual challenge hook; harmless in other rounds.
    def bid(self):
        return 15

    def run(self, state: TradingState):
        """Main entry point called by the Prosperity engine each iteration."""
        memory = self._load_memory(state.traderData)
        self._ensure_memory_shape(memory)

        result: Dict[str, List[Order]] = {}

        # --------------------------------------------------------------------
        # Step 1: update histories from the current market snapshot
        # --------------------------------------------------------------------
        self._update_histories(memory, state)

        # --------------------------------------------------------------------
        # Step 2: estimate option fair vol and total option delta first
        #
        # We do this before trading VFE because the option book may want us to
        # lean long or short VFE as a hedge instrument.
        # --------------------------------------------------------------------
        voucher_context = self._build_voucher_context(memory, state)
        hedge_target_vfe = voucher_context["hedge_target_vfe"]

        # --------------------------------------------------------------------
        # Step 3: trade the two spot products
        # --------------------------------------------------------------------
        if self.HYDROGEL in state.order_depths:
            result[self.HYDROGEL] = self._trade_spot_product(
                product=self.HYDROGEL,
                order_depth=state.order_depths[self.HYDROGEL],
                position=state.position.get(self.HYDROGEL, 0),
                price_history=memory["spot_history"][self.HYDROGEL],
                base_fair=9990.0,  # Hydrogel is centered very stably in the data.
                quote_size=self.HYDROGEL_QUOTE_SIZE,
                hedge_target=0,
                mean_reversion_strength=0.35,
                imbalance_strength=1.20,
            )

        if self.VFE in state.order_depths:
            result[self.VFE] = self._trade_spot_product(
                product=self.VFE,
                order_depth=state.order_depths[self.VFE],
                position=state.position.get(self.VFE, 0),
                price_history=memory["spot_history"][self.VFE],
                base_fair=None,  # For VFE we trust recent rolling history more.
                quote_size=self.VFE_QUOTE_SIZE,
                hedge_target=hedge_target_vfe,
                mean_reversion_strength=0.45,
                imbalance_strength=1.00,
            )

        # --------------------------------------------------------------------
        # Step 4: trade the vouchers using model-vs-market pricing
        # --------------------------------------------------------------------
        for voucher, strike in self.VOUCHERS.items():
            if voucher not in state.order_depths:
                continue
            orders = self._trade_voucher(
                product=voucher,
                strike=strike,
                order_depth=state.order_depths[voucher],
                position=state.position.get(voucher, 0),
                spot=voucher_context["spot"],
                sigma=voucher_context["fair_sigma"],
                time_years=voucher_context["time_years"],
            )
            if orders:
                result[voucher] = orders

        trader_data = json.dumps(memory, separators=(",", ":"))
        conversions = 0
        return result, conversions, trader_data

    # ========================================================================
    # Memory / state helpers
    # ========================================================================

    def _load_memory(self, raw: str) -> dict:
        """Deserialize our state string safely.

        The wiki notes that AWS Lambda is effectively stateless, so we should
        not rely on class variables surviving between calls. `traderData` is
        the correct place to persist rolling histories.
        """
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _ensure_memory_shape(self, memory: dict) -> None:
        """Create any missing keys in our state dict."""
        memory.setdefault("spot_history", {})
        memory["spot_history"].setdefault(self.HYDROGEL, [])
        memory["spot_history"].setdefault(self.VFE, [])
        memory.setdefault("iv_history", [])

    def _update_histories(self, memory: dict, state: TradingState) -> None:
        """Store the latest mid prices for the two spot products."""
        for product in [self.HYDROGEL, self.VFE]:
            if product not in state.order_depths:
                continue
            mid = mid_price(state.order_depths[product])
            if mid is None:
                continue

            hist = memory["spot_history"][product]
            hist.append(mid)
            if len(hist) > self.SPOT_HISTORY_WINDOW:
                del hist[:-self.SPOT_HISTORY_WINDOW]

    # ========================================================================
    # Spot trading
    # ========================================================================

    def _trade_spot_product(
        self,
        product: str,
        order_depth: OrderDepth,
        position: int,
        price_history: List[float],
        base_fair: Optional[float],
        quote_size: int,
        hedge_target: int,
        mean_reversion_strength: float,
        imbalance_strength: float,
    ) -> List[Order]:
        """Trade a spot product with fair-value quoting and light mean reversion.

        This is the core structure:
          1. Estimate a fair value.
          2. Nudge that fair using mean reversion and book imbalance.
          3. Aggressively take obviously good prices already in the book.
          4. Post one passive bid and one passive ask around our adjusted fair.

        `hedge_target` is mostly used for VFE, which also serves as the hedge
        asset for total option delta.
        """
        orders: List[Order] = []
        limit = self.POSITION_LIMITS[product]

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        current_mid = 0.5 * (best_bid + best_ask)

        rolling_mean = statistics.mean(price_history) if price_history else current_mid
        rolling_std = statistics.pstdev(price_history) if len(price_history) >= 2 else 1.0
        rolling_std = max(rolling_std, 1.0)

        # For Hydrogel we blend a stable known center (9990) with the current
        # rolling mean. For VFE we mostly rely on the rolling mean.
        if base_fair is None:
            fair = rolling_mean
        else:
            fair = 0.6 * base_fair + 0.4 * rolling_mean

        # Z-score tells us how stretched the current price is relative to its
        # recent behavior.
        z_score = (current_mid - rolling_mean) / rolling_std

        # Positive imbalance = bid side is stronger -> small upward nudge.
        imbalance = top_level_imbalance(order_depth)

        # Mean reversion says:
        # - if current price is above recent mean, fair should be a bit lower
        # - if current price is below recent mean, fair should be a bit higher
        adjusted_fair = (
            fair
            - mean_reversion_strength * z_score
            + imbalance_strength * imbalance
        )

        # If VFE is being used to hedge option delta, shift our target inventory
        # toward that hedge target. We do not instantly force it there; instead
        # we skew quotes and aggressive takes so inventory gradually moves.
        inventory_error = position - hedge_target
        inventory_skew = inventory_error / max(limit, 1)

        # ----------------------------
        # Aggressively take edge first
        # ----------------------------
        #
        # We buy asks that are clearly below our fair and sell bids that are
        # clearly above our fair. The edge threshold prevents overtrading tiny
        # noise.
        take_edge = max(1.0, 0.35 * (best_ask - best_bid))

        sim_position = position

        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > adjusted_fair - take_edge:
                break
            if sim_position >= limit:
                break
            ask_volume = -order_depth.sell_orders[ask_price]
            buy_qty = min(ask_volume, limit - sim_position)
            if buy_qty > 0:
                orders.append(Order(product, ask_price, buy_qty))
                sim_position += buy_qty

        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price < adjusted_fair + take_edge:
                break
            if sim_position <= -limit:
                break
            bid_volume = order_depth.buy_orders[bid_price]
            sell_qty = min(bid_volume, sim_position + limit)
            if sell_qty > 0:
                orders.append(Order(product, bid_price, -sell_qty))
                sim_position -= sell_qty

        # -----------------------------------------
        # Passive quoting with inventory-aware skew
        # -----------------------------------------
        #
        # If we are too long relative to target, we want:
        # - a less aggressive bid
        # - a more aggressive ask
        #
        # If we are too short, we want the opposite.
        quote_shift = int(round(2.0 * inventory_skew))
        fair_int = int(round(adjusted_fair))

        buy_quote = min(best_bid + 1, fair_int - 1 - quote_shift)
        sell_quote = max(best_ask - 1, fair_int + 1 - quote_shift)

        if sell_quote <= buy_quote:
            sell_quote = buy_quote + 1

        # Make sizes asymmetric when inventory is off target.
        buy_capacity = limit - sim_position
        sell_capacity = sim_position + limit

        buy_size = int(round(quote_size * max(0.25, 1.0 - inventory_skew)))
        sell_size = int(round(quote_size * max(0.25, 1.0 + inventory_skew)))

        buy_size = min(buy_size, buy_capacity)
        sell_size = min(sell_size, sell_capacity)

        if buy_size > 0:
            orders.append(Order(product, buy_quote, buy_size))
        if sell_size > 0:
            orders.append(Order(product, sell_quote, -sell_size))

        return orders

    # ========================================================================
    # Voucher context and hedging
    # ========================================================================

    def _build_voucher_context(self, memory: dict, state: TradingState) -> dict:
        """Build shared option context: spot, TTE, fair sigma, hedge target."""
        spot = None
        if self.VFE in state.order_depths:
            spot = mid_price(state.order_depths[self.VFE])

        # If VFE is missing, we cannot sensibly price options.
        if spot is None:
            return {
                "spot": None,
                "time_years": 0.0,
                "fair_sigma": None,
                "hedge_target_vfe": 0,
            }

        time_days = max(0.05, self.START_TTE_DAYS - state.timestamp / self.TICKS_PER_DAY)
        time_years = time_days / 365.0

        # Collect current implied vol estimates from the more relevant strikes.
        # We ignore the deepest ITM and dead far OTM contracts because their
        # implied vols are either noisy or uninformative.
        current_ivs: List[float] = []
        for voucher, strike in self.VOUCHERS.items():
            if voucher not in state.order_depths:
                continue
            if strike in (4000, 4500, 6000, 6500):
                continue

            voucher_mid = mid_price(state.order_depths[voucher])
            if voucher_mid is None:
                continue

            iv = implied_volatility(voucher_mid, spot, strike, time_years)
            if iv is None:
                continue

            # Filter absurd outliers; in our historical read, fair sigma is low.
            if 0.001 <= iv <= 0.50:
                current_ivs.append(iv)

        if current_ivs:
            current_sigma = statistics.mean(current_ivs)
            memory["iv_history"].append(current_sigma)
            if len(memory["iv_history"]) > self.IV_HISTORY_WINDOW:
                del memory["iv_history"][:-self.IV_HISTORY_WINDOW]
        elif memory["iv_history"]:
            current_sigma = memory["iv_history"][-1]
        else:
            # Historical data suggests the chain roughly lives near 1.2% daily-
            # style vol in this toy market. We keep a sensible fallback.
            current_sigma = 0.012

        fair_sigma = statistics.mean(memory["iv_history"]) if memory["iv_history"] else current_sigma

        # Compute total option delta exposure from CURRENT positions.
        # Long calls -> positive delta; short calls -> negative delta.
        total_option_delta = 0.0
        for voucher, strike in self.VOUCHERS.items():
            pos = state.position.get(voucher, 0)
            if pos == 0:
                continue
            delta = bs_call_delta(spot, strike, time_years, fair_sigma)
            total_option_delta += pos * delta

        # Hedge target in VFE is the opposite of total option delta.
        # We clamp it because VFE also has its own spot alpha and a hard limit.
        raw_hedge_target = int(round(-total_option_delta))
        hedge_target_vfe = max(-150, min(150, raw_hedge_target))

        return {
            "spot": spot,
            "time_years": time_years,
            "fair_sigma": fair_sigma,
            "hedge_target_vfe": hedge_target_vfe,
        }

    # ========================================================================
    # Voucher trading
    # ========================================================================

    def _trade_voucher(
        self,
        product: str,
        strike: int,
        order_depth: OrderDepth,
        position: int,
        spot: Optional[float],
        sigma: Optional[float],
        time_years: float,
    ) -> List[Order]:
        """Trade one voucher using model fair value.

        The option strategy is intentionally simple:
          - compute fair option value
          - buy below fair by enough margin
          - sell above fair by enough margin

        Deep ITM contracts are mostly intrinsic value.
        Dead far OTM contracts are almost worthless.
        Middle strikes tend to carry the most interesting optionality.
        """
        orders: List[Order] = []

        if spot is None or sigma is None:
            return orders

        best_bid, best_ask = best_bid_ask(order_depth)
        if best_bid is None or best_ask is None:
            return orders

        limit = self.POSITION_LIMITS[product]
        soft_cap = self.VOUCHER_SOFT_CAP

        intrinsic = max(spot - strike, 0.0)
        fair_price = bs_call_price(spot, strike, time_years, sigma)

        # For deep ITM calls, fair value is very close to intrinsic.
        # To avoid tiny-model-noise overtrading, anchor partly to intrinsic.
        if strike in (4000, 4500):
            fair_price = 0.85 * intrinsic + 0.15 * fair_price

        # For dead far OTM calls, only trade if the market gets silly.
        if strike in (6000, 6500):
            fair_price = min(fair_price, 0.5)

        spread = best_ask - best_bid

        # Entry edge says how far market price must deviate from our model
        # before we want to trade. Wider-spread options need more edge.
        model_edge = max(1.0, 0.8 * spread)

        sim_position = position

        # Buy undervalued asks.
        for ask_price in sorted(order_depth.sell_orders.keys()):
            if ask_price > fair_price - model_edge:
                break
            if sim_position >= min(limit, soft_cap):
                break
            ask_volume = -order_depth.sell_orders[ask_price]
            buy_qty = min(ask_volume, min(limit, soft_cap) - sim_position)
            if buy_qty > 0:
                orders.append(Order(product, ask_price, buy_qty))
                sim_position += buy_qty

        # Sell overvalued bids.
        for bid_price in sorted(order_depth.buy_orders.keys(), reverse=True):
            if bid_price < fair_price + model_edge:
                break
            if sim_position <= -min(limit, soft_cap):
                break
            bid_volume = order_depth.buy_orders[bid_price]
            sell_qty = min(bid_volume, sim_position + min(limit, soft_cap))
            if sell_qty > 0:
                orders.append(Order(product, bid_price, -sell_qty))
                sim_position -= sell_qty

        # Small passive presence around fair for the middle strikes only.
        # We do NOT quote every option heavily; we want our main risk to come
        # from obvious mispricings, not from donating on stale quotes.
        if strike in (5000, 5100, 5200, 5300, 5400, 5500):
            fair_int = int(round(fair_price))

            bid_quote = min(best_bid + 1, fair_int - 1)
            ask_quote = max(best_ask - 1, fair_int + 1)
            if ask_quote <= bid_quote:
                ask_quote = bid_quote + 1

            passive_size = 8
            buy_capacity = min(limit, soft_cap) - sim_position
            sell_capacity = sim_position + min(limit, soft_cap)

            if buy_capacity > 0:
                orders.append(Order(product, bid_quote, min(passive_size, buy_capacity)))
            if sell_capacity > 0:
                orders.append(Order(product, ask_quote, -min(passive_size, sell_capacity)))

        return orders
