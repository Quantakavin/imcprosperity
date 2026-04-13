"""
parse_data.py — Load and inspect IMC Prosperity CSV data files.

Usage:
    python3 parse_data.py                          # load all files in src/data/
    python3 parse_data.py --prices path/to/prices.csv
    python3 parse_data.py --trades path/to/trades.csv
    python3 parse_data.py --dir path/to/data/dir   # load all CSVs in directory
    python3 parse_data.py --summary                # print summary stats only
"""

import argparse
import glob
import os
import sys
from pathlib import Path

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("Missing dependencies. Run: pip install pandas numpy")
    sys.exit(1)

# ── Default data directory ────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "src" / "data"


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_prices(path: str | Path) -> pd.DataFrame:
    """Load a prices CSV into a clean DataFrame.

    Adds computed columns: spread, depth_bid, depth_ask, total_depth.
    """
    df = pd.read_csv(path, sep=";")
    df.columns = df.columns.str.strip()

    # Numeric coercion for price/volume columns
    price_cols = [c for c in df.columns if "price" in c or "volume" in c or c == "profit_and_loss"]
    for col in price_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Derived columns
    if "bid_price_1" in df.columns and "ask_price_1" in df.columns:
        df["spread"] = df["ask_price_1"] - df["bid_price_1"]

    bid_vol_cols = [c for c in df.columns if c.startswith("bid_volume")]
    ask_vol_cols = [c for c in df.columns if c.startswith("ask_volume")]
    if bid_vol_cols:
        df["depth_bid"] = df[bid_vol_cols].sum(axis=1)
    if ask_vol_cols:
        df["depth_ask"] = df[ask_vol_cols].sum(axis=1)
    if bid_vol_cols and ask_vol_cols:
        df["total_depth"] = df["depth_bid"] + df["depth_ask"]

    return df


def load_trades(path: str | Path) -> pd.DataFrame:
    """Load a trades CSV into a clean DataFrame."""
    df = pd.read_csv(path, sep=";")
    df.columns = df.columns.str.strip()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    # Rename 'symbol' → 'product' to match prices convention
    if "symbol" in df.columns and "product" not in df.columns:
        df = df.rename(columns={"symbol": "product"})
    return df


def load_directory(data_dir: str | Path) -> dict[str, pd.DataFrame]:
    """Load all price and trade CSVs from a directory.

    Returns dict with keys like 'prices_round_0_day_-1', 'trades_round_0_day_-1'.
    """
    data_dir = Path(data_dir)
    result = {}

    for path in sorted(data_dir.glob("*.csv")):
        name = path.stem
        if "prices" in name:
            result[name] = load_prices(path)
        elif "trades" in name:
            result[name] = load_trades(path)

    return result


# ── Summary helpers ───────────────────────────────────────────────────────────

def summarize_prices(df: pd.DataFrame, name: str = "") -> None:
    """Print a human-readable summary of a prices DataFrame."""
    label = f"[{name}] " if name else ""
    products = df["product"].unique() if "product" in df.columns else ["(unknown)"]
    timestamps = df["timestamp"].nunique() if "timestamp" in df.columns else "?"

    print(f"\n{label}PRICES  rows={len(df):,}  products={list(products)}  steps={timestamps}")
    print(f"  columns: {list(df.columns)}")

    for product in products:
        sub = df[df["product"] == product] if "product" in df.columns else df
        mid = sub["mid_price"] if "mid_price" in sub.columns else pd.Series(dtype=float)
        spread = sub["spread"] if "spread" in sub.columns else pd.Series(dtype=float)

        if not mid.empty:
            print(f"  {product}: mid_price  min={mid.min():.2f}  max={mid.max():.2f}"
                  f"  mean={mid.mean():.2f}  std={mid.std():.2f}")
        if not spread.empty:
            print(f"  {product}: spread     min={spread.min():.0f}  max={spread.max():.0f}"
                  f"  mean={spread.mean():.2f}")


def summarize_trades(df: pd.DataFrame, name: str = "") -> None:
    """Print a human-readable summary of a trades DataFrame."""
    label = f"[{name}] " if name else ""
    products = df["product"].unique() if "product" in df.columns else ["(unknown)"]

    print(f"\n{label}TRADES  rows={len(df):,}  products={list(products)}")

    for product in products:
        sub = df[df["product"] == product] if "product" in df.columns else df
        vwap = (sub["price"] * sub["quantity"]).sum() / sub["quantity"].sum() if not sub.empty else float("nan")
        print(f"  {product}: trades={len(sub):,}  total_qty={int(sub['quantity'].sum()):,}"
              f"  vwap={vwap:.2f}  price_range=[{sub['price'].min():.0f}, {sub['price'].max():.0f}]")


# ── Filtering helpers ─────────────────────────────────────────────────────────

def filter_product(df: pd.DataFrame, product: str) -> pd.DataFrame:
    """Return rows for a specific product."""
    col = "product" if "product" in df.columns else "symbol"
    return df[df[col].str.upper() == product.upper()].copy()


def filter_timerange(df: pd.DataFrame, t_start: int, t_end: int) -> pd.DataFrame:
    """Return rows within a timestamp range (inclusive)."""
    return df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)].copy()


def get_mid_prices(prices_df: pd.DataFrame, product: str) -> pd.Series:
    """Return mid_price series for one product, indexed by timestamp."""
    sub = filter_product(prices_df, product)
    return sub.set_index("timestamp")["mid_price"]


def get_best_bid_ask(prices_df: pd.DataFrame, product: str) -> pd.DataFrame:
    """Return best bid/ask/spread for one product, indexed by timestamp."""
    sub = filter_product(prices_df, product)
    cols = ["timestamp", "bid_price_1", "ask_price_1", "mid_price"]
    if "spread" in sub.columns:
        cols.append("spread")
    return sub[cols].set_index("timestamp")


def compute_vwap(trades_df: pd.DataFrame, product: str,
                 window: int | None = None) -> pd.Series:
    """Compute VWAP for a product over all trades (or a rolling window of steps)."""
    sub = filter_product(trades_df, product).sort_values("timestamp")
    if sub.empty:
        return pd.Series(dtype=float)

    sub["pxq"] = sub["price"] * sub["quantity"]

    if window is None:
        overall = sub["pxq"].sum() / sub["quantity"].sum()
        print(f"  Overall VWAP for {product}: {overall:.4f}")
        return pd.Series([overall], name="vwap")

    # Rolling VWAP by timestamp groups
    grouped = sub.groupby("timestamp").agg(pxq=("pxq", "sum"), qty=("quantity", "sum"))
    rolling_vwap = grouped["pxq"].rolling(window).sum() / grouped["qty"].rolling(window).sum()
    rolling_vwap.name = "vwap"
    return rolling_vwap


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Load and inspect IMC Prosperity CSV data")
    parser.add_argument("--prices", help="Path to a single prices CSV file")
    parser.add_argument("--trades", help="Path to a single trades CSV file")
    parser.add_argument("--dir", default=str(DEFAULT_DATA_DIR), help="Directory with CSV files")
    parser.add_argument("--summary", action="store_true", help="Print summary stats only")
    parser.add_argument("--product", help="Filter to a specific product (e.g. EMERALDS)")
    args = parser.parse_args()

    if args.prices:
        df = load_prices(args.prices)
        summarize_prices(df, name=Path(args.prices).stem)
        if args.product:
            df = filter_product(df, args.product)
            print(f"\nFiltered to {args.product}:\n{df.head(10).to_string(index=False)}")
        elif not args.summary:
            print(f"\nFirst 10 rows:\n{df.head(10).to_string(index=False)}")
        return

    if args.trades:
        df = load_trades(args.trades)
        summarize_trades(df, name=Path(args.trades).stem)
        if args.product:
            df = filter_product(df, args.product)
            print(f"\nFiltered to {args.product}:\n{df.head(10).to_string(index=False)}")
        elif not args.summary:
            print(f"\nFirst 10 rows:\n{df.head(10).to_string(index=False)}")
        return

    # Default: load entire directory
    data_dir = Path(args.dir)
    if not data_dir.exists():
        print(f"Data directory not found: {data_dir}")
        sys.exit(1)

    datasets = load_directory(data_dir)
    if not datasets:
        print(f"No CSV files found in {data_dir}")
        sys.exit(1)

    print(f"Loaded {len(datasets)} files from {data_dir}\n")
    for name, df in datasets.items():
        if "prices" in name:
            summarize_prices(df, name=name)
            if args.product:
                df2 = filter_product(df, args.product)
                if not df2.empty and not args.summary:
                    print(f"\n  {args.product} sample:\n{df2.head(5).to_string(index=False)}")
        elif "trades" in name:
            summarize_trades(df, name=name)
            if args.product:
                df2 = filter_product(df, args.product)
                if not df2.empty and not args.summary:
                    print(f"\n  {args.product} sample:\n{df2.head(5).to_string(index=False)}")

    print()


if __name__ == "__main__":
    main()
