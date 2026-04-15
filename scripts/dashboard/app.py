"""
Prosperity backtest dashboard (Plotly Dash).

Run:
    python app.py --log /path/to/backtest.log
    # then open http://127.0.0.1:8050

Layout:
    ┌──────────────────────────────────────────┬────────────┐
    │  Main book + trade plot (price)          │  Sidebar:  │
    │     red asks, blue bids, trade markers   │  - log file│
    │                                          │  - product │
    ├──────────────────────────────────────────┤  - overlay │
    │  PnL subplot                             │  - norm by │
    ├──────────────────────────────────────────┤  - trade   │
    │  Position subplot                        │    filters │
    ├──────────────────────────────────────────┤  - downsmpl│
    │  Log viewer (hover-synced)               │            │
    └──────────────────────────────────────────┴────────────┘

Hovering over the main plot cross-filters the log viewer to the nearest
timestamp (numbered section #4 in the spec).
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html, no_update
from plotly.subplots import make_subplots

from loader import BacktestData, load_log


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIRS = [
    REPO_ROOT / "backtester" / "backtests",
    REPO_ROOT / "src" / "data",
]
SMALL_QTY_MAX = 5     # heuristic split between small/big takers
BIG_QTY_MIN = 15      # anything >= this qty is "big" taker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def list_log_files() -> list[str]:
    candidates: list[Path] = []
    for root in [*DEFAULT_LOG_DIRS, Path.cwd(), Path("/tmp")]:
        if root.exists():
            candidates.extend(sorted(root.rglob("*.log")))
    # dedupe preserving order
    seen = set()
    out = []
    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def classify_trade(row, position_at_ts: dict) -> str:
    """Rough taker/maker/ours classification.

    We don't have trader IDs in early rounds, so:
        F = our own trade (detected via our orders at same ts)
        B = "big" taker (large qty)
        S = "small" taker
        I = "informed" (reserved — no signal without IDs, treated as '')
        M = maker (default)
    """
    if row.get("is_own"):
        return "F"
    q = abs(row.get("quantity", 0))
    if q >= BIG_QTY_MIN:
        return "B"
    if q <= SMALL_QTY_MAX:
        return "S"
    return "M"


def compute_position(orders: pd.DataFrame, trades: pd.DataFrame, product: str) -> pd.DataFrame:
    """Our cumulative position over time, inferred from our executed trades.
    A trade counts as ours if is_own is True. Sign: if trade price <= our bid
    at that ts we BOUGHT, else if >= our ask we SOLD. We approximate by
    matching against our orders at that timestamp."""
    o = orders[orders["product"] == product]
    t = trades[(trades["symbol"] == product) & (trades["is_own"])]
    if t.empty:
        return pd.DataFrame({"timestamp": [0], "position": [0]})

    # Build a lookup of orders per timestamp for sign inference
    orders_by_ts: dict[int, list[tuple[float, int]]] = {}
    for _, r in o.iterrows():
        orders_by_ts.setdefault(int(r.timestamp), []).append((r.price, r.quantity))

    deltas = []
    for _, r in t.iterrows():
        ts = int(r["timestamp"])
        px = float(r["price"])
        qty = int(r["quantity"])
        sign = 0
        for op, oq in orders_by_ts.get(ts, []):
            if oq > 0 and op >= px:
                sign = 1
                break
            if oq < 0 and op <= px:
                sign = -1
                break
        deltas.append((ts, sign * qty))

    df = pd.DataFrame(deltas, columns=["timestamp", "delta"])
    df = df.groupby("timestamp", as_index=False)["delta"].sum().sort_values("timestamp")
    df["position"] = df["delta"].cumsum()
    return df[["timestamp", "position"]]


def compute_indicators(prices: pd.DataFrame, product: str) -> pd.DataFrame:
    """Common overlayable indicators derived from the book."""
    p = prices[prices["product"] == product].copy().sort_values("timestamp")
    if p.empty:
        return p
    # mid
    p["mid"] = p["mid_price"]
    # weighted mid (VWAP of L1 bid/ask)
    bv = p["bid_volume_1"].fillna(0)
    av = p["ask_volume_1"].fillna(0)
    total = bv + av
    p["weighted_mid"] = np.where(
        total > 0,
        (p["bid_price_1"] * av + p["ask_price_1"] * bv) / total.replace(0, np.nan),
        p["mid_price"],
    )
    # "WallMid": midpoint using the deepest visible level as a proxy for
    # the true market maker's quote (robust to thin L1 spikes).
    def deepest(row, side):
        prices_ = [row.get(f"{side}_price_{i}") for i in (1, 2, 3)]
        vols = [row.get(f"{side}_volume_{i}") for i in (1, 2, 3)]
        best = None
        best_v = -1
        for pr, v in zip(prices_, vols):
            if pd.notna(pr) and pd.notna(v) and v > best_v:
                best_v = v
                best = pr
        return best
    p["wall_bid"] = p.apply(lambda r: deepest(r, "bid"), axis=1)
    p["wall_ask"] = p.apply(lambda r: deepest(r, "ask"), axis=1)
    p["wall_mid"] = (p["wall_bid"] + p["wall_ask"]) / 2
    return p


# ---------------------------------------------------------------------------
# Figure builders
# ---------------------------------------------------------------------------
TRADE_GROUP_SYMBOLS = {
    "M": "square",
    "S": "triangle-up",
    "B": "triangle-up",
    "I": "diamond",
    "F": "x",
}
TRADE_GROUP_COLORS = {
    "M": "#888",
    "S": "#2ca02c",
    "B": "#d62728",
    "I": "#9467bd",
    "F": "#111",
}


def build_figure(
    data: BacktestData,
    product: str,
    show_levels: list[int],
    trade_groups: list[str],
    qty_range: tuple[int, int],
    overlay: list[str],
    normalize_by: str | None,
    max_points: int,
) -> go.Figure:
    prices = compute_indicators(data.prices, product)
    if prices.empty:
        fig = go.Figure()
        fig.update_layout(title=f"No data for {product}")
        return fig

    # Optional downsampling: stride to cap visible points
    if len(prices) > max_points:
        stride = max(1, len(prices) // max_points)
        prices = prices.iloc[::stride]

    # Normalization (subtract selected indicator from all prices)
    norm = None
    if normalize_by and normalize_by in prices.columns:
        norm_series = prices.set_index("timestamp")[normalize_by]
        norm = norm_series

    def adj(series, ts_series):
        if norm is None:
            return series
        aligned = norm.reindex(ts_series).values
        return series - aligned

    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.65, 0.175, 0.175],
        vertical_spacing=0.04,
        subplot_titles=("Book & trades", "PnL", "Position"),
    )

    ts = prices["timestamp"]

    # --- Book levels -------------------------------------------------------
    for lvl in show_levels:
        bp = prices[f"bid_price_{lvl}"]
        ap = prices[f"ask_price_{lvl}"]
        fig.add_trace(go.Scatter(
            x=ts, y=adj(bp, ts), mode="lines",
            line=dict(color="rgba(30,100,220,%.2f)" % (1.0 - 0.25 * (lvl - 1)), width=1),
            name=f"Bid L{lvl}", legendgroup="bids",
            hovertemplate=f"t=%{{x}}<br>bid{lvl}=%{{y}}<extra></extra>",
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=ts, y=adj(ap, ts), mode="lines",
            line=dict(color="rgba(220,40,40,%.2f)" % (1.0 - 0.25 * (lvl - 1)), width=1),
            name=f"Ask L{lvl}", legendgroup="asks",
            hovertemplate=f"t=%{{x}}<br>ask{lvl}=%{{y}}<extra></extra>",
        ), row=1, col=1)

    # --- Overlays ----------------------------------------------------------
    for col in overlay:
        if col in prices.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=adj(prices[col], ts),
                mode="lines",
                line=dict(color="#ff7f0e", width=1.5, dash="dot"),
                name=col,
            ), row=1, col=1)

    # --- Trades ------------------------------------------------------------
    tr = data.trades[data.trades["symbol"] == product].copy()
    if not tr.empty:
        tr["group"] = tr.apply(lambda r: classify_trade(r, {}), axis=1)
        tr = tr[tr["group"].isin(trade_groups)]
        tr = tr[(tr["quantity"].abs() >= qty_range[0]) & (tr["quantity"].abs() <= qty_range[1])]
        if norm is not None:
            # align each trade's price to its nearest known timestamp in norm
            nt = norm.sort_index()
            idx = nt.index.get_indexer(tr["timestamp"], method="nearest")
            adj_px = tr["price"].values - nt.values[idx]
        else:
            adj_px = tr["price"].values

        for grp, g in tr.groupby("group"):
            idx_g = g.index
            fig.add_trace(go.Scatter(
                x=g["timestamp"], y=adj_px[tr.index.get_indexer(idx_g)],
                mode="markers",
                marker=dict(
                    symbol=TRADE_GROUP_SYMBOLS.get(grp, "circle"),
                    color=TRADE_GROUP_COLORS.get(grp, "#555"),
                    size=9 if grp == "F" else 7,
                    line=dict(width=0.5, color="white"),
                ),
                name=f"{grp} trades",
                customdata=np.stack([
                    g["quantity"].values,
                    g.get("buyer", pd.Series([""] * len(g))).fillna("").values,
                    g.get("seller", pd.Series([""] * len(g))).fillna("").values,
                ], axis=1),
                hovertemplate=(
                    "t=%{x}<br>px=%{y}<br>qty=%{customdata[0]}"
                    "<br>buyer=%{customdata[1]}<br>seller=%{customdata[2]}"
                    f"<br>group={grp}<extra></extra>"
                ),
            ), row=1, col=1)

    # --- PnL ---------------------------------------------------------------
    fig.add_trace(go.Scatter(
        x=ts, y=prices["profit_and_loss"],
        mode="lines", line=dict(color="#2ca02c", width=1.3),
        name="PnL", showlegend=False,
        hovertemplate="t=%{x}<br>pnl=%{y}<extra></extra>",
    ), row=2, col=1)

    # --- Position ----------------------------------------------------------
    pos = compute_position(data.orders, data.trades, product)
    if not pos.empty:
        # Expand cumulative position onto the price timeline for nice step viz
        pos_full = pd.merge_asof(
            pd.DataFrame({"timestamp": ts.values}).sort_values("timestamp"),
            pos.sort_values("timestamp"),
            on="timestamp", direction="backward",
        ).fillna(0)
        fig.add_trace(go.Scatter(
            x=pos_full["timestamp"], y=pos_full["position"],
            mode="lines", line=dict(color="#1f77b4", width=1.3, shape="hv"),
            name="Position", showlegend=False,
            hovertemplate="t=%{x}<br>pos=%{y}<extra></extra>",
        ), row=3, col=1)

    # Position limit guides
    fig.add_hline(y=80, line=dict(color="#ccc", width=1, dash="dash"), row=3, col=1)
    fig.add_hline(y=-80, line=dict(color="#ccc", width=1, dash="dash"), row=3, col=1)

    fig.update_layout(
        height=780,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=20, t=40, b=30),
        plot_bgcolor="#fafafa",
    )
    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor")
    return fig


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def make_app(initial_log: str | None) -> Dash:
    app = Dash(__name__, title="Prosperity Dashboard")

    files = list_log_files()
    if initial_log and initial_log not in files:
        files = [initial_log] + files

    app.layout = html.Div([
        dcc.Store(id="data-store"),
        html.Div([
            html.Div([
                dcc.Graph(id="main-graph", config={"displaylogo": False}),
                html.Div([
                    html.H4("Logs (hover-synced)", style={"margin": "4px 0"}),
                    html.Pre(id="log-view", style={
                        "height": "180px", "overflow": "auto",
                        "background": "#111", "color": "#0f0",
                        "padding": "8px", "fontSize": "12px",
                        "margin": 0, "whiteSpace": "pre-wrap",
                    }),
                ]),
            ], style={"flex": "1 1 auto", "minWidth": 0}),

            html.Div([
                html.H3("Controls", style={"marginTop": 0}),

                html.Label("Log file"),
                dcc.Dropdown(
                    id="log-dropdown",
                    options=[{"label": Path(f).name, "value": f} for f in files],
                    value=initial_log or (files[0] if files else None),
                    clearable=False,
                ),

                html.Label("Product", style={"marginTop": 10}),
                dcc.Dropdown(id="product-dropdown", clearable=False),

                html.Label("Overlays", style={"marginTop": 10}),
                dcc.Checklist(
                    id="overlay-check",
                    options=[
                        {"label": "mid", "value": "mid"},
                        {"label": "weighted_mid", "value": "weighted_mid"},
                        {"label": "wall_mid", "value": "wall_mid"},
                    ],
                    value=[],
                    inline=True,
                ),

                html.Label("Normalize prices by", style={"marginTop": 10}),
                dcc.Dropdown(
                    id="normalize-dropdown",
                    options=[
                        {"label": "— none —", "value": ""},
                        {"label": "mid", "value": "mid"},
                        {"label": "weighted_mid", "value": "weighted_mid"},
                        {"label": "wall_mid", "value": "wall_mid"},
                    ],
                    value="",
                ),

                html.Label("Book levels", style={"marginTop": 10}),
                dcc.Checklist(
                    id="levels-check",
                    options=[{"label": f"L{i}", "value": i} for i in (1, 2, 3)],
                    value=[1, 2, 3], inline=True,
                ),

                html.Label("Trade groups", style={"marginTop": 10}),
                dcc.Checklist(
                    id="groups-check",
                    options=[
                        {"label": "M (maker)", "value": "M"},
                        {"label": "S (small taker)", "value": "S"},
                        {"label": "B (big taker)", "value": "B"},
                        {"label": "I (informed)", "value": "I"},
                        {"label": "F (our own)", "value": "F"},
                    ],
                    value=["M", "S", "B", "I", "F"],
                ),

                html.Label("Trade qty range", style={"marginTop": 10}),
                dcc.RangeSlider(
                    id="qty-range", min=1, max=50, step=1, value=[1, 50],
                    marks={1: "1", 10: "10", 25: "25", 50: "50"},
                ),

                html.Label("Max plotted points (downsample)", style={"marginTop": 10}),
                dcc.Slider(
                    id="max-points", min=1000, max=20000, step=1000, value=5000,
                    marks={1000: "1k", 5000: "5k", 10000: "10k", 20000: "20k"},
                ),

                html.Hr(),
                html.Div(id="stats-box", style={"fontSize": "13px"}),
            ], style={
                "flex": "0 0 280px",
                "marginLeft": "12px",
                "padding": "12px",
                "background": "#f4f4f4",
                "border": "1px solid #ddd",
                "borderRadius": "6px",
            }),
        ], style={"display": "flex", "flexDirection": "row", "padding": "12px"}),
    ])

    # ---- Load log -> store ----
    @app.callback(
        Output("data-store", "data"),
        Output("product-dropdown", "options"),
        Output("product-dropdown", "value"),
        Input("log-dropdown", "value"),
    )
    def _load(log_path):
        if not log_path:
            return {}, [], None
        bd = load_log(log_path)
        products = sorted(bd.prices["product"].unique().tolist()) if not bd.prices.empty else []
        payload = {
            "prices": bd.prices.to_json(orient="split"),
            "trades": bd.trades.to_json(orient="split"),
            "orders": bd.orders.to_json(orient="split"),
            "logs": bd.logs.to_json(orient="split"),
        }
        return payload, [{"label": p, "value": p} for p in products], (products[0] if products else None)

    # ---- Redraw ----
    @app.callback(
        Output("main-graph", "figure"),
        Output("stats-box", "children"),
        Input("data-store", "data"),
        Input("product-dropdown", "value"),
        Input("levels-check", "value"),
        Input("groups-check", "value"),
        Input("qty-range", "value"),
        Input("overlay-check", "value"),
        Input("normalize-dropdown", "value"),
        Input("max-points", "value"),
    )
    def _redraw(store, product, levels, groups, qty_range, overlay, normalize, max_points):
        if not store or not product:
            return go.Figure(), ""
        bd = BacktestData(
            prices=pd.read_json(io.StringIO(store["prices"]), orient="split"),
            trades=pd.read_json(io.StringIO(store["trades"]), orient="split"),
            orders=pd.read_json(io.StringIO(store["orders"]), orient="split"),
            logs=pd.read_json(io.StringIO(store["logs"]), orient="split"),
        )
        fig = build_figure(
            bd, product,
            show_levels=sorted(levels or []),
            trade_groups=groups or [],
            qty_range=tuple(qty_range or (1, 50)),
            overlay=overlay or [],
            normalize_by=normalize or None,
            max_points=max_points or 5000,
        )
        # Stats
        pnl_final = 0.0
        if not bd.prices.empty:
            pp = bd.prices[bd.prices["product"] == product]
            if not pp.empty:
                pnl_final = float(pp["profit_and_loss"].iloc[-1])
        n_own = int(bd.trades[(bd.trades["symbol"] == product) & bd.trades["is_own"]].shape[0]) if not bd.trades.empty else 0
        stats = html.Div([
            html.Div(f"Final PnL: {pnl_final:,.0f}"),
            html.Div(f"Our fills: {n_own}"),
        ])
        return fig, stats

    # ---- Log viewer sync ----
    @app.callback(
        Output("log-view", "children"),
        Input("main-graph", "hoverData"),
        State("data-store", "data"),
    )
    def _logs(hover, store):
        if not store or not hover:
            return ""
        try:
            ts = hover["points"][0]["x"]
        except (KeyError, IndexError, TypeError):
            return ""
        logs = pd.read_json(io.StringIO(store["logs"]), orient="split")
        trades = pd.read_json(io.StringIO(store["trades"]), orient="split")
        if logs.empty and trades.empty:
            return f"(no logs at t={ts})"
        parts = [f"t = {ts}"]
        # Nearest trader log line
        if not logs.empty:
            idx = (logs["timestamp"] - ts).abs().idxmin()
            row = logs.loc[idx]
            parts.append(f"-- trader log @ {int(row.timestamp)} --")
            parts.append(str(row.text))
        # Trades within +/-100 ticks
        if not trades.empty:
            window = trades[(trades["timestamp"] >= ts - 100) & (trades["timestamp"] <= ts + 100)]
            if not window.empty:
                parts.append(f"-- trades in [{ts-100}, {ts+100}] --")
                for _, r in window.head(20).iterrows():
                    who = f"{r.get('buyer','') or '?'} <- {r.get('seller','') or '?'}"
                    parts.append(f"  {int(r.timestamp):>6}  {r.symbol:<24} "
                                 f"px={r.price:<8} qty={r.quantity:<4}  {who}  own={r.is_own}")
        return "\n".join(parts)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", help="Path to backtester log file")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    initial = args.log or os.environ.get("PROSPERITY_LOG")
    app = make_app(initial)
    app.run(debug=args.debug, port=args.port)


if __name__ == "__main__":
    main()
