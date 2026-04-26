"""Plot Round 3 price evolution — VELVETFRUIT_EXTRACT (underlying), VEV_xxxx options, HYDROGEL_PACK.

One figure per day: underlying + option strike fan + HYDROGEL_PACK.
Plus an option-vs-strike snapshot so the call-curve shape is visible.
"""

from __future__ import annotations

import re
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "src" / "data"
OUT_DIR = Path(__file__).resolve().parent
DAYS = [0, 1, 2]
UNDERLYING = "VELVETFRUIT_EXTRACT"
OTHER = "HYDROGEL_PACK"
STRIKE_RE = re.compile(r"^VEV_(\d+)$")


def load_day(day: int) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"prices_round_3_day_{day}.csv", sep=";")
    df = df[df["mid_price"] > 0].copy()
    return df


def strike_of(product: str) -> int | None:
    m = STRIKE_RE.match(product)
    return int(m.group(1)) if m else None


def plot_day(day: int) -> None:
    df = load_day(day)
    products = sorted(df["product"].unique())
    options = sorted([p for p in products if STRIKE_RE.match(p)],
                     key=lambda p: strike_of(p))

    fig, (ax_u, ax_o, ax_h) = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

    # --- underlying ---
    sub = df[df["product"] == UNDERLYING].sort_values("timestamp")
    ax_u.plot(sub["timestamp"], sub["mid_price"], color="black", linewidth=0.7)
    ax_u.fill_between(sub["timestamp"], sub["bid_price_1"], sub["ask_price_1"],
                      alpha=0.2, color="black")
    ax_u.set_ylabel("Mid")
    ax_u.set_title(f"Day {day} — {UNDERLYING} (underlying)")
    ax_u.grid(True, alpha=0.3)

    # --- options fan ---
    cmap = plt.cm.viridis(np.linspace(0, 1, len(options)))
    for color, opt in zip(cmap, options):
        sub = df[df["product"] == opt].sort_values("timestamp")
        ax_o.plot(sub["timestamp"], sub["mid_price"],
                  linewidth=0.6, color=color, label=f"K={strike_of(opt)}")
    ax_o.set_ylabel("Option mid")
    ax_o.set_title(f"Day {day} — VEV call options across strikes")
    ax_o.set_yscale("symlog", linthresh=1)
    ax_o.legend(ncol=5, fontsize=8, loc="upper right")
    ax_o.grid(True, alpha=0.3)

    # --- hydrogel ---
    sub = df[df["product"] == OTHER].sort_values("timestamp")
    ax_h.plot(sub["timestamp"], sub["mid_price"], color="teal", linewidth=0.7)
    ax_h.set_ylabel("Mid")
    ax_h.set_title(f"Day {day} — {OTHER}")
    ax_h.set_xlabel("Timestamp")
    ax_h.grid(True, alpha=0.3)

    fig.tight_layout()
    out = OUT_DIR / f"round3_day{day}_prices.png"
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"saved {out.name}")


def plot_smile(day: int) -> None:
    """Average option mid-price by strike vs. average underlying — sanity-check call curve."""
    df = load_day(day)
    options = sorted([p for p in df["product"].unique() if STRIKE_RE.match(p)],
                     key=lambda p: strike_of(p))
    avg_under = df.loc[df["product"] == UNDERLYING, "mid_price"].mean()
    strikes = [strike_of(p) for p in options]
    avg_opt = [df.loc[df["product"] == p, "mid_price"].mean() for p in options]
    intrinsic = [max(0, avg_under - k) for k in strikes]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(strikes, avg_opt, "o-", label="Avg option mid")
    ax.plot(strikes, intrinsic, "x--", label=f"Intrinsic: max(0, {avg_under:.0f} − K)")
    ax.axvline(avg_under, color="red", linestyle=":", alpha=0.6,
               label=f"Avg underlying = {avg_under:.1f}")
    ax.set_xlabel("Strike")
    ax.set_ylabel("Avg mid price")
    ax.set_title(f"Day {day} — Call curve: option mid vs strike")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out = OUT_DIR / f"round3_day{day}_smile.png"
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"saved {out.name}")


def summary() -> None:
    rows = []
    for day in DAYS:
        df = load_day(day)
        for product in sorted(df["product"].unique()):
            sub = df[df["product"] == product]
            rows.append({
                "day": day,
                "product": product,
                "strike": strike_of(product),
                "avg_mid": sub["mid_price"].mean(),
                "min_mid": sub["mid_price"].min(),
                "max_mid": sub["mid_price"].max(),
                "std_mid": sub["mid_price"].std(),
                "n_quotes": len(sub),
            })
    out = pd.DataFrame(rows)
    out.to_csv(OUT_DIR / "round3_summary.csv", index=False)
    print(out.to_string(index=False))


if __name__ == "__main__":
    for day in DAYS:
        plot_day(day)
        plot_smile(day)
    summary()
