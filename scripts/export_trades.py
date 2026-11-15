"""Export per-year trade CSVs and a yearly profitability summary.

Usage:
    python scripts/export_trades.py <backtest_dir> <out_dir>

Attributes each trade to the calendar year of its EXIT (when P&L is realised).
Per-trade capital is fixed at Rs 15,000 (no compounding).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

CAPITAL_PER_TRADE = 15_000.0

COLUMNS = [
    "symbol", "exchange", "entry_date", "exit_date", "entry_price", "exit_price",
    "shares", "pnl", "return_pct", "exit_reason", "days_held",
    "stop_at_exit", "high_water", "atr_at_exit",
    "entry_cost", "exit_cost",
]


def export(src_dir: Path, out_dir: Path) -> pd.DataFrame:
    trades = pd.read_parquet(src_dir / "trades.parquet")
    trades["entry_date"] = pd.to_datetime(trades["entry_date"])
    trades["exit_date"] = pd.to_datetime(trades["exit_date"])
    trades["exit_reason"] = trades["exit_reason"].astype(str).str.replace(
        "ExitReason.", "", regex=False)
    trades["year"] = trades["exit_date"].dt.year

    # closed trades only (open at data end have no exit_date)
    closed = trades[trades["exit_date"].notna()].copy()

    out_dir.mkdir(parents=True, exist_ok=True)
    by_year = out_dir / "trades_by_year"
    by_year.mkdir(exist_ok=True)

    cols = [c for c in COLUMNS if c in closed.columns]
    closed.sort_values(["year", "entry_date"])[cols].to_csv(
        out_dir / "trades_all.csv", index=False)

    for year, group in closed.groupby("year"):
        group.sort_values("entry_date")[cols].to_csv(
            by_year / f"trades_{int(year)}.csv", index=False)

    rows = []
    for year, g in closed.groupby("year"):
        wins = g[g["pnl"] > 0]
        losses = g[g["pnl"] <= 0]
        gross_profit = float(wins["pnl"].sum())
        gross_loss = float(abs(losses["pnl"].sum()))
        capital = len(g) * CAPITAL_PER_TRADE
        rows.append({
            "year": int(year),
            "trades": len(g),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(g) * 100, 1),
            "gross_profit_inr": round(gross_profit),
            "gross_loss_inr": round(gross_loss),
            "net_pnl_inr": round(float(g["pnl"].sum())),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
            "avg_pnl_per_trade_inr": round(float(g["pnl"].mean())),
            "avg_return_pct": round(float(g["return_pct"].mean()) * 100, 2),
            "avg_win_pct": round(float(wins["return_pct"].mean()) * 100, 2) if len(wins) else 0.0,
            "avg_loss_pct": round(float(losses["return_pct"].mean()) * 100, 2) if len(losses) else 0.0,
            "avg_hold_days": round(float(g["days_held"].mean()), 1),
            "capital_traded_inr": round(capital),
            "net_pnl_pct_of_capital": round(float(g["pnl"].sum()) / capital * 100, 2),
        })
    summary = pd.DataFrame(rows).sort_values("year")
    summary.to_csv(out_dir / "yearly_summary.csv", index=False)
    print(f"wrote {out_dir}/yearly_summary.csv, trades_all.csv, "
          f"trades_by_year/ ({closed['year'].nunique()} years)")
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python scripts/export_trades.py <backtest_dir> <out_dir>")
    export(Path(sys.argv[1]), Path(sys.argv[2]))
