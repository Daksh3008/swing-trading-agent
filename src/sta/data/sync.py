"""Incremental history sync against the per-symbol parquet cache.

Design (D1):

- each symbol's last stored date drives the fetch window: the last
  ``OVERLAP_BARS`` stored bars are re-fetched and compared, and the missing
  tail is appended;
- if any overlapping close moved by more than ``MISMATCH_PCT`` the symbol is
  fully refreshed (``period="max"``) — Yahoo rewrites history after splits and
  bonuses;
- symbols are fetched in batches (~50) through ``yf.download`` with a
  per-ticker fallback, so a 4,000-symbol daily refresh is minutes, not hours;
- a calendar guard derives the expected last trading day from the batch itself
  and retries laggards once before recording them in ``sync_failures.csv``;
- a data-quality gate flags bars with a >35% one-day move that the split
  factor (``Adj_Close / Close``) does not explain; flagged symbols never emit
  signals;
- writes are atomic (temp file + replace) and the whole run is guarded by
  ``data/.sync.lock`` so the scheduler and the app can never overlap.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..core.config import Settings
from ..core.market_calendar import (
    IST,
    latest_closed_session,
    session_cutoff,
    sessions_behind,
)
from .fetch import COLUMNS, _strip_tz, fetch_max, fetch_range, history_path

log = logging.getLogger(__name__)

OVERLAP_BARS = 7
MISMATCH_PCT = 0.005       # 0.5% move in an overlapping close -> full refresh
SUSPICIOUS_MOVE = 0.35     # |1-day move| that must be explained by a split
FACTOR_TOL = 0.01          # split-factor change that counts as "explained"
STALE_BDAYS = 3            # trading days behind the expected close
DEAD_BDAYS = 10            # beyond this, stop retrying (delisted/never traded)
PUBLISH_HOUR = 18          # a sync is only a real attempt after the publish window
PUBLISH_MINUTE = 30


class SyncLocked(Exception):
    """Another sync (or the app) already holds the lock."""


# ------------------------------------------------------------------- helpers


@contextmanager
def sync_lock(data_dir: Path, stale_after_s: int = 6 * 3600) -> Iterator[Path]:
    """Exclusive cross-process lock via an O_EXCL file create.

    A lock is stale when its owner process is gone (Windows PID check) or when
    it is older than ``stale_after_s`` (other platforms / recycled PIDs).
    """
    path = Path(data_dir) / ".sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        owner = lock_owner_pid(path)
        if owner is not None and not pid_alive(owner):
            log.warning("removing sync lock left by dead process %s", owner)
            path.unlink(missing_ok=True)
        elif age > stale_after_s:
            log.warning("removing stale sync lock (%.1f h old)", age / 3600)
            path.unlink(missing_ok=True)
        else:
            raise SyncLocked(f"{path} held for {age / 60:.1f} min")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}".encode())
        yield path
    finally:
        os.close(fd)
        path.unlink(missing_ok=True)


def lock_owner_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check without signalling the process."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_history(path: Path) -> pd.DataFrame | None:
    """Load one symbol's cached parquet, indexed by naive date."""
    if not Path(path).exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — corrupt cache behaves as missing
        log.warning("corrupt cache %s: %s", path, exc)
        return None
    return _indexed(df)


def _indexed(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a fetched frame to a naive midnight DatetimeIndex."""
    if "Date" in df.columns:
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
    df = _strip_tz(df)
    keep = [c for c in COLUMNS if c in df.columns]
    return df[keep].astype(float).sort_index()


def write_history_atomic(df: pd.DataFrame, path: Path) -> None:
    """Temp-file + replace, so a killed sync cannot corrupt a history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    out = df.reset_index().rename(columns={df.index.name or "index": "Date"})
    if "Date" not in out.columns:
        out = out.rename(columns={out.columns[0]: "Date"})
    out.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def last_stored_date(path: Path) -> Date | None:
    """Newest bar date in one cached file (reads the date column only)."""
    import pyarrow.parquet as pq

    path = Path(path)
    if not path.exists():
        return None
    try:
        table = pq.read_table(path, columns=["Date"])
    except Exception as exc:  # noqa: BLE001 — unreadable behaves as missing
        log.warning("unreadable cache %s: %s", path, exc)
        return None
    if table.num_rows == 0:
        return None
    dates = pd.to_datetime(table.column("Date").to_pandas(), errors="coerce")
    dates = dates.dropna()
    if dates.empty:
        return None
    return dates.max().date()


def merge_history(old: pd.DataFrame, new: pd.DataFrame
                  ) -> tuple[pd.DataFrame, bool]:
    """Append ``new`` over ``old``; flag retro-adjustment of the overlap."""
    if new.empty:
        return old, False
    overlap = old.tail(OVERLAP_BARS)
    common = overlap.index.intersection(new.index)
    mismatch = False
    if len(common):
        old_close = overlap.loc[common, "Close"].astype(float)
        new_close = new.loc[common, "Close"].astype(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = (new_close / old_close - 1.0).abs()
        mismatch = bool((ratio > MISMATCH_PCT).fillna(False).any())
    head = old[old.index < new.index.min()]
    merged = pd.concat([head, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged, mismatch


def quality_flags(df: pd.DataFrame) -> pd.Series:
    """Bars whose move the split factor cannot explain (bad Yahoo print)."""
    close = df["Close"].astype(float)
    adj = df["Adj_Close"].astype(float)
    factor = adj / close.replace(0.0, np.nan)
    ret = close.pct_change().abs()
    factor_ret = factor.pct_change().abs().fillna(0.0)
    return (ret > SUSPICIOUS_MOVE) & (factor_ret < FACTOR_TOL)


def _latest_business_day(day: Date, now: datetime | None = None) -> Date:
    """Most recent session whose close is published (holidays aware)."""
    now = now or datetime.now()
    return latest_closed_session(datetime.combine(day, now.time()))


def expected_session(now: datetime | None = None) -> Date:
    """The newest *closed* trading session — never today's live bar."""
    return latest_closed_session(now or datetime.now(IST))


def expected_last_day(last_dates: Iterable[Date | None],
                      min_share: float = 0.5) -> Date | None:
    """Most plausible exchange close: the newest date at least half the symbols
    reached. Half is the scan floor, so a day only a handful of symbols printed
    is never treated as the session."""
    dates = [d for d in last_dates if d is not None]
    if not dates:
        return None
    counts = Counter(dates)
    floor = min(len(dates), max(5, int(min_share * len(dates))))
    viable = [d for d, n in counts.items() if n >= floor]
    return max(viable) if viable else None


def business_days_behind(last: Date | None, expected: Date | None) -> int:
    """Sessions in ``(last, expected]``, weekends and holidays excluded."""
    return sessions_behind(last, expected)


def attempted_since(db, expected: Date,
                    at: tuple[int, int] = (PUBLISH_HOUR, PUBLISH_MINUTE)) -> bool:
    """True when a real sync already ran after ``expected`` closed and published.

    ``at`` is the publish window for that session (18:30 IST by default). A
    startup attempt at 16:05 must not stop the scheduled 18:30 run, and a
    tiny ``--limit`` run must not count as a full attempt.
    """
    if db is None:
        return False
    last = db.last_finished_sync_log()
    if last is None or last["status"] not in ("ok", "partial"):
        return False
    finished = str(last["finished_at"] or "")
    cutoff = session_cutoff(expected).replace(hour=at[0], minute=at[1])
    if finished < cutoff.isoformat(timespec="seconds"):
        return False
    symbols = int(last["symbols"] or 0)
    failed = int(last["failed"] or 0)
    if symbols < 50:                   # a --limit smoke run proves nothing
        return False
    return failed < 0.5 * symbols


# -------------------------------------------------------------------- types


@dataclass
class SyncResult:
    symbols: int = 0
    updated: int = 0
    refreshed: int = 0
    unchanged: int = 0
    failed: int = 0
    suspicious: int = 0
    stale: int = 0
    expected_day: Date | None = None
    gap_filled: int = 0
    gap_day: Date | None = None
    duration_s: float = 0.0
    status: str = "ok"
    note: str | None = None
    errors: list[str] = field(default_factory=list)
    suspicious_dates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "symbols": self.symbols, "updated": self.updated,
            "refreshed": self.refreshed, "unchanged": self.unchanged,
            "failed": self.failed, "suspicious": self.suspicious,
            "stale": self.stale,
            "expected_day": self.expected_day.isoformat() if self.expected_day else None,
            "gap_filled": self.gap_filled,
            "gap_day": self.gap_day.isoformat() if self.gap_day else None,
            "duration_s": round(self.duration_s, 1), "status": self.status,
            "note": self.note,
            "errors": self.errors[:20], "suspicious_dates": self.suspicious_dates[:20],
        }


# --------------------------------------------------------------------- sync


class DataSync:
    """Incremental refresh of ``data/daily/*.parquet``.

    ``db`` is optional: without it the sync still updates the parquet cache and
    returns a result, which keeps the CLI usable before the app is initialised.
    """

    def __init__(self, settings: Settings, db=None, *,
                 workers: int = 8, sleep: float = 0.05,
                 progress: Callable[[str, int, int], None] | None = None,
                 single_fetch: Callable[[str, Date, Date], pd.DataFrame] | None = None,
                 max_fetch: Callable[[str], pd.DataFrame] | None = None):
        self.settings = settings
        self.db = db
        self.workers = workers
        self.sleep = sleep
        self._progress_cb = progress
        self.daily_dir = settings.data_dir / "daily"
        self.failures_path = settings.universe_dir / "sync_failures.csv"
        self.quality_path = settings.universe_dir / "data_quality.csv"
        self._single_fetch = single_fetch or fetch_range
        self._max_fetch = max_fetch or fetch_max
        self._sources: dict[str, str] = {}
        self._coverage_allowed = True
        if single_fetch is None:           # real runs honour ns_fallbacks.csv
            self._sources = self._fallback_sources()

    # ---------------------------------------------------------------- public

    def run(self, full: bool = False, limit: int | None = None,
            catch_up: bool = False) -> SyncResult:
        started = time.time()
        symbols = self.symbols()[:limit] if limit else self.symbols()
        result = SyncResult(symbols=len(symbols))
        self._coverage_allowed = limit is None

        if catch_up and not full and self._already_current():
            log.info("catch-up: nothing newer than %s upstream",
                     self.db.get_state("sync.last_day") if self.db else "?")
            result.status = "skipped"
            return result
        if not symbols:
            result.status = "empty"
            return result

        if self.db:
            self.db.reap_sync_logs()          # a killed run leaves a row behind
        log_id = self.db.start_sync_log() if self.db else None
        try:
            with sync_lock(self.settings.data_dir):
                self._run(symbols, result, full=full)
        except SyncLocked as exc:
            result.status = "locked"
            result.errors.append(str(exc))
            log.warning("%s", exc)
        except BaseException as exc:   # noqa: BLE001 — always close the log row
            result.status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            result.errors.append(repr(exc))
            log.exception("sync aborted")
            if isinstance(exc, KeyboardInterrupt):
                raise
        finally:
            result.duration_s = time.time() - started
            if self.db and log_id:
                self.db.finish_sync_log(
                    log_id, finished_at=datetime.now(IST).isoformat(timespec="seconds"),
                    duration_s=result.duration_s, symbols=result.symbols,
                    updated=result.updated, refreshed=result.refreshed,
                    failed=result.failed, suspicious=result.suspicious,
                    status=result.status,
                    note=f"stale={result.stale} expected={result.expected_day}")
                if (self._coverage_allowed and result.expected_day is not None
                        and result.status in ("ok", "partial")):
                    self.db.set_state("sync.last_day", result.expected_day)
        log.info("sync %s in %.1fs: %s", result.status, result.duration_s,
                 result.as_dict())
        return result

    def symbols(self) -> list[str]:
        membership = self.settings.universe_dir / "membership.parquet"
        if membership.exists():
            symbols = pd.read_parquet(membership)["symbol"].astype(str)
            return sorted(set(symbols))
        return sorted(p.stem for p in self.daily_dir.glob("*.parquet"))

    # -------------------------------------------------------------- internal

    def _already_current(self) -> bool:
        """True when the last closed session is already in the cache, or a
        successful sync after its close proved upstream has nothing newer."""
        if not self.db:
            return False
        logged = self.db.get_state("sync.last_day")
        if logged and logged >= expected_session().isoformat():
            return True
        return attempted_since(self.db, expected_session())

    def _run(self, symbols: list[str], result: SyncResult, full: bool) -> None:
        plans = {sym: history_path(self.daily_dir, sym) for sym in symbols}
        last_dates = {sym: last_stored_date(p) for sym, p in plans.items()}
        self._progress("reading", 0, len(symbols))

        fetched = self._fetch_symbols(symbols, last_dates, full=full)

        days: set[Date] = set()
        for i, symbol in enumerate(symbols, 1):
            try:
                new = fetched.get(symbol)
                if new is None:
                    result.failed += 1
                    result.errors.append(f"{symbol}: no data returned")
                    self._state(symbol, last_dates[symbol], status="failed",
                                error="no data returned")
                    continue
                self._apply(symbol, plans[symbol], new, last_dates[symbol],
                            result, days, force_full=full, full_fetch=full)
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the run
                result.failed += 1
                result.errors.append(f"{symbol}: {exc}")
                self._state(symbol, last_dates[symbol], status="failed", error=str(exc))
            if i % 100 == 0 or i == len(symbols):
                self._progress("writing", i, len(symbols))
                log.info("sync progress %d/%d (updated %d, refreshed %d, failed %d)",
                         i, len(symbols), result.updated, result.refreshed,
                         result.failed)

        final = self._calendar_guard(symbols, plans, result, days)

        # A BSE feed can skip one session for most of the universe while the NSE
        # listing still has it. Repair that one date before reporting coverage.
        target = expected_session()
        if (self.settings.download.gap_fill and not full
                and result.expected_day is not None
                and result.expected_day < target):
            from .crosslist import fill_session_gap

            gap = fill_session_gap(self.settings, self.db, target, symbols)
            result.gap_filled = gap.filled
            result.gap_day = target if gap.filled else None
            if gap.errors:
                result.errors.extend(f"gap: {e}" for e in gap.errors)
            if gap.filled:
                result.updated += gap.filled
                days.add(target)
                final = {sym: last_stored_date(plans[sym]) for sym in symbols}
                result.expected_day = expected_last_day(final.values())
                if result.expected_day:
                    result.stale = sum(
                        1 for d in final.values()
                        if business_days_behind(d, result.expected_day) > STALE_BDAYS)

        self._progress("finalising", len(symbols), len(symbols))

        if self.db:
            if result.expected_day:
                # One early print must not enter the calendar the tracker
                # replays; keep only dates up to the session the universe
                # actually reached.
                days = {d for d in days if d <= result.expected_day}
            self.db.upsert_days(days)
            expected = result.expected_day
            at_session = sum(1 for d in final.values()
                             if expected and d and d >= expected)
            self._save_coverage(expected, at_session, len(symbols))
        result.status = ("ok" if result.failed == 0
                         else "partial" if result.failed < max(1, len(symbols) // 10)
                         else "failed")
        self._write_reports(result)

    def _progress(self, phase: str, current: int, total: int) -> None:
        if self._progress_cb is None:
            return
        try:
            self._progress_cb(phase, current, total)
        except Exception:  # noqa: BLE001 — progress must never break a sync
            log.debug("progress callback failed", exc_info=True)

    def _save_coverage(self, day: Date | None, at_session: int,
                       total: int) -> None:
        if not self.db or day is None or not self._coverage_allowed:
            return
        pct = round(100 * at_session / total) if total else 0
        if pct < 50:                  # not a session the scan or tracker can use
            return
        self.db.set_json("sync.coverage", {
            "day": day.isoformat(), "at_session": at_session, "total": total,
            "universe_day": self.db.universe_day(), "pct": pct,
        })

    def _fetch_symbols(self, symbols: list[str],
                       last_dates: dict[str, Date | None],
                       full: bool) -> dict[str, pd.DataFrame]:
        """Fetch tails with a bounded pool, then one cooled-down retry pass."""
        out = self._fetch_pool(symbols, last_dates, full)
        missing = [s for s in symbols if s not in out]
        if missing and len(missing) < len(symbols):
            log.info("retrying %d symbols that failed the first pass", len(missing))
            time.sleep(3)
            out.update(self._fetch_pool(missing, last_dates, full, attempts=2))
        return out

    def _fetch_pool(self, symbols: list[str],
                    last_dates: dict[str, Date | None], full: bool,
                    attempts: int = 3) -> dict[str, pd.DataFrame]:
        """Per-ticker rangefetches in a bounded thread pool.

        ``yf.download`` batches are not used: Yahoo intermittently returns
        duplicate keys that abort a whole chunk, and bisecting down to singles
        turned a 3-minute refresh into an hour. Per-ticker fetches are faster,
        isolated, and can back off per symbol.
        """
        end = expected_session() + timedelta(days=1)   # exclusive upper bound
        out: dict[str, pd.DataFrame] = {}
        done = 0
        total = len(symbols)
        lock = threading.Lock()

        def one(symbol: str) -> tuple[str, pd.DataFrame | None]:
            source = self._source(symbol)
            last = last_dates.get(symbol)
            for attempt in range(1, attempts + 1):
                try:
                    if full or last is None:
                        return symbol, self._max_fetch(source)
                    return symbol, self._single_fetch(
                        source, last - timedelta(days=14), end)
                except Exception as exc:  # noqa: BLE001 — back off, then report
                    if attempt == attempts:
                        log.debug("fetch failed for %s: %s", symbol, exc)
                        return symbol, None
                    time.sleep(0.6 * attempt)
            return symbol, None

        workers = max(4, self.workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, sym) for sym in symbols]
            for future in as_completed(futures):
                symbol, frame = future.result()
                if frame is not None:
                    out[symbol] = frame
                with lock:
                    done += 1
                    if done % 50 == 0 or done == total:
                        self._progress("fetching", done, total)
                        if done % 500 == 0:
                            log.info("fetch progress %d/%d", done, total)
        return out

    def _source(self, symbol: str) -> str:
        """Yahoo ticker to fetch: the NSE listing when BSE history is broken."""
        return self._sources.get(symbol, symbol)

    def _fallback_sources(self) -> dict[str, str]:
        path = self.settings.universe_dir / "ns_fallbacks.csv"
        if not path.exists():
            return {}
        try:
            frame = pd.read_csv(path)
        except Exception:  # noqa: BLE001 — a bad file must not block a sync
            log.warning("could not read %s", path)
            return {}
        if not {"ticker", "source"}.issubset(frame.columns):
            return {}
        return dict(zip(frame["ticker"].astype(str), frame["source"].astype(str),
                        strict=False))

    def _apply(self, symbol: str, path: Path, new: pd.DataFrame,
               last_date: Date | None, result: SyncResult,
               days: set[Date], force_full: bool = False,
               full_fetch: bool = False) -> None:
        new = _indexed(new)
        if new.empty:
            raise ValueError("empty frame")
        old = read_history(path)
        old_rows = 0 if old is None else len(old)

        if full_fetch and len(new) < 30 and old_rows >= 30:
            # Yahoo served a stub for a full fetch. Overwriting a 6,000-row
            # history with 10 bars would silently destroy the symbol; keep it.
            result.unchanged += 1
            self._state(symbol, old.index[-1].date(), bars=old_rows,
                        status="stale", error="stub response; kept stored history")
            log.warning("%s: stub response (%d rows), stored history kept",
                        symbol, len(new))
            return

        if force_full or old is None or old_rows < 30:
            merged = new
            refreshed = old is not None
        else:
            merged, mismatch = merge_history(old, new)
            refreshed = False
            if mismatch:
                log.info("%s: overlap mismatch -> full refresh", symbol)
                replacement = _indexed(self._max_fetch(self._source(symbol)))
                truncated = (old is not None and not replacement.empty
                             and (len(replacement) < int(old_rows * 0.5)
                                  or replacement.index.min() > old.index.min()))
                if (len(replacement) < 30 and old_rows >= 30) or truncated:
                    # Yahoo served a stub or a truncated refresh. Never replace
                    # a real history with it; leave the stored file untouched.
                    result.unchanged += 1
                    self._state(symbol, old.index[-1].date(), bars=old_rows,
                                status="stale",
                                error="truncated refresh; kept stored history")
                    log.warning("%s: truncated refresh (%d rows), stored history kept",
                                symbol, len(replacement))
                    return
                merged = replacement
                refreshed = True

        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        # never keep a live intraday bar, or a pending bar with no close
        cutoff = pd.Timestamp(expected_session())
        if len(merged) and merged.index[-1] > cutoff:
            merged = merged[merged.index <= cutoff]
        merged = merged[merged["Close"].notna()]
        if merged.empty:
            result.unchanged += 1
            self._state(symbol, old.index[-1].date() if old_rows else None,
                        bars=old_rows, status="stale",
                        error="no completed session in the response")
            return
        write_history_atomic(merged, path)
        days.update(d.date() for d in merged.index[-400:])

        if len(merged) < 30:
            result.unchanged += 1
            self._state(symbol, merged.index[-1].date(), bars=len(merged),
                        status="stale",
                        error=f"only {len(merged)} bars available upstream")
            return

        flags = quality_flags(merged)
        suspicious = int(flags.sum())
        recent = int(flags.tail(90).sum())
        status = "ok"
        error = None
        if recent:
            status = "suspicious"
            error = f"{suspicious} unexplained >35% bars ({recent} in the last 90)"
            result.suspicious += 1
            result.suspicious_dates.append(
                f"{symbol}:{','.join(str(d.date()) for d in merged.index[flags][-3:])}")
        elif suspicious:
            error = f"{suspicious} old unexplained >35% bars (before the last 90)"

        if refreshed:
            result.refreshed += 1
        elif old is None or merged.index[-1] > old.index[-1]:
            result.updated += 1
        else:
            result.unchanged += 1

        self._state(symbol, merged.index[-1].date(), bars=len(merged),
                    status=status, error=error)
        result.expected_day = _newer(result.expected_day, merged.index[-1].date())

    def _calendar_guard(self, symbols: list[str], plans: dict[str, Path],
                        result: SyncResult, days: set[Date]) -> dict[str, Date | None]:
        lasts = {sym: last_stored_date(plans[sym]) for sym in symbols}
        expected = expected_last_day(lasts.values())
        result.expected_day = expected
        if expected is None:
            message = ("no session reached by half the universe; coverage left "
                       "unchanged and no laggards retried")
            log.warning(message)
            result.note = message
            return lasts
        laggards = [s for s, d in lasts.items()
                    if business_days_behind(d, expected) > STALE_BDAYS]
        retry = [s for s in laggards
                 if business_days_behind(lasts[s], expected) <= DEAD_BDAYS]
        for sym in retry:
            try:
                start = (lasts[sym] or expected) - timedelta(days=14)
                new = self._single_fetch(self._source(sym), start,
                                         expected + timedelta(days=1))
                self._apply(sym, plans[sym], new, lasts[sym], result, days)
            except Exception as exc:  # noqa: BLE001 — recorded below
                log.debug("stale retry failed for %s: %s", sym, exc)
        final = {sym: last_stored_date(plans[sym]) for sym in symbols}
        stale = [s for s, d in final.items()
                 if business_days_behind(d, expected) > STALE_BDAYS]
        result.stale = len(stale)
        for sym in stale:
            self._state(sym, final[sym], status="stale",
                        error=f"last {final[sym]} vs expected {expected}")
        if stale:
            log.warning("%d symbols stale vs %s (retried %d)",
                        len(stale), expected, len(retry))
        return final

    def _state(self, symbol: str, last_date: Date | None, bars: int = 0,
               status: str = "ok", error: str | None = None) -> None:
        if self.db:
            self.db.set_sync_state(symbol, last_date, bars=bars,
                                   status=status, error=error)

    def _write_reports(self, result: SyncResult) -> None:
        self.settings.universe_dir.mkdir(parents=True, exist_ok=True)
        if result.errors:
            pd.DataFrame({"error": result.errors}).to_csv(
                self.failures_path, index=False)
        elif self.failures_path.exists():
            self.failures_path.unlink()
        if result.suspicious_dates:
            pd.DataFrame({"flag": result.suspicious_dates}).to_csv(
                self.quality_path, index=False)
        elif self.quality_path.exists():
            self.quality_path.unlink()


def _newer(a: Date | None, b: Date | None) -> Date | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
