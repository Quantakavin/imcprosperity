# Dryland Flax auction calculator
# Tries every bid price and quantity up to max_qty, then finds:
# - clearing price
# - your fill
# - total profit
#
# Assumptions:
# - you submit a BUY order
# - you are last in time priority at your chosen price level
# - all fills happen at the single auction clearing price
# - after auction, all bought inventory is sold at 30
# - no fees

# Existing order book
bids = {
    30: 30000,
    29: 5000,
    28: 12000,
    27: 28000,
}

asks = {
    28: 40000,
    31: 20000,
    32: 20000,
    33: 30000,
}

BUYBACK_PRICE = 30.0
FEE_PER_UNIT = 0.0
MAX_QTY = 75000


def traded_volume_at_price(price, all_bids, all_asks):
    demand = sum(q for p, q in all_bids.items() if p >= price)
    supply = sum(q for p, q in all_asks.items() if p <= price)
    return min(demand, supply)


def find_clearing_price(all_bids, all_asks):
    prices = sorted(set(all_bids.keys()) | set(all_asks.keys()))
    best_price = None
    best_volume = -1

    for p in prices:
        vol = traded_volume_at_price(p, all_bids, all_asks)
        if best_price is None or vol > best_volume or (vol == best_volume and p > best_price):
            best_volume = vol
            best_price = p

    return best_price, best_volume


def my_fill_if_i_bid(my_price, my_qty, base_bids, base_asks):
    # Add your bid to the book
    all_bids = dict(base_bids)
    all_bids[my_price] = all_bids.get(my_price, 0) + my_qty

    clearing_price, total_traded = find_clearing_price(all_bids, base_asks)

    # If your bid is below clearing price, you do not trade
    if my_price < clearing_price:
        return clearing_price, total_traded, 0

    total_supply = sum(q for p, q in base_asks.items() if p <= clearing_price)

    # Buyers with strictly better prices go first
    better_bids = sum(q for p, q in all_bids.items() if p >
                      my_price and p >= clearing_price)

    # Earlier orders at your same price level go before you
    earlier_same_price = base_bids.get(
        my_price, 0) if my_price >= clearing_price else 0

    remaining_for_you = total_supply - better_bids - earlier_same_price
    my_fill = max(0, min(my_qty, remaining_for_you))

    return clearing_price, total_traded, my_fill


def my_profit_if_i_bid(my_price, my_qty, base_bids, base_asks):
    clearing_price, total_traded, my_fill = my_fill_if_i_bid(
        my_price, my_qty, base_bids, base_asks
    )

    profit_per_unit = BUYBACK_PRICE - FEE_PER_UNIT - clearing_price
    profit = my_fill * profit_per_unit

    return {
        "bid_price": my_price,
        "bid_qty": my_qty,
        "clearing_price": clearing_price,
        "auction_volume": total_traded,
        "my_fill": my_fill,
        "profit_per_unit": profit_per_unit,
        "profit": profit,
    }


def brute_force_best_bid(base_bids, base_asks, max_qty=75000):
    prices = sorted(set(base_bids.keys()) | set(base_asks.keys()))
    best = None

    for price in prices:
        for qty in range(1, max_qty + 1):
            result = my_profit_if_i_bid(price, qty, base_bids, base_asks)

            if (
                best is None
                or result["profit"] > best["profit"]
                or (
                    abs(result["profit"] - best["profit"]) < 1e-12
                    and result["bid_qty"] < best["bid_qty"]
                )
            ):
                best = result

    return best


if __name__ == "__main__":
    print("\nSearching for best order...")
    best = brute_force_best_bid(bids, asks, MAX_QTY)

    print("\nBest BUY order found:")
    print(f"  Bid price      : {best['bid_price']}")
    print(f"  Bid quantity   : {best['bid_qty']}")
    print(f"  Clearing price : {best['clearing_price']}")
    print(f"  Your fill      : {best['my_fill']}")
    print(f"  Profit / unit  : {best['profit_per_unit']:.2f}")
    print(f"  Total profit   : {best['profit']:.2f}")
