# Prosperity Backtest Dashboard

Interactive Plotly Dash app for inspecting backtester output.

## Install

```bash
cd scripts/dashboard
pip install -r requirements.txt
```

## Run

Generate a log file from the backtester, then point the dashboard at it:

```bash
# produce a log
cd ../../backtester
.venv/bin/prosperity3bt ../src/algorithms/ash_coated_osmium.py 1 \
    --data custom_data --out /tmp/ash.log

# launch dashboard
cd ../scripts/dashboard
python app.py --log /tmp/ash.log
# open http://127.0.0.1:8050
```

Without `--log`, the app scans `backtester/backtests/`, cwd, and `/tmp` for
`*.log` files and lets you pick one from the dropdown.

## Features

| # | Feature |
|---|---------|
| 1 | Hoverable tooltips on every book level and trade marker |
| 2 | PnL subplot (from activities log) |
| 3 | Position subplot with ±80 limit guides |
| 4 | Log viewer that syncs to the hovered timestamp |
| 5 | Log file / product / overlay / normalize-by dropdowns |
| 6 | Trade group toggles (M/S/B/I/F) + qty range slider |
| 7 | Max-points slider for downsampling |

## Trade groups

Early rounds of Prosperity 4 ship anonymized trades (no buyer/seller IDs), so
classification is heuristic:

- **F** — our own trades (detected via our orders at the same timestamp)
- **B** — big taker (|qty| ≥ 15)
- **S** — small taker (|qty| ≤ 5)
- **M** — anything else (assumed maker)
- **I** — reserved for informed-trader IDs when they become available

Once IMC reveals trader IDs in later rounds, update `classify_trade` in
[app.py](app.py) to use them.

## Normalization

Pick `mid`, `weighted_mid`, or `wall_mid` from the "Normalize prices by"
dropdown — the plot then shows every price as a delta from that indicator.
Useful for mean-reversion visualisations (e.g. subtracting `wall_mid` from
book levels shows the true deviation around the market-maker's anchor).
