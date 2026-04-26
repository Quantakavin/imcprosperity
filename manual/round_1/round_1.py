from collections import defaultdict

# -----------------------------
# STALE ORDER BOOK: EMBER MUSHROOM
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

TERMINAL_VALUE = 20.0
BUY_FEE = 0.05
SELL_FEE = 0.05


def cumulative_bid_volume(book, price):
    """Volume willing to buy at or above this price."""
    return sum(v for p, v in book.items() if p >= price)


def cumulative_ask_volume(book, price):
    """Volume willing to sell at or below this price."""
    return sum(v for p, v in book.items() if p <= price)


def clearing_candidates(bids_book, asks_book):
    """All candidate prices visible in the combined book."""
    prices = sorted(set(bids_book.keys()) | set(asks_book.keys()))
    return prices


def find_clearing_price_and_volume(bids_book, asks_book):
    """
    Choose the clearing price as the price that maximizes matched volume:
        matched_volume(price) = min(cum_bids(price), cum_asks(price))

    If there are ties, choose the lowest tied price.
    You can change the tie-break if the auction uses a different rule.
    """
    best_price = None
    best_matched = -1

    for price in clearing_candidates(bids_book, asks_book):
        demand = cumulative_bid_volume(bids_book, price)
        supply = cumulative_ask_volume(asks_book, price)
        matched = min(demand, supply)

        if matched > best_matched:
            best_matched = matched
            best_price = price
        elif matched == best_matched and best_price is not None and price < best_price:
            best_price = price

    return best_price, best_matched


def my_buy_fill(my_price, my_qty, clearing_price, total_matched, base_bids):
    """
    Compute how much of MY buy order gets filled under price-time priority.
    Assumption: all stale book orders are ahead of me in time priority.
    """
    if my_price < clearing_price:
        return 0

    # Orders strictly better than mine get priority before me
    better_bid_volume = sum(v for p, v in base_bids.items() if p > my_price)

    # Same-price stale bids are also ahead of me
    same_price_ahead = base_bids.get(my_price, 0)

    # Everyone better than the clearing price definitely participates.
    # For my own price level, if my_price > clearing_price, then all bids at my price are better than clearing price.
    # If my_price == clearing_price, I am at the marginal level.
    ahead_of_me = better_bid_volume + same_price_ahead

    remaining_for_me = total_matched - ahead_of_me
    return max(0, min(my_qty, remaining_for_me))


def profit_for_buy_order(my_price, my_qty, base_bids, base_asks):
    """
    Add my buy order, clear the auction, compute my fill, then profit.
    """
    new_bids = dict(base_bids)
    new_bids[my_price] = new_bids.get(my_price, 0) + my_qty

    clearing_price, total_matched = find_clearing_price_and_volume(
        new_bids, base_asks)
    filled = my_buy_fill(my_price, my_qty, clearing_price,
                         total_matched, base_bids)

    # Buy at clearing_price, then auto-sell later at TERMINAL_VALUE
    pnl_per_unit = TERMINAL_VALUE - clearing_price - BUY_FEE - SELL_FEE
    total_profit = filled * pnl_per_unit

    return {
        "my_price": my_price,
        "my_qty": my_qty,
        "clearing_price": clearing_price,
        "matched_volume": total_matched,
        "my_fill": filled,
        "pnl_per_unit": pnl_per_unit,
        "total_profit": total_profit,
    }


def brute_force_best_buy(base_bids, base_asks, max_qty=150000, qty_step=1000, price_min=1, price_max=25):
    best = None

    for price in range(price_min, price_max + 1):
        for qty in range(0, max_qty + 1, qty_step):
            result = profit_for_buy_order(price, qty, base_bids, base_asks)

            if best is None or result["total_profit"] > best["total_profit"]:
                best = result

    return best


# -----------------------------
# RUN SEARCH
# -----------------------------
best = brute_force_best_buy(
    bids,
    asks,
    max_qty=150000,
    qty_step=1000,
    price_min=1,
    price_max=25
)

print("BEST BUY ORDER FOUND")
for k, v in best.items():
    print(f"{k}: {v}")
