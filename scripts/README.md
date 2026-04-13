# Scripts

Helper scripts for parsing and visualizing IMC Prosperity CSV data.

## Prerequisites

```bash
pip install pandas numpy matplotlib
```

## parse_data.py

Core loader — use this directly or import from other scripts.

```bash
# Summarize all CSVs in src/data/
python3 parse_data.py

# Summarize a single file
python3 parse_data.py --prices ../src/data/prices_round_0_day_-1.csv
python3 parse_data.py --trades ../src/data/trades_round_0_day_-1.csv

# Filter to one product
python3 parse_data.py --product EMERALDS

# Summary stats only (no row previews)
python3 parse_data.py --summary
```

**Functions you can import:**

```python
from parse_data import load_prices, load_trades, load_directory
from parse_data import filter_product, filter_timerange, get_mid_prices, get_best_bid_ask, compute_vwap

# Load a file
prices = load_prices("../src/data/prices_round_0_day_-1.csv")
trades = load_trades("../src/data/trades_round_0_day_-1.csv")

# Load all CSVs in a directory
datasets = load_directory("../src/data/")
# datasets = {"prices_round_0_day_-1": df, "trades_round_0_day_-1": df, ...}

# Filter
emerald_prices = filter_product(prices, "EMERALDS")
early_trades   = filter_timerange(trades, 0, 10000)

# Get clean series
mid = get_mid_prices(prices, "EMERALDS")   # pd.Series indexed by timestamp
book = get_best_bid_ask(prices, "TOMATOES") # DataFrame: bid1, ask1, spread

# VWAP
vwap = compute_vwap(trades, "EMERALDS")               # overall
rolling = compute_vwap(trades, "TOMATOES", window=50) # rolling 50 steps
```

## analyze_prices.py

Plots mid-price, bid-ask spread, order book depth, and return distribution.

```bash
python3 analyze_prices.py                          # all files in src/data/
python3 analyze_prices.py --product EMERALDS       # single product
python3 analyze_prices.py --no-plot                # stats only, no charts
python3 analyze_prices.py --file path/prices.csv   # specific file
```

Saves charts as `price_analysis_<product>.png` in the current directory.

## analyze_trades.py

Plots trade prices over time, volume bars, trade size distribution, and buyer/seller breakdown.

```bash
python3 analyze_trades.py                          # all files in src/data/
python3 analyze_trades.py --product TOMATOES       # single product
python3 analyze_trades.py --no-plot                # stats only
python3 analyze_trades.py --file path/trades.csv   # specific file
```

Saves charts as `trade_analysis_<product>.png` in the current directory.
