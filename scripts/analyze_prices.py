"""
analyze_prices.py — Visualize price history, spreads, and order book depth.

Usage:
    python3 analyze_prices.py                         # all files in src/data/
    python3 analyze_prices.py --file path/prices.csv
    python3 analyze_prices.py --product EMERALDS
    python3 analyze_prices.py --no-plot               # print stats, skip charts
"""

import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
except ImportError:
    print("Missing dependencies. Run: pip install pandas numpy matplotlib")
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from parse_data import load_prices, load_directory, filter_product

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "src" / "data"


def plot_prices(df: pd.DataFrame, product: str, title_prefix: str = "") -> None:
    """Plot mid-price, spread, and order book depth for one product."""
    sub = filter_product(df, product)
    if sub.empty:
        print(f"  No data for {product}")
        return

    sub = sub.sort_values("timestamp")
    ts = sub["timestamp"]

    fig = plt.figure(figsize=(14, 9))
    fig.suptitle(f"{title_prefix}{product} — Price Analysis", fontsize=14, fontweight="bold")
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)

    # ── Mid-price over time ──────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(ts, sub["mid_price"], linewidth=1.2, color="steelblue", label="Mid price")
    if "bid_price_1" in sub.columns:
        ax1.fill_between(ts, sub["bid_price_1"], sub["ask_price_1"],
                         alpha=0.15, color="steelblue", label="Bid-ask range")
    ax1.set_title("Mid-price (with bid-ask band)")
    ax1.set_xlabel("Timestamp")
    ax1.set_ylabel("Price")
    ax1.legend(fontsize=8)
    ax1.grid(True, alpha=0.3)

    # ── Spread over time ─────────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    if "spread" in sub.columns:
        ax2.plot(ts, sub["spread"], linewidth=1, color="darkorange")
        ax2.axhline(sub["spread"].mean(), color="red", linestyle="--", linewidth=0.8, label=f"Mean {sub['spread'].mean():.2f}")
        ax2.set_title("Bid-Ask Spread")
        ax2.set_xlabel("Timestamp")
        ax2.set_ylabel("Spread (ticks)")
        ax2.legend(fontsize=8)
        ax2.grid(True, alpha=0.3)

    # ── Spread histogram ─────────────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    if "spread" in sub.columns:
        ax3.hist(sub["spread"].dropna(), bins=30, color="darkorange", edgecolor="white", linewidth=0.5)
        ax3.set_title("Spread Distribution")
        ax3.set_xlabel("Spread")
        ax3.set_ylabel("Count")
        ax3.grid(True, alpha=0.3)

    # ── Order book depth over time ───────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, 0])
    if "depth_bid" in sub.columns and "depth_ask" in sub.columns:
        ax4.stackplot(ts, sub["depth_bid"], sub["depth_ask"],
                      labels=["Bid depth", "Ask depth"],
                      colors=["#2ecc71", "#e74c3c"], alpha=0.7)
        ax4.set_title("Order Book Depth")
        ax4.set_xlabel("Timestamp")
        ax4.set_ylabel("Total volume")
        ax4.legend(fontsize=8)
        ax4.grid(True, alpha=0.3)

    # ── Mid-price returns distribution ───────────────────────────────────────
    ax5 = fig.add_subplot(gs[2, 1])
    returns = sub["mid_price"].diff().dropna()
    ax5.hist(returns, bins=40, color="steelblue", edgecolor="white", linewidth=0.5)
    ax5.set_title("Mid-price Tick-to-Tick Changes")
    ax5.set_xlabel("Δ Mid-price")
    ax5.set_ylabel("Count")
    ax5.axvline(0, color="red", linestyle="--", linewidth=0.8)
    ax5.grid(True, alpha=0.3)

    plt.savefig(f"price_analysis_{product.lower()}.png", dpi=150, bbox_inches="tight")
    print(f"  Chart saved: price_analysis_{product.lower()}.png")
    plt.show()


def print_price_stats(df: pd.DataFrame, product: str) -> None:
    sub = filter_product(df, product)
    if sub.empty:
        return
    sub = sub.sort_values("timestamp")

    print(f"\n{'─'*50}")
    print(f"  {product}")
    print(f"{'─'*50}")

    mid = sub["mid_price"]
    print(f"  Mid-price:  mean={mid.mean():.4f}  std={mid.std():.4f}"
          f"  min={mid.min():.2f}  max={mid.max():.2f}")

    if "spread" in sub.columns:
        sp = sub["spread"]
        print(f"  Spread:     mean={sp.mean():.4f}  std={sp.std():.4f}"
              f"  min={sp.min():.0f}  max={sp.max():.0f}")

    if "depth_bid" in sub.columns:
        print(f"  Bid depth:  mean={sub['depth_bid'].mean():.1f}")
        print(f"  Ask depth:  mean={sub['depth_ask'].mean():.1f}")

    returns = mid.diff().dropna()
    print(f"  Δ mid:      mean={returns.mean():.6f}  std={returns.std():.4f}")
    print(f"  Steps:      {len(sub):,}")


def main():
    parser = argparse.ArgumentParser(description="Analyze IMC price data")
    parser.add_argument("--file", help="Path to a single prices CSV")
    parser.add_argument("--dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--product", help="Only analyze this product (e.g. EMERALDS)")
    parser.add_argument("--no-plot", action="store_true", help="Skip charts, print stats only")
    args = parser.parse_args()

    if args.file:
        datasets = {"file": load_prices(args.file)}
    else:
        data_dir = Path(args.dir)
        all_data = load_directory(data_dir)
        datasets = {k: v for k, v in all_data.items() if "prices" in k}

    if not datasets:
        print("No price files found.")
        sys.exit(1)

    for name, df in datasets.items():
        products = [args.product.upper()] if args.product else df["product"].unique().tolist()
        print(f"\n=== {name} ===")
        for product in products:
            print_price_stats(df, product)
            if not args.no_plot:
                plot_prices(df, product, title_prefix=f"{name} — ")

    print()


if __name__ == "__main__":
    main()
