"""Repair a one-session hole in a BSE series from the company's NSE listing.

Yahoo's ``.BO`` feed occasionally skips a session for most of the universe while
the ``.NS`` listing has it. A missing session freezes the tracker and blocks the
scan, so we fill just that date - never rewrite stored bars - and only when the
two listings demonstrably track each other.

Guards, in order:

1. the symbol is a ``.BO`` ticker whose stored history ends before the gap;
2. the ``.NS`` listing has a bar for the gap date;
3. at least ``MIN_OVERLAP`` closes overlap, the median close ratio stays inside
   ``MAX_RATIO``, and the median daily-return difference stays inside
   ``MAX_RETURN_DIFF``;
4. the filled close does not imply a one-day move beyond ``SUSPICIOUS_MOVE``
   relative to the last stored close.

Batches of ``BATCH_SIZE`` tickers go through ``yf.download``; the fetcher is
injectable so the tests never touch the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta

import numpy as np
import pandas as pd

from ..core.config import Settings
from ..core.market_calendar import sessions_between
from .fetch import COLUMNS
from .sync import (
    SUSPICIOUS_MOVE,
    read_history,
    write_history_atomic,
)

log = logging.getLogger(__name__)

BATCH_SIZE = 50
LOOKBACK_DAYS = 40
MIN_OVERLAP = 3
MAX_RATIO = 0.03
MAX_RETURN_DIFF = 0.02

BatchFetcher = Callable[[list[str], Date, Date], dict[str, pd.DataFrame]]


@dataclass
class GapFillResult:
    day: Date | None = None
    candidates: int = 0
    filled: int = 0
    already: int = 0
    skipped_mismatch: int = 0
    skipped_quality: int = 0
    skipped_missing: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat() if self.day else None,
            "candidates": self.candidates,
            "filled": self.filled,
            "already": self.already,
            "skipped_mismatch": self.skipped_mismatch,
            "skipped_quality": self.skipped_quality,
            "skipped_missing": self.skipped_missing,
            "errors": self.errors[:20],
        }


def fill_session_gap(settings: Settings, db, day: Date,
                     symbols: list[str] | None = None, *,
                     batch_fetch: BatchFetcher | None = None) -> GapFillResult:
    """Fill ``day`` for every eligible ``.BO`` symbol. Idempotent."""
    result = GapFillResult(day=day)
    if db is None:
        return result

    universe = symbols if symbols is not None else _membership(settings)
    state = {row["symbol"]: row for row in db.sync_states()}
    candidates: list[str] = []
    for sym in universe:
        if not sym.endswith(".BO"):
            continue
        row = state.get(sym)
        if row is None or not row["last_date"]:
            continue
        if str(row["last_date"])[:10] >= day.isoformat():
            continue
        try:
            last = Date.fromisoformat(str(row["last_date"])[:10])
        except ValueError:
            continue
        # Only a single-session hole is repaired. A longer gap is the normal
        # sync's job; filling the middle of it would hide the missing bars.
        if sessions_between(last, day) != [day]:
            continue
        candidates.append(sym)
    result.candidates = len(candidates)
    if not candidates:
        return result

    fetcher = batch_fetch or _yf_batch
    ns_tickers = [sym[:-3] + ".NS" for sym in candidates]
    try:
        fetched = fetcher(ns_tickers, day - timedelta(days=LOOKBACK_DAYS),
                          day + timedelta(days=1))
    except Exception as exc:  # noqa: BLE001 - a gap fill must never break a sync
        log.warning("cross-listing fetch failed: %s", exc)
        result.errors.append(repr(exc))
        return result

    for symbol, ns in zip(candidates, ns_tickers, strict=True):
        frame = fetched.get(ns)
        if frame is None or frame.empty:
            result.skipped_missing += 1
            continue
        try:
            filled = _fill_one(settings, db, symbol, frame, day)
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the pass
            result.errors.append(f"{symbol}: {exc}")
            log.debug("gap fill failed for %s: %s", symbol, exc)
            continue
        if filled == "ok":
            result.filled += 1
            db.upsert_days([day])
        elif filled == "already":
            result.already += 1
        elif filled == "mismatch":
            result.skipped_mismatch += 1
        elif filled == "quality":
            result.skipped_quality += 1
        else:
            result.skipped_missing += 1

    if result.filled:
        log.info("gap fill %s: %d of %d symbols repaired from the NSE listing",
                 day, result.filled, result.candidates)
    return result


# ------------------------------------------------------------------ internals


def _fill_one(settings: Settings, db, symbol: str, frame: pd.DataFrame,
              day: Date) -> str:
    path = settings.daily_dir / f"{symbol}.parquet"
    old = read_history(path)
    if old is None or old.empty:
        return "missing"

    ns = _indexed_ns(frame)
    if ns.empty or pd.Timestamp(day) not in ns.index:
        return "missing"
    if pd.Timestamp(day) in old.index:
        db.set_sync_state(symbol, day, bars=len(old), status="ok",
                          error=f"session {day} already present")
        return "already"              # nothing to do, already healed

    if not _tracks(old, ns):
        return "mismatch"

    last_close = float(old["Close"].iloc[-1])
    bar = ns.loc[pd.Timestamp(day)]
    new_close = float(bar["Close"])
    if not np.isfinite(new_close) or new_close <= 0:
        return "missing"
    if abs(new_close / last_close - 1.0) > SUSPICIOUS_MOVE:
        return "quality"

    patch = ns.loc[[pd.Timestamp(day)]]
    merged = pd.concat([old, patch])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    write_history_atomic(merged, path)
    current = db.sync_state(symbol)
    status = "suspicious" if current and current["status"] == "suspicious" else "ok"
    db.set_sync_state(symbol, day, bars=len(merged), status=status,
                      error=f"session {day} filled from {symbol[:-3]}.NS")
    return "ok"


def _tracks(old: pd.DataFrame, ns: pd.DataFrame) -> bool:
    """True when the two listings look like the same instrument."""
    common = old.index.intersection(ns.index)
    if len(common) < MIN_OVERLAP:
        return False
    bo_close = old.loc[common, "Close"].astype(float)
    ns_close = ns.loc[common, "Close"].astype(float)
    valid = (bo_close > 0) & (ns_close > 0)
    if valid.sum() < MIN_OVERLAP:
        return False
    ratio = float((ns_close[valid] / bo_close[valid]).median())
    if not (1.0 - MAX_RATIO <= ratio <= 1.0 + MAX_RATIO):
        return False
    bo_ret = bo_close[valid].pct_change()
    ns_ret = ns_close[valid].pct_change()
    diff = (ns_ret - bo_ret).abs().dropna()
    if diff.empty:
        return False
    return float(diff.median()) <= MAX_RETURN_DIFF


def _indexed_ns(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize a ``yf.download`` sub-frame to the parquet bar schema."""
    df = frame.copy()
    if "Date" not in df.columns:
        df = df.reset_index()
        if "Date" not in df.columns:
            df = df.rename(columns={df.columns[0]: "Date"})
    df = df.rename(columns={"Adj Close": "Adj_Close"})
    dates = pd.to_datetime(df["Date"])
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_localize(None)
    df["Date"] = dates.dt.normalize()
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    out = df.set_index("Date")[COLUMNS].astype(float).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _membership(settings: Settings) -> list[str]:
    path = settings.universe_dir / "membership.parquet"
    if not path.exists():
        return sorted(p.stem for p in settings.daily_dir.glob("*.parquet"))
    frame = pd.read_parquet(path)
    return sorted(set(frame["symbol"].astype(str)))


def _yf_batch(tickers: list[str], start: Date, end: Date) -> dict[str, pd.DataFrame]:
    """Batched ``yf.download``; unresolvable tickers are simply absent."""
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.CRITICAL)      # 4,000 "possibly delisted" lines help nobody
    try:
        for offset in range(0, len(tickers), BATCH_SIZE):
            group = tickers[offset:offset + BATCH_SIZE]
            try:
                raw = yf.download(group, start=str(start), end=str(end),
                                  interval="1d", auto_adjust=False, progress=False,
                                  group_by="ticker", threads=True)
            except Exception as exc:  # noqa: BLE001 - try the next chunk
                log.debug("batch failed for %d tickers: %s", len(group), exc)
                continue
            if raw is None or raw.empty:
                continue
            for ticker in group:
                try:
                    sub = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) else raw
                    sub = sub.dropna(how="all")
                except KeyError:
                    continue
                if not sub.empty:
                    out[ticker] = sub
    finally:
        yf_log.setLevel(previous)
    return out
