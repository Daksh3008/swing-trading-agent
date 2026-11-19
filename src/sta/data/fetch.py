"""Per-ticker yfinance fetch — replica of ``updated_stock_finder/src/fetch.py``.

Same contract as the reference implementation, with two differences:

1. the per-ticker cache is written as **parquet** instead of CSV;
2. when Yahoo's BSE (``.BO``) daily feed is broken for a symbol (it serves
   only a stub or monthly aggregates), the fetch falls back to the company's
   NSE listing (``.NS``) and records the substitution in
   ``data/universe/ns_fallbacks.csv``.

Downloads ``period="max"`` daily bars (OHLCV + Adj_Close, unadjusted) and is
resumable: existing files are returned from cache unless ``force=True``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# Project root: src/sta/data/fetch.py -> parents[3]
ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data" / "daily"
DATA_DIR.mkdir(exist_ok=True, parents=True)

COLUMNS = ["Open", "High", "Low", "Close", "Volume", "Adj_Close"]
MIN_ROWS = 30  # anything shorter than this for a multi-year period is a Yahoo stub

_fallback_lock = threading.Lock()


def history_path(daily_dir: Path, ticker: str) -> Path:
    """Cache path for one ticker (``:`` is path-hostile on Windows)."""
    return Path(daily_dir) / f"{ticker.replace(':', '_')}.parquet"


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a tz-aware DatetimeIndex to naive midnight dates.

    Uses tz_localize(None), NOT tz_convert(None): converting IST midnight to
    UTC would shift each bar to 18:30 of the previous calendar day.
    """
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()
    return df


def _read_parquet(path: Path) -> pd.DataFrame | None:
    """Load a cached history, mirroring the reference CSV read path."""
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 — corrupt cache is re-fetched
        log.warning("corrupt cache, re-fetching: %s", path)
        return None
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df[df["Date"].notna()].copy()
        df = df.set_index("Date").sort_index()
    _strip_tz(df)
    return df[COLUMNS]


def _fetch_raw(ticker: str, period: str, interval: str) -> pd.DataFrame:
    t = yf.Ticker(ticker)
    df = t.history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"No data for {ticker}")
    return _normalize(df)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten a yfinance frame to ``Date, Open..Adj_Close`` rows.

    Handles both the tz-aware intraday-style index returned by
    ``Ticker.history`` and the naive daily index returned by ``yf.download``.
    """
    df = df.reset_index()
    df = df.rename(columns={"Adj Close": "Adj_Close", "index": "Date",
                            "Datetime": "Date", "Price": "Date"})
    dates = pd.to_datetime(df["Date"])
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    df["Date"] = dates.dt.normalize()
    df = df.loc[:, ~df.columns.duplicated()]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    return df[["Date", *COLUMNS]]


def fetch_range(ticker: str, start, end, interval: str = "1d",
                retries: int = 2) -> pd.DataFrame:
    """Fetch ``[start, end)`` for one ticker (incremental sync path)."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            df = yf.Ticker(ticker).history(
                start=start, end=end, interval=interval, auto_adjust=False)
            if df is None or df.empty:
                raise ValueError(f"no rows in range for {ticker}")
            return _normalize(df)
        except Exception as exc:  # noqa: BLE001 — retry then surface
            last = exc
            if attempt < retries:
                time.sleep(0.4 * attempt)
    raise ValueError(f"range fetch failed for {ticker}: {last}")


def fetch_max(ticker: str, interval: str = "1d") -> pd.DataFrame:
    """Fetch the full history for one ticker (repair path)."""
    return _fetch_raw(ticker, "max", interval)



def _record_fallback(ticker: str, source: str) -> None:
    path = ROOT / "data" / "universe" / "ns_fallbacks.csv"
    with _fallback_lock:
        header = not path.exists()
        with open(path, "a", encoding="utf-8") as fh:
            if header:
                fh.write("ticker,source\n")
            fh.write(f"{ticker},{source}\n")


def download_ticker(ticker: str, period: str = "max", interval: str = "1d",
                    force: bool = False, sleep: float = 0.25,
                    retries: int = 3) -> pd.DataFrame:
    """Download & cache flattened parquet from yfinance.

    Returns a DataFrame indexed by naive ``Date`` with columns
    ``Open, High, Low, Close, Volume, Adj_Close``.
    """
    path = history_path(DATA_DIR, ticker)
    min_rows = MIN_ROWS if period in ("max", "10y", "5y") else 1
    if path.exists() and not force:
        cached = _read_parquet(path)
        if cached is not None and len(cached) >= min_rows:
            return cached
        elif cached is not None:
            log.warning("%s: stub cache (%d rows), re-fetching", ticker, len(cached))
    last_err: Exception | None = None
    best: pd.DataFrame | None = None
    for attempt in range(1, retries + 1):
        try:
            df = _fetch_raw(ticker, period, interval)
            if best is None or len(df) > len(best):
                best = df
            if len(best) >= min_rows or attempt == retries:
                break
            log.warning("%s: short response (%d rows), retrying", ticker, len(df))
            time.sleep(sleep * attempt * 2)
        except Exception as exc:  # noqa: BLE001 — retry then surface last error
            last_err = exc
            if attempt < retries:
                time.sleep(sleep * attempt * 2)

    # Yahoo's BSE daily feed is broken for some symbols (stub or monthly-only
    # aggregates). The company's NSE listing carries the same history.
    if (best is None or len(best) < min_rows) and ticker.endswith(".BO"):
        ns_ticker = ticker[:-3] + ".NS"
        try:
            ns_df = _fetch_raw(ns_ticker, period, interval)
            if best is None or len(ns_df) > len(best):
                if len(ns_df) >= min_rows:
                    log.info("%s: using NSE listing %s (%d rows)",
                             ticker, ns_ticker, len(ns_df))
                    _record_fallback(ticker, ns_ticker)
                    best = ns_df
        except Exception as exc:  # noqa: BLE001 — keep the BSE result
            last_err = last_err or exc

    if best is None:
        raise ValueError(f"No data for {ticker}: {last_err}")

    best.to_parquet(path, index=False)
    time.sleep(sleep)
    df = best.set_index("Date")
    return _strip_tz(df)
