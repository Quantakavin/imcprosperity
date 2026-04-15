"""
Parses a Prosperity backtester log file into tidy pandas DataFrames.

A backtester log has three sections:
    1. "Sandbox logs:"   — one JSON object per tick.
                           lambdaLog holds the compressed state (order book,
                           own_trades, market_trades) plus the orders WE sent.
    2. "Activities log:" — a CSV of book depth + P&L per (timestamp, product).
    3. "Trade History:"  — a JSON array of every trade executed.

We parse all three and expose:
    - prices_df:   one row per (timestamp, product) with L1-L3 book + pnl
    - trades_df:   one row per trade, with is_own flag (True if we're a side)
    - orders_df:   one row per order our trader sent
    - logs_df:     timestamp → our trader's stdout log text (for log viewer)
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Prosperity's logs contain trailing commas in the Trade History JSON
# (non-standard but the official tools accept it). Strip before parsing.
_TRAILING_COMMA_RE = re.compile(r",(\s*[\]}])")


@dataclass
class BacktestData:
    prices: pd.DataFrame
    trades: pd.DataFrame
    orders: pd.DataFrame
    logs: pd.DataFrame


def _split_sections(text: str) -> dict[str, str]:
    """Split the log file into its three named sections."""
    markers = ["Sandbox logs:", "Activities log:", "Trade History:"]
    sections: dict[str, str] = {}
    for i, marker in enumerate(markers):
        start = text.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = len(text)
        for later in markers[i + 1:]:
            pos = text.find(later, start)
            if pos >= 0 and pos < end:
                end = pos
        sections[marker[:-1]] = text[start:end].strip()
    return sections


def _parse_sandbox(raw: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (orders_df, logs_df). Each tick is a pretty-printed JSON object
    separated by blank lines, not one JSON array — so we stream-parse."""
    decoder = json.JSONDecoder()
    orders_rows = []
    log_rows = []
    i = 0
    n = len(raw)
    while i < n:
        while i < n and raw[i] in " \n\r\t":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            break
        i = end
        ts = obj.get("timestamp", 0)
        lam = obj.get("lambdaLog", "") or ""
        if not lam:
            continue
        # lambdaLog is itself a JSON array (our Logger output):
        #   [ compressed_state, compressed_orders, conversions, trader_data, logs ]
        try:
            inner = json.loads(lam)
        except json.JSONDecodeError:
            continue
        if not isinstance(inner, list) or len(inner) < 5:
            continue
        compressed_orders = inner[1]
        trader_text = inner[4] or ""
        for row in compressed_orders:
            # row = [symbol, price, qty]   (+qty = buy, -qty = sell)
            if len(row) >= 3:
                orders_rows.append({
                    "timestamp": ts,
                    "product": row[0],
                    "price": float(row[1]),
                    "quantity": int(row[2]),
                })
        if trader_text.strip():
            log_rows.append({"timestamp": ts, "text": trader_text})

    orders = pd.DataFrame(orders_rows, columns=["timestamp", "product", "price", "quantity"])
    logs = pd.DataFrame(log_rows, columns=["timestamp", "text"])
    return orders, logs


def _parse_activities(raw: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(raw), sep=";")
    return df


def _parse_trades(raw: str, our_orders: pd.DataFrame) -> pd.DataFrame:
    """Parse trade history + annotate which trades were ours.

    A trade is "ours" if at the same timestamp we sent an order for the same
    product that could have matched it (price on the correct side + qty).
    Prosperity logs don't explicitly mark own trades in the trade history,
    so we infer from our orders."""
    if not raw.strip():
        return pd.DataFrame(columns=["timestamp", "symbol", "price", "quantity",
                                     "buyer", "seller", "is_own"])
    data = json.loads(_TRAILING_COMMA_RE.sub(r"\1", raw))
    df = pd.DataFrame(data)
    if df.empty:
        df["is_own"] = False
        return df

    # Index our orders for quick lookup
    our_by_key: dict[tuple[int, str], list[tuple[float, int]]] = {}
    for _, row in our_orders.iterrows():
        key = (int(row.timestamp), row["product"])
        our_by_key.setdefault(key, []).append((row.price, row.quantity))

    def is_own(r):
        key = (int(r["timestamp"]), r["symbol"])
        orders = our_by_key.get(key, [])
        for price, qty in orders:
            # Our BUY (+qty) at price >= trade price → we could have bought it
            # Our SELL (-qty) at price <= trade price → we could have sold it
            if qty > 0 and price >= r["price"]:
                return True
            if qty < 0 and price <= r["price"]:
                return True
        return False

    df["is_own"] = df.apply(is_own, axis=1)
    return df


def _load_submission_json(obj: dict) -> BacktestData:
    """Format used by the official Prosperity submission platform:
        { submissionId, activitiesLog, logs: [...], tradeHistory: [...] }
    Trades have real buyer/seller identities — "SUBMISSION" marks our own."""
    prices = pd.read_csv(io.StringIO(obj.get("activitiesLog", "")), sep=";")

    # logs is a list of {sandboxLog, lambdaLog, timestamp}
    orders_rows = []
    log_rows = []
    for entry in obj.get("logs", []):
        ts = entry.get("timestamp", 0)
        lam = entry.get("lambdaLog", "") or ""
        if not lam:
            continue
        try:
            inner = json.loads(lam)
        except json.JSONDecodeError:
            continue
        if not isinstance(inner, list) or len(inner) < 5:
            continue
        for row in inner[1]:
            if len(row) >= 3:
                orders_rows.append({
                    "timestamp": ts, "product": row[0],
                    "price": float(row[1]), "quantity": int(row[2]),
                })
        trader_text = inner[4] or ""
        if trader_text.strip():
            log_rows.append({"timestamp": ts, "text": trader_text})
    orders = pd.DataFrame(orders_rows, columns=["timestamp", "product", "price", "quantity"])
    logs = pd.DataFrame(log_rows, columns=["timestamp", "text"])

    trades_list = obj.get("tradeHistory", [])
    trades = pd.DataFrame(trades_list) if trades_list else pd.DataFrame(
        columns=["timestamp", "symbol", "price", "quantity", "buyer", "seller"])
    if not trades.empty:
        trades["is_own"] = (trades["buyer"] == "SUBMISSION") | (trades["seller"] == "SUBMISSION")
    else:
        trades["is_own"] = False
    return BacktestData(prices=prices, trades=trades, orders=orders, logs=logs)


def load_log(path: str | Path) -> BacktestData:
    text = Path(path).read_text()

    # Auto-detect format: official submission logs are a single JSON object,
    # backtester logs are plain text with named sections.
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(_TRAILING_COMMA_RE.sub(r"\1", text))
            if isinstance(obj, dict) and "activitiesLog" in obj:
                return _load_submission_json(obj)
        except json.JSONDecodeError:
            pass

    sections = _split_sections(text)
    orders, logs = (pd.DataFrame(), pd.DataFrame())
    if "Sandbox logs" in sections:
        orders, logs = _parse_sandbox(sections["Sandbox logs"])
    prices = _parse_activities(sections["Activities log"]) if "Activities log" in sections else pd.DataFrame()
    trades = _parse_trades(sections.get("Trade History", ""), orders)
    return BacktestData(prices=prices, trades=trades, orders=orders, logs=logs)
