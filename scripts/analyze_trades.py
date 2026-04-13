"""
analyze_trades.py — Analyze trade flow: volume, frequency, VWAP, buyer/seller breakdown.

Usage:
    python3 analyze_trades.py                         # all files in src/data/
    python3 analyze_trades.py --file path/trades.csv
    python3 analyze_trades.py --product TOMATOES
    python3 analyze_trades.py --no-plot
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
from parse_data import load_trades, load_directory, filter_product, compute_vwap

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "src" / "data"


def print_trade_stats(df: pd.DataFrame, product: str) -> None:
    sub = filter_product(df, product)
    if sub.empty:
        print(f"  No trade data for {product}")
        return

    print(f"\n{'─'*50}")
    print(f"  {product}")
    print(f"{'─'*50}")

    total_qty = sub["quantity"].sum()
    vwap = (sub["price"] * sub["quantity"]).sum() / total_qty if total_qty > 0 else float("nan")

    print(f"  Total trades:  {len(sub):,}")
    print(f"  Total volume:  {int(total_qty):,}")
    print(f"  VWAP:          {vwap:.4f}")
    print(f"  Price range:   [{sub['price'].min():.2f}, {sub['price'].max():.2f}]")
    print(f"  Avg size:      {sub['quantity'].mean():.2f}")

    # Buyer/seller breakdown
    buyers = sub["buyer"].value_counts()
    sellers = sub["seller"].value_counts()
    if not buyers.empty:
        print(f"  Top buyers:    {dict(buyers.head(5))}")
    if not sellers.empty:
        print(f"  Top sellers:   {dict(sellers.head(5))}")

    # Trade frequency per timestamp bucket
    trades_per_step = sub.groupby("timestamp")["quantity"].sum()
    print(f"  Steps with trades:  {len(trades_per_step):,}")
    print(f"  Avg vol/step:  {trades_per_step.mean():.2f}")


def plot_trades(df: pd.DataFrame, product: str, title_prefix: str = "") -> None:
    sub = filter_product(df, product)
    if sub.empty:
        return
    sub = sub.sort_values("timestamp")

    fig = plt.figure(figsize=(14, 9))
    fig.suptitle(f"{title_prefix}{product} — Trade Analysis", fontsize=14, fontweight="bold")
    gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.45, wspace=0.35)

    # ── Trade price over time ────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[0, :])
    ax1.scatter(sub["timestamp"], sub["price"], s=sub["quantity"] * 2,
                alpha=0.5, color="steelblue", edgecolors="none", label="Trade price")
    ax1.set_title("Trade Price Over Time (dot size = quantity)")
    ax1.set_xlabel("Timestamp")
    ax1.set_ylabel("Price")
    ax1.grid(True, alpha=0.3)

    # VWAP line (rolling 50 steps)
    vwap_ts = compute_vwap(sub, product, window=50)
    if not vwap_ts.empty:
        ax1.plot(vwap_ts.index, vwap_ts.values, color="red", linewidth=1.2,
                 label="Rolling VWAP (50)", zorder=5)
        ax1.legend(fontsize=8)

    # ── Volume per timestamp ─────────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[1, 0])
    vol_by_ts = sub.groupby("timestamp")["quantity"].sum()
    ax2.bar(vol_by_ts.index, vol_by_ts.values, width=80, color="steelblue", alpha=0.7)
    ax2.set_title("Volume Per Timestamp")
    ax2.set_xlabel("Timestamp")
    ax2.set_ylabel("Total Quantity")
    ax2.grid(True, alpha=0.3)

    # ── Trade size distribution ──────────────────────────────────────────────
    ax3 = fig.add_subplot(gs[1, 1])
    ax3.hist(sub["quantity"], bins=30, color="steelblue", edgecolor="white", linewidth=0.5)
    ax3.set_title("Trade Size Distribution")
    ax3.set_xlabel("Quantity")
    ax3.set_ylabel("Count")
    ax3.grid(True, alpha=0.3)

    # ── Buyer/seller activity ────────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[2, 0])
    buyers = sub["buyer"].fillna("(anon)").value_counts().head(8)
    colors = ["#2ecc71" if b != "(anon)" else "#95a5a6" for b in buyers.index]
    ax4.barh(buyers.index, buyers.values, color=colors)
    ax4.set_title("Top Buyers (by trade count)")
    ax4.set_xlabel("Trades")
    ax4.grid(True, alpha=0.3)

    ax5 = fig.add_subplot(gs[2, 1])
    sellers = sub["seller"].fillna("(anon)").value_counts().head(8)
    colors = ["#e74c3c" if s != "(anon)" else "#95a5a6" for s in sellers.index]
    ax5.barh(sellers.index, sellers.values, color=colors)
    ax5.set_title("Top Sellers (by trade count)")
    ax5.set_xlabel("Trades")
    ax5.grid(True, alpha=0.3)

    plt.savefig(f"trade_analysis_{product.lower()}.png", dpi=150, bbox_inches="tight")
    print(f"  Chart saved: trade_analysis_{product.lower()}.png")
    plt.show()


def main():
    parser = argparse.ArgumentParser(description="Analyze IMC trade data")
    parser.add_argument("--file", help="Path to a single trades CSV")
    parser.add_argument("--dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--product", help="Only analyze this product")
    parser.add_argument("--no-plot", action="store_true", help="Skip charts, print stats only")
    args = parser.parse_args()

    if args.file:
        datasets = {"file": load_trades(args.file)}
    else:
        data_dir = Path(args.dir)
        all_data = load_directory(data_dir)
        datasets = {k: v for k, v in all_data.items() if "trades" in k}

    if not datasets:
        print("No trade files found.")
        sys.exit(1)

    for name, df in datasets.items():
        products = [args.product.upper()] if args.product else df["product"].unique().tolist()
        print(f"\n=== {name} ===")
        for product in products:
            print_trade_stats(df, product)
            if not args.no_plot:
                plot_trades(df, product, title_prefix=f"{name} — ")

    print()


if __name__ == "__main__":
    main()
