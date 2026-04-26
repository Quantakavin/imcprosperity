# ============================================
# GENERIC ONE-SHOT AUCTION BRUTE FORCE
# Works for Ember Mushroom or Dryland Flax
# ============================================

BUY_FEE = 0.05
SELL_FEE = 0.05

PRICE_MIN = 0
PRICE_MAX = 35

QTY_MIN = 0
QTY_MAX = 150000
QTY_STEP = 1000


# --------------------------------------------
# Helpers
# --------------------------------------------

def cumulative_bid_volume(book, price):
    return sum(v for p, v in book.items() if p >= price)


def cumulative_ask_volume(book, price):
    return sum(v for p, v in book.items() if p <= price)


def candidate_prices(bids_book, asks_book):
    return sorted(set(bids_book.keys()) | set(asks_book.keys()))


def find_clearing_price_and_volume(bids_book, asks_book):
    """
    Uniform-price auction:
    choose the price that maximizes matched volume.
    Tie-break: choose the lowest such price.
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
# Assumption: stale orders are ahead of us
# --------------------------------------------

def my_buy_fill(my_price, my_qty, clearing_price, total_matched, base_bids):
    if my_price < clearing_price:
        return 0

    better_ahead = sum(v for p, v in base_bids.items() if p > my_price)
    same_price_ahead = base_bids.get(my_price, 0)

    ahead_of_me = better_ahead + same_price_ahead
    remaining = total_matched - ahead_of_me

    return max(0, min(my_qty, remaining))


def my_sell_fill(my_price, my_qty, clearing_price, total_matched, base_asks):
    if my_price > clearing_price:
        return 0

    better_ahead = sum(v for p, v in base_asks.items() if p < my_price)
    same_price_ahead = base_asks.get(my_price, 0)

    ahead_of_me = better_ahead + same_price_ahead
    remaining = total_matched - ahead_of_me

    return max(0, min(my_qty, remaining))


# --------------------------------------------
# Profit functions
# --------------------------------------------

def profit_for_buy_order(my_price, my_qty, base_bids, base_asks, terminal_value):
    new_bids = dict(base_bids)
    new_bids[my_price] = new_bids.get(my_price, 0) + my_qty

    clearing_price, total_matched = find_clearing_price_and_volume(
        new_bids, base_asks)
    filled = my_buy_fill(my_price, my_qty, clearing_price,
                         total_matched, base_bids)

    pnl_per_unit = terminal_value - clearing_price - BUY_FEE - SELL_FEE
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


def profit_for_sell_order(my_price, my_qty, base_bids, base_asks, terminal_value):
    new_asks = dict(base_asks)
    new_asks[my_price] = new_asks.get(my_price, 0) + my_qty

    clearing_price, total_matched = find_clearing_price_and_volume(
        base_bids, new_asks)
    filled = my_sell_fill(my_price, my_qty, clearing_price,
                          total_matched, base_asks)

    pnl_per_unit = clearing_price - terminal_value - BUY_FEE - SELL_FEE
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
# Search best qty for each price
# --------------------------------------------

def best_buy_for_price(price, base_bids, base_asks, terminal_value,
                       qty_min=QTY_MIN, qty_max=QTY_MAX, qty_step=QTY_STEP):
    best = None
    for qty in range(qty_min, qty_max + 1, qty_step):
        result = profit_for_buy_order(
            price, qty, base_bids, base_asks, terminal_value)
        if best is None or result["profit"] > best["profit"]:
            best = result
    return best


def best_sell_for_price(price, base_bids, base_asks, terminal_value,
                        qty_min=QTY_MIN, qty_max=QTY_MAX, qty_step=QTY_STEP):
    best = None
    for qty in range(qty_min, qty_max + 1, qty_step):
        result = profit_for_sell_order(
            price, qty, base_bids, base_asks, terminal_value)
        if best is None or result["profit"] > best["profit"]:
            best = result
    return best


def print_results_table(product_name, bids, asks, terminal_value):
    print("\n" + "=" * 120)
    print(f"{product_name} | TERMINAL VALUE = {terminal_value}")
    print("=" * 120)

    print("\nBEST BUY PROFIT FOR EACH PRICE")
    print("-" * 120)
    print(f"{'Price':>5} | {'BestQty':>8} | {'ClrPx':>5} | {'Fill':>8} | {'PnL/Unit':>9} | {'Profit':>12}")

    best_overall_buy = None

    for price in range(PRICE_MIN, PRICE_MAX + 1):
        best = best_buy_for_price(price, bids, asks, terminal_value)
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
        best = best_sell_for_price(price, bids, asks, terminal_value)
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

    print("\nBEST OVERALL BUY")
    print(best_overall_buy)

    print("\nBEST OVERALL SELL")
    print(best_overall_sell)


# ============================================
# DRYLAND FLAX
# ============================================

dryland_bids = {
    30: 30000,
    29:  5000,
    28: 12000,
    27: 28000,
}

dryland_asks = {
    28: 40000,
    31: 20000,
    32: 20000,
    33: 30000,
}

DRYLAND_TERMINAL_VALUE = 30.0

print_results_table(
    product_name="Dryland Flax",
    bids=dryland_bids,
    asks=dryland_asks,
    terminal_value=DRYLAND_TERMINAL_VALUE
)


# ============================================
# EMBER MUSHROOM
# Uncomment if you want both in one script
# ============================================

"""
ember_bids = {
    20: 43000,
    19: 17000,
    18:  6000,
    17:  5000,
    16: 10000,
    15:  5000,
    14: 10000,
    13:  7000,
}

ember_asks = {
    12: 20000,
    13: 25000,
    14: 35000,
    15:  6000,
    16:  5000,
    17:     0,
    18: 10000,
    19: 12000,
}

EMBER_TERMINAL_VALUE = 20.0

print_results_table(
    product_name="Ember Mushroom",
    bids=ember_bids,
    asks=ember_asks,
    terminal_value=EMBER_TERMINAL_VALUE
)
"""
