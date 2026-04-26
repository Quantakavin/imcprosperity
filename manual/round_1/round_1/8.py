# Ember Mushroom

bids = {
    20: 43000,
    19: 17000,
    18: 6000,
    17: 5000,
    16: 10000,
    15: 5000,
    14: 10000,
    13: 7000
}

asks = {
    12: 20000,
    13: 25000,
    14: 35000,
    15: 6000,
    16: 5000,
    17: 0,
    18: 10000,
    19: 12000
}

BUYBACK = 20
FEE = 0.10
MAX_QTY = 75000


def get_volume(price, all_bids, all_asks):
    buy = 0
    sell = 0

    for p in all_bids:
        if p >= price:
            buy += all_bids[p]

    for p in all_asks:
        if p <= price:
            sell += all_asks[p]

    return min(buy, sell)


def get_clearing_price(all_bids, all_asks):
    prices = sorted(set(all_bids) | set(all_asks))
    best_price = -1
    best_volume = -1

    for price in prices:
        volume = get_volume(price, all_bids, all_asks)
        if volume > best_volume or (volume == best_volume and price > best_price):
            best_volume = volume
            best_price = price

    return best_price


def get_fill(my_price, my_qty):
    all_bids = bids.copy()
    all_bids[my_price] = all_bids.get(my_price, 0) + my_qty

    clearing_price = get_clearing_price(all_bids, asks)

    if my_price < clearing_price:
        return clearing_price, 0

    supply = 0
    for p in asks:
        if p <= clearing_price:
            supply += asks[p]

    before_me = 0

    for p in all_bids:
        if p > my_price and p >= clearing_price:
            before_me += all_bids[p]

    if my_price >= clearing_price:
        before_me += bids.get(my_price, 0)

    fill = min(my_qty, max(0, supply - before_me))
    return clearing_price, fill


best_profit = -1
best_price = -1
best_qty = -1

for price in range(12, 21):
    for qty in range(1, MAX_QTY + 1):
        clearing_price, fill = get_fill(price, qty)
        profit = fill * (BUYBACK - FEE - clearing_price)

        if profit > best_profit:
            best_profit = profit
            best_price = price
            best_qty = qty

print("Best bid price:", best_price)
print("Best quantity:", best_qty)
print("Best profit:", best_profit)
