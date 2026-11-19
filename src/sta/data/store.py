"""Read-side view over per-symbol parquet history written by ``fetch.py``.

Adapts the parquet schema (capitalised OHLCV + Adj_Close) to the lowercase
bar schema the scanner and backtest engine expect, deriving the fields the
old bhavcopy store provided:

    open, high, low, close, volume, adj_close,
    value       = close * volume (traded turnover),
    prev_close  = close.shift(1),
    exchange    = "BSE",           isin = "" (not available from yfinance),
    name        = symbol.
"""

from __future__ import annotations

import logging
from datetime import date as Date
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_RAW = ["Open", "High", "Low", "Close", "Volume", "Adj_Close"]


class ParquetBarStore:
    """Loads per-symbol histories from ``<data_dir>/daily/*.parquet``."""

    def __init__(self, data_dir: Path):
        self.symbols_dir = Path(data_dir) / "daily"

    # ------------------------------------------------------------ symbols

    def list_symbols(self) -> list[str]:
        if not self.symbols_dir.exists():
            return []
        return sorted(p.stem for p in self.symbols_dir.glob("*.parquet"))

    # ------------------------------------------------------------- symbol

    def load_symbol(self, symbol: str, tail: int | None = None) -> pd.DataFrame | None:
        path = self.symbols_dir / f"{symbol.replace(':', '_')}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 — corrupt file behaves like missing
            log.warning("corrupt parquet: %s", path)
            return None
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
            df = df[df["Date"].notna()].set_index("Date").sort_index()
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        # a bar without a close is not a session (Yahoo's pending BSE rows)
        df = df[df["Close"].notna()]
        if tail:
            df = df.tail(tail)

        out = pd.DataFrame(index=df.index)
        out["open"] = df["Open"].astype(float)
        out["high"] = df["High"].astype(float)
        out["low"] = df["Low"].astype(float)
        out["close"] = df["Close"].astype(float)
        out["volume"] = df["Volume"].fillna(0).astype("int64")
        out["adj_close"] = df["Adj_Close"].astype(float)
        out["value"] = out["close"] * out["volume"]
        out["prev_close"] = out["close"].shift(1)
        out["exchange"] = "BSE"
        out["isin"] = ""
        out["name"] = symbol
        return out

    # ---------------------------------------------------------------- day

    def _frames(self) -> dict[str, pd.DataFrame]:
        return {sym: df for sym in self.list_symbols()
                if (df := self.load_symbol(sym)) is not None}

    def available_days(self) -> list[Date]:
        """Union of trading dates across every cached symbol."""
        days: set[Date] = set()
        for df in self._frames().values():
            days.update(df.index.date)
        return sorted(days)

    def load_day(self, day: Date) -> pd.DataFrame | None:
        rows = []
        for sym, df in self._frames().items():
            ts = pd.Timestamp(day)
            if ts in df.index:
                rows.append(df.loc[ts].to_dict() | {"symbol": sym})
        return pd.DataFrame(rows) if rows else None
