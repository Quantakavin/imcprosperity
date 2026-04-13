# IMC Prosperity — Competition Workspace

Workspace for [IMC Prosperity](https://imc-prosperity.com/), a global algorithmic trading competition.

```
imcprosperity/
├── src/                       # Your strategies and local data
│   ├── algorithms/            # Trading algorithm files (write yours here)
│   └── data/                  # Tutorial round CSV data (EMERALDS + TOMATOES)
├── backtester/                # Prosperity 3 CSV-replay backtester
├── visualisation/             # Prosperity 4 Monte Carlo backtester + React dashboard
│   ├── backtester/            # Python CLI (prosperity4mcbt)
│   ├── rust_simulator/        # Rust Monte Carlo engine (required by prosperity4mcbt)
│   └── visualizer/            # React frontend for dashboards
└── scripts/                   # CSV parsing and analysis helpers
```

---

## Prerequisites

Before doing anything, make sure you have:

| Tool | Required for | Install |
|------|-------------|---------|
| Python 3.9+ | Both backtester tools, scripts | `brew install python` |
| Node.js + npm | Visualizer frontend | `brew install node` |
| Rust + Cargo | **prosperity4mcbt (Monte Carlo)** | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |

> **Rust is mandatory for `prosperity4mcbt`.** There is no Python fallback — it calls `cargo run --release` internally to run the simulation engine. Install it before trying to use the Monte Carlo tool.

---

## One-Time Setup

Run these once from the project root:

```bash
# 1. Backtester (Prosperity 3 CSV replay)
cd backtester
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
deactivate
cd ..

# 2. Monte Carlo backtester (Prosperity 4)
cd visualisation/backtester
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install matplotlib      # needed for the analysis scripts
deactivate
cd ../..

# 3. Visualizer frontend
cd visualisation/visualizer
npm install
cd ../..

# 4. Rust simulator (REQUIRED for prosperity4mcbt)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh  # if not already installed
source ~/.cargo/env                                                # or restart terminal
cd visualisation/rust_simulator
cargo build --release
cd ../..
```

After this, your directory will have:
- `backtester/.venv/`
- `visualisation/backtester/.venv/`
- `visualisation/visualizer/node_modules/`
- `visualisation/rust_simulator/target/release/` (Rust binary)

---

## Writing a Strategy

Create a `.py` file anywhere (e.g. `src/algorithms/mytrader.py`):

```python
from datamodel import TradingState, Order

class Trader:
    def run(self, state: TradingState):
        orders = {}       # dict[str, list[Order]]
        conversions = 0   # always 0 for tutorial round
        trader_data = ""  # persisted string between steps

        # Example: buy EMERALDS if ask is below fair value
        fv = 10000
        if "EMERALDS" in state.order_depths:
            depth = state.order_depths["EMERALDS"]
            for ask, vol in sorted(depth.sell_orders.items()):
                if ask < fv:
                    orders.setdefault("EMERALDS", []).append(Order("EMERALDS", ask, -vol))

        return orders, conversions, trader_data
```

See `src/algorithms/algorithm1.py` for a fully commented market-making example.

**Key state fields:**

| Field | Type | Description |
|-------|------|-------------|
| `state.order_depths` | `dict[str, OrderDepth]` | Current order book per product |
| `state.position` | `dict[str, int]` | Your current position per product |
| `state.own_trades` | `dict[str, list[Trade]]` | Your trades last step |
| `state.market_trades` | `dict[str, list[Trade]]` | All market trades last step |
| `state.traderData` | `str` | Your persisted state from previous step |
| `state.timestamp` | `int` | Current step (0, 100, 200, …) |

---

## Tool 1 — Prosperity 3 CSV Replay (`backtester/`)

Replays your strategy against the **exact historical order books** from previous Prosperity rounds. Fast and deterministic.

### Activate and run

```bash
cd backtester
source .venv/bin/activate

prosperity3bt ../src/algorithms/mytrader.py 0         # Tutorial round (all days)
prosperity3bt ../src/algorithms/mytrader.py 1         # Round 1 (all days)
prosperity3bt ../src/algorithms/mytrader.py 1-0       # Round 1, day 0 only
prosperity3bt ../src/algorithms/mytrader.py 1 2 3     # Rounds 1, 2, 3 back-to-back
prosperity3bt ../src/algorithms/mytrader.py 0 --vis   # Run + auto-open browser visualizer
```

### All flags

| Flag | Default | Description |
|------|---------|-------------|
| `<round>` | required | `0`–`8`, or `1-0` for round 1 day 0 |
| `--vis` | off | Open results in browser visualizer when done |
| `--out FILE` | `backtests/<timestamp>.log` | Write log to custom path |
| `--no-out` | off | Skip saving a log file |
| `--print` | off | Print log to stdout while running |
| `--merge-pnl` | off | Merge PnL across days (removes per-day resets) |
| `--match-trades all` | on | Match market trades at prices ≤ your quote |
| `--match-trades worse` | — | Only match trades priced worse than yours |
| `--match-trades none` | — | Never match market trades |
| `--no-progress` | off | Suppress progress bars |
| `--original-timestamps` | off | Keep original timestamps instead of incrementing across days |

### Available rounds

| Round | Products |
|-------|---------|
| 0 | RAINFOREST_RESIN, KELP (tutorial) |
| 1 | + SQUID_INK |
| 2–3 | + CROISSANTS, JAMS, DJEMBES, PICNIC_BASKET |
| 4–5 | + VOLCANIC_ROCK variants, MAGNIFICENT_MACARONS |
| 6 | Submission data with de-anonymized trades |
| 7 | End-of-round data |
| 8 | Pre-update Round 2 data |

---

## Tool 2 — Prosperity 4 Monte Carlo (`visualisation/`)

Runs **stochastic simulations** of the tutorial round (EMERALDS + TOMATOES) using calibrated bots and a Rust simulation engine. Produces a statistical dashboard.

> **Requires Rust/Cargo to be installed and the simulator to be built.** See the One-Time Setup section.

### Activate and run

```bash
cd visualisation/backtester
source .venv/bin/activate

prosperity4mcbt ../../src/algorithms/mytrader.py --quick              # 100 sessions, fast
prosperity4mcbt ../../src/algorithms/mytrader.py                      # 100 sessions (default)
prosperity4mcbt ../../src/algorithms/mytrader.py --heavy              # 1000 sessions, thorough
prosperity4mcbt ../../src/algorithms/mytrader.py --sessions 500       # custom count

# Save output to a specific directory + open dashboard in browser
prosperity4mcbt ../../src/algorithms/mytrader.py --quick \
  --out ../tmp/myrun/dashboard.json --vis
```

### All flags

| Flag | Default | Description |
|------|---------|-------------|
| `--quick` | off | Preset: 100 sessions, 10 sample paths |
| `--heavy` | off | Preset: 1000 sessions, 100 sample paths |
| `--sessions N` | 100 | Number of Monte Carlo sessions |
| `--sample-sessions N` | 10 | Sessions saved with full path traces for charts |
| `--out PATH` | `backtests/<timestamp>_monte_carlo/dashboard.json` | Output path |
| `--vis` | off | Open local dashboard in browser when done |
| `--seed N` | 20260401 | RNG seed for reproducibility |
| `--fv-mode MODE` | `simulate` | Fair-value mode (`simulate` or `replay`) |
| `--trade-mode MODE` | `simulate` | Trade-arrival mode (`simulate` or `replay`) |
| `--tomato-support MODE` | `quarter` | Tomato latent fair-value support |
| `--data DIR` | built-in | Custom calibration data directory |
| `--python-bin PATH` | current venv's python | Python interpreter for strategy worker |

> `--no-out` is **not supported** — the Monte Carlo tool always writes an output bundle.

### Output files

Running `prosperity4mcbt` creates a directory containing:

```
<output_dir>/
├── dashboard.json          # Main dashboard bundle — load this in the visualizer
├── session_summary.csv     # Per-session stats (PnL, Sharpe, drawdown, …)
├── run_summary.csv         # Aggregate stats (mean, std, P05/P95, …)
├── sample_paths/           # Full path traces for charted sessions
└── sessions/               # Complete logs for each session
```

### Dashboard metrics shown

- Total PnL distribution (histogram, mean, std, P05–P95)
- Per-product PnL breakdown (EMERALDS vs TOMATOES)
- Individual path traces
- Sharpe ratio, Stability (R²), max drawdown

### Visualizer frontend

Load a `dashboard.json` interactively:

```bash
cd visualisation/visualizer
npm run dev        # Dev server at http://localhost:5173/
```

Then open http://localhost:5173/ and load the `dashboard.json` file.

For a production build:
```bash
npm run build      # Outputs to dist/
```

---

## Tool 3 — Analysis Scripts (`scripts/`)

These scripts use the visualisation venv (which has pandas, numpy, matplotlib).

```bash
# Activate visualisation venv first
cd visualisation/backtester && source .venv/bin/activate && cd ../..

# Summarize all CSV files in src/data/
python3 scripts/parse_data.py

# Filter to one product
python3 scripts/parse_data.py --product EMERALDS

# Price charts (mid-price, spread, depth, returns)
python3 scripts/analyze_prices.py
python3 scripts/analyze_prices.py --product TOMATOES --no-plot   # stats only

# Trade charts (price over time, VWAP, volume, buyer/seller)
python3 scripts/analyze_trades.py
python3 scripts/analyze_trades.py --no-plot   # stats only
```

You can also import the helpers directly in your strategy analysis:

```python
from scripts.parse_data import load_prices, load_trades, get_mid_prices, compute_vwap

prices = load_prices("src/data/prices_round_0_day_-1.csv")
trades = load_trades("src/data/trades_round_0_day_-1.csv")
mid = get_mid_prices(prices, "TOMATOES")   # pd.Series indexed by timestamp
```

See `scripts/README.md` for full API reference.

---

## CSV Data Format

### Prices (`prices_round_X_day_Y.csv`)

Semicolon-delimited.

| Column | Description |
|--------|-------------|
| `day` | Day number (negative = tutorial) |
| `timestamp` | Timestep (0, 100, 200, … up to 999900) |
| `product` | e.g. `EMERALDS`, `TOMATOES` |
| `bid_price_1/2/3` | Best → 3rd best bid |
| `bid_volume_1/2/3` | Corresponding bid sizes |
| `ask_price_1/2/3` | Best → 3rd best ask |
| `ask_volume_1/2/3` | Corresponding ask sizes |
| `mid_price` | (bid1 + ask1) / 2 |
| `profit_and_loss` | Running PnL at this step |

### Trades (`trades_round_X_day_Y.csv`)

Semicolon-delimited.

| Column | Description |
|--------|-------------|
| `timestamp` | Step when the trade occurred |
| `buyer` | Buyer name (blank = anonymous market) |
| `seller` | Seller name (blank = anonymous market) |
| `symbol` | Product name |
| `currency` | e.g. `XIRECS` |
| `price` | Trade price |
| `quantity` | Trade size |

---

## Position Limits

If your orders would push your position past the limit, **all orders for that product are cancelled that step**.

| Product | Limit |
|---------|-------|
| EMERALDS | ±80 |
| TOMATOES | ±80 |

---

## Strategy Tips

- **EMERALDS** has a fixed fair value of exactly `10,000`. Pure market-making — post bids just below and asks just above, collect the spread.
- **TOMATOES** has a stochastic fair value (random walk, zero drift). Use mid-price or a rolling estimate as your fair value.
- Use `state.traderData` (a JSON string) to persist variables across timesteps (e.g. rolling averages, inventory targets).
- The `Logger` class in `algorithm1.py` formats print output so it appears correctly in the IMC and local visualizers.
- `state.own_trades[product]` shows what filled last step — useful for confirming executions.
- Position limits reset each new day in the P3 backtester unless you use `--merge-pnl`.

---

## Cheat Sheet

```bash
# ── Setup (one-time) ──────────────────────────────────────────────────────────

# P3 backtester
cd backtester && python3 -m venv .venv && source .venv/bin/activate && pip install -e . && deactivate && cd ..

# P4 Monte Carlo
cd visualisation/backtester && python3 -m venv .venv && source .venv/bin/activate && pip install -e . && pip install matplotlib && deactivate && cd ../..

# Visualizer
cd visualisation/visualizer && npm install && cd ../..

# Rust simulator (required for prosperity4mcbt)
cd visualisation/rust_simulator && cargo build --release && cd ../..

# ── Activate envs ─────────────────────────────────────────────────────────────

source backtester/.venv/bin/activate                    # P3 backtester
source visualisation/backtester/.venv/bin/activate      # P4 Monte Carlo + scripts

# ── Run backtests ─────────────────────────────────────────────────────────────

prosperity3bt src/algorithms/algorithm1.py 0             # P3 tutorial round
prosperity3bt src/algorithms/algorithm1.py 0 --vis       # + open browser visualizer

prosperity4mcbt src/algorithms/algorithm1.py --quick --vis   # P4 Monte Carlo

# ── Analysis scripts (activate visualisation venv first) ──────────────────────

python3 scripts/parse_data.py
python3 scripts/analyze_prices.py
python3 scripts/analyze_trades.py

# ── Visualizer dev server ─────────────────────────────────────────────────────

cd visualisation/visualizer && npm run dev    # → http://localhost:5173/
```
