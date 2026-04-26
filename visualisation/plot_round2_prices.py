"""Plot price evolution for Round 2 products (ASH_COATED_OSMIUM, INTARIAN_PEPPER_ROOT)."""

import pandas as pd
import matplotlib.pyplot as plt

DATA_DIR = "../src/data"
DAYS = [-1, 0, 1]

# Load and concatenate all days
frames = []
for day in DAYS:
    path = f"{DATA_DIR}/prices_round_2_day_{day}.csv"
    df = pd.read_csv(path, sep=";")
    frames.append(df)

prices = pd.concat(frames, ignore_index=True)

# Build a global timestamp: each day has timestamps 0..999900 (step 100).
# Offset each day so they appear sequentially on the x-axis.
day_len = prices.groupby("day")["timestamp"].max().max() + 100  # one step past last
prices["global_ts"] = prices["day"] * day_len + prices["timestamp"]

products = prices["product"].unique()

# --- Figure 1: Mid-price evolution ---
fig, axes = plt.subplots(len(products), 1, figsize=(14, 5 * len(products)), sharex=True)
if len(products) == 1:
    axes = [axes]

for ax, product in zip(axes, sorted(products)):
    subset = prices[prices["product"] == product].sort_values("global_ts")
    # Drop rows where mid_price is 0 (no quotes)
    subset = subset[subset["mid_price"] > 0]

    ax.plot(subset["global_ts"], subset["mid_price"], linewidth=0.6, label="Mid price")
    ax.set_ylabel("Mid Price")
    ax.set_title(f"{product} — Mid Price Evolution")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Mark day boundaries
    for d in DAYS:
        ax.axvline(x=d * day_len, color="grey", linestyle="--", alpha=0.5)
        ax.text(d * day_len + day_len * 0.01, ax.get_ylim()[0], f"Day {d}",
                fontsize=8, color="grey", va="bottom")

axes[-1].set_xlabel("Global Timestamp")
plt.tight_layout()
plt.savefig("round2_mid_prices.png", dpi=150)
print("Saved round2_mid_prices.png")

# --- Figure 2: Bid/Ask spread ---
fig2, axes2 = plt.subplots(len(products), 1, figsize=(14, 5 * len(products)), sharex=True)
if len(products) == 1:
    axes2 = [axes2]

for ax, product in zip(axes2, sorted(products)):
    subset = prices[prices["product"] == product].sort_values("global_ts")
    has_quotes = (subset["bid_price_1"] > 0) & (subset["ask_price_1"] > 0)
    subset = subset[has_quotes]

    ax.fill_between(subset["global_ts"], subset["bid_price_1"], subset["ask_price_1"],
                    alpha=0.3, label="Bid-Ask spread")
    ax.plot(subset["global_ts"], subset["mid_price"], linewidth=0.5, color="black", label="Mid price")
    ax.set_ylabel("Price")
    ax.set_title(f"{product} — Bid/Ask Spread")
    ax.legend()
    ax.grid(True, alpha=0.3)

    for d in DAYS:
        ax.axvline(x=d * day_len, color="grey", linestyle="--", alpha=0.5)

axes2[-1].set_xlabel("Global Timestamp")
plt.tight_layout()
plt.savefig("round2_bid_ask_spread.png", dpi=150)
print("Saved round2_bid_ask_spread.png")

# --- Figure 3: Trade prices overlaid ---
trade_frames = []
for day in DAYS:
    path = f"{DATA_DIR}/trades_round_2_day_{day}.csv"
    df = pd.read_csv(path, sep=";")
    df["day"] = day
    trade_frames.append(df)

trades = pd.concat(trade_frames, ignore_index=True)
trades["global_ts"] = trades["day"] * day_len + trades["timestamp"]

fig3, axes3 = plt.subplots(len(products), 1, figsize=(14, 5 * len(products)), sharex=True)
if len(products) == 1:
    axes3 = [axes3]

for ax, product in zip(axes3, sorted(products)):
    # Mid price line
    p_sub = prices[(prices["product"] == product) & (prices["mid_price"] > 0)].sort_values("global_ts")
    ax.plot(p_sub["global_ts"], p_sub["mid_price"], linewidth=0.5, color="blue", alpha=0.6, label="Mid price")

    # Trade scatter
    t_sub = trades[trades["symbol"] == product]
    ax.scatter(t_sub["global_ts"], t_sub["price"], s=t_sub["quantity"] * 2,
               color="red", alpha=0.5, label="Trades (size ~ qty)")

    ax.set_ylabel("Price")
    ax.set_title(f"{product} — Mid Price + Trades")
    ax.legend()
    ax.grid(True, alpha=0.3)

    for d in DAYS:
        ax.axvline(x=d * day_len, color="grey", linestyle="--", alpha=0.5)

axes3[-1].set_xlabel("Global Timestamp")
plt.tight_layout()
plt.savefig("round2_trades_overlay.png", dpi=150)
print("Saved round2_trades_overlay.png")

plt.show()
