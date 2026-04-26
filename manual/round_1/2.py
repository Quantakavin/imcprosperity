# ============================================
# EMBER MUSHROOM AUCTION BRUTE FORCE
# Max allowed order size: 75,000
# ============================================

# -----------------------------
# Order book from screenshot
# -----------------------------
bids = {
    20: 43000,
    19: 17000,
    18:  6000,
    17:  5000,
    16: 10000,
    15:  5000,
    14: 10000,
    13:  7000,
}

asks = {
    12: 20000,
    13: 25000,
    14: 35000,
    15:  6000,
    16:  5000,
    17:     0,
    18: 10000,
    19: 12000,
}

# -----------------------------
# Problem settings
# -----------------------------
TERMINAL_VALUE = 20.0
BUY_FEE = 0.05
SELL_FEE = 0.05

PRICE_MIN = 0
PRICE_MAX = 30

QTY_MIN = 0
QTY_MAX = 75000     # <- max allowed volume
QTY_STEP = 100     # you can reduce to 100 for finer search


# --------------------------------------------
# Helpers
# --------------------------------------------

def cumulative_bid_volume(book, price):
    """Total bid volume willing to buy at or above this price."""
    return sum(v for p, v in book.items() if p >= price)


def cumulative_ask_volume(book, price):
    """Total ask volume willing to sell at or below this price."""
    return sum(v for p, v in book.items() if p <= price)


def candidate_prices(bids_book, asks_book):
    """Possible clearing prices from the visible book."""
    return sorted(set(bids_book.keys()) | set(asks_book.keys()))


def find_clearing_price_and_volume(bids_book, asks_book):
    """
    Uniform-price auction assumption:
    choose the price that maximizes matched volume.
    Tie-break: choose the LOWEST such price.
    """
    best_price = None
    best_matched = -1

    for price in candidate_prices(bids_book, asks_book):
        demand = cumulative_bid_volume(bids_book, price)
        supply = cumulative_ask_volume(asks_book, price)
        matched = min(demand, supply)

        if matched > best_matched:
            best_matched = matched
            best_price = price
        elif matched == best_matched and price < best_price:
            best_price = price

    return best_price, best_matched


# --------------------------------------------
# Fill logic
# Assumption:
# - stale book orders have priority over us
# - within same price level, existing displayed orders are ahead of us
# --------------------------------------------

def my_buy_fill(my_price, my_qty, clearing_price, total_matched, base_bids):
    """
    My buy order is active only if my_price >= clearing_price.
    Orders at better prices go first.
    Existing orders at my same price also go first.
    """
    if my_price < clearing_price:
        return 0

    better_ahead = sum(v for p, v in base_bids.items() if p > my_price)
    same_price_ahead = base_bids.get(my_price, 0)

    ahead_of_me = better_ahead + same_price_ahead
    remaining = total_matched - ahead_of_me

    return max(0, min(my_qty, remaining))


def my_sell_fill(my_price, my_qty, clearing_price, total_matched, base_asks):
    """
    My sell order is active only if my_price <= clearing_price.
    Lower asks are better and go first.
    Existing orders at my same price also go first.
    """
    if my_price > clearing_price:
        return 0

    better_ahead = sum(v for p, v in base_asks.items() if p < my_price)
    same_price_ahead = base_asks.get(my_price, 0)

    ahead_of_me = better_ahead + same_price_ahead
    remaining = total_matched - ahead_of_me

    return max(0, min(my_qty, remaining))


# --------------------------------------------
# PnL functions
# --------------------------------------------

def profit_for_buy_order(my_price, my_qty, base_bids, base_asks):
    """
    Buy in auction, then auto-sell after auction at TERMINAL_VALUE.
    Net PnL per unit:
        TERMINAL_VALUE - clearing_price - BUY_FEE - SELL_FEE
    """
    new_bids = dict(base_bids)
    new_bids[my_price] = new_bids.get(my_price, 0) + my_qty

    clearing_price, total_matched = find_clearing_price_and_volume(
        new_bids, base_asks)
    filled = my_buy_fill(my_price, my_qty, clearing_price,
                         total_matched, base_bids)

    pnl_per_unit = TERMINAL_VALUE - clearing_price - BUY_FEE - SELL_FEE
    total_profit = filled * pnl_per_unit

    return {
        "side": "BUY",
        "price": my_price,
        "qty": my_qty,
        "clearing_price": clearing_price,
        "matched_volume": total_matched,
        "fill": filled,
        "pnl_per_unit": pnl_per_unit,
        "profit": total_profit,
    }


def profit_for_sell_order(my_price, my_qty, base_bids, base_asks):
    """
    Sell in auction.
    If you interpret this as selling something you don't later recover,
    PnL per unit is:
        clearing_price - TERMINAL_VALUE - BUY_FEE - SELL_FEE

    For Ember this will usually be unattractive, but we include it for completeness.
    """
    new_asks = dict(base_asks)
    new_asks[my_price] = new_asks.get(my_price, 0) + my_qty

    clearing_price, total_matched = find_clearing_price_and_volume(
        base_bids, new_asks)
    filled = my_sell_fill(my_price, my_qty, clearing_price,
                          total_matched, base_asks)

    pnl_per_unit = clearing_price - TERMINAL_VALUE - BUY_FEE - SELL_FEE
    total_profit = filled * pnl_per_unit

    return {
        "side": "SELL",
        "price": my_price,
        "qty": my_qty,
        "clearing_price": clearing_price,
        "matched_volume": total_matched,
        "fill": filled,
        "pnl_per_unit": pnl_per_unit,
        "profit": total_profit,
    }


# --------------------------------------------
# Search best quantity for each price
# --------------------------------------------

def best_buy_for_price(price):
    best = None
    for qty in range(QTY_MIN, QTY_MAX + 1, QTY_STEP):
        result = profit_for_buy_order(price, qty, bids, asks)
        if best is None or result["profit"] > best["profit"]:
            best = result
    return best


def best_sell_for_price(price):
    best = None
    for qty in range(QTY_MIN, QTY_MAX + 1, QTY_STEP):
        result = profit_for_sell_order(price, qty, bids, asks)
        if best is None or result["profit"] > best["profit"]:
            best = result
    return best


# --------------------------------------------
# Main display
# --------------------------------------------

def print_results():
    print("=" * 120)
    print("EMBER MUSHROOM | MAX ORDER SIZE = 75,000")
    print("=" * 120)

    print("\nBEST BUY PROFIT FOR EACH PRICE")
    print("-" * 120)
    print(f"{'Price':>5} | {'BestQty':>8} | {'ClrPx':>5} | {'Fill':>8} | {'PnL/Unit':>9} | {'Profit':>12}")

    best_overall_buy = None

    for price in range(PRICE_MIN, PRICE_MAX + 1):
        best = best_buy_for_price(price)
        if best_overall_buy is None or best["profit"] > best_overall_buy["profit"]:
            best_overall_buy = best

        print(
            f"{price:>5} | "
            f"{best['qty']:>8} | "
            f"{best['clearing_price']:>5} | "
            f"{best['fill']:>8} | "
            f"{best['pnl_per_unit']:>9.2f} | "
            f"{best['profit']:>12.2f}"
        )

    print("\nBEST SELL PROFIT FOR EACH PRICE")
    print("-" * 120)
    print(f"{'Price':>5} | {'BestQty':>8} | {'ClrPx':>5} | {'Fill':>8} | {'PnL/Unit':>9} | {'Profit':>12}")

    best_overall_sell = None

    for price in range(PRICE_MIN, PRICE_MAX + 1):
        best = best_sell_for_price(price)
        if best_overall_sell is None or best["profit"] > best_overall_sell["profit"]:
            best_overall_sell = best

        print(
            f"{price:>5} | "
            f"{best['qty']:>8} | "
            f"{best['clearing_price']:>5} | "
            f"{best['fill']:>8} | "
            f"{best['pnl_per_unit']:>9.2f} | "
            f"{best['profit']:>12.2f}"
        )

    print("\n" + "=" * 120)
    print("BEST OVERALL BUY")
    print("=" * 120)
    for k, v in best_overall_buy.items():
        print(f"{k}: {v}")

    print("\n" + "=" * 120)
    print("BEST OVERALL SELL")
    print("=" * 120)
    for k, v in best_overall_sell.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    print_results()
