"""D1: incremental sync — idempotency, overlap repair, quality gate, lock."""

from __future__ import annotations

import pandas as pd
import pytest

from sta.core.config import DownloadConfig, Settings, UniverseConfig
from sta.data.sync import (
    DataSync,
    SyncLocked,
    expected_last_day,
    quality_flags,
    read_history,
    sync_lock,
    write_history_atomic,
)

DATES = pd.bdate_range("2024-01-01", periods=40)
LAST_CLOSE = 100.0 + len(DATES) - 1


def _frame(dates, closes, adj=None, volume=1000.0) -> pd.DataFrame:
    closes = list(closes)
    adj = list(adj if adj is not None else closes)
    return pd.DataFrame({
        "Date": pd.to_datetime(dates),
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes,
        "Volume": [volume] * len(closes), "Adj_Close": adj,
    })


def _settings(tmp_path) -> Settings:
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    settings.ensure_dirs()
    return settings


def _seed(settings, symbol, dates=DATES, closes=None):
    closes = closes or [100.0 + i for i in range(len(dates))]
    write_history_atomic(
        _frame(dates, closes).set_index("Date"), 
        settings.daily_dir / f"{symbol}.parquet")


class FakeFeed:
    """Deterministic stand-in for yfinance (per-ticker + max paths)."""

    def __init__(self, frames):
        self.frames = frames          # symbol -> full frame (Date column)
        self.max_calls = []
        self.single_calls = []

    def single(self, symbol, start, end):
        self.single_calls.append((symbol, start))
        df = self.frames[symbol]
        return df[pd.to_datetime(df["Date"]) >= pd.Timestamp(start)]

    def max(self, symbol):
        self.max_calls.append(symbol)
        return self.frames[symbol]


def _sync(settings, feed, **kwargs):
    return DataSync(settings, single_fetch=feed.single,
                    max_fetch=feed.max, sleep=0.0, workers=4, **kwargs)


def test_sync_fills_gap_and_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    extra = pd.bdate_range(DATES[-1] + pd.Timedelta(days=1), periods=3)
    for sym in ("AAA.BO", "BBB.BO", "CCC.BO"):
        _seed(settings, sym, DATES, [100.0 + i for i in range(len(DATES))])
    feed = FakeFeed({sym: _frame(list(DATES) + list(extra),
                                 [100.0 + i for i in range(len(DATES))]
                                 + [LAST_CLOSE + 1 + i for i in range(3)])
                     for sym in ("AAA.BO", "BBB.BO", "CCC.BO")})

    result = _sync(settings, feed).run()
    assert result.status == "ok"
    assert result.updated == 3
    assert result.failed == 0
    for sym in ("AAA.BO", "BBB.BO", "CCC.BO"):
        hist = read_history(settings.daily_dir / f"{sym}.parquet")
        assert hist.index[-1].date() == extra[-1].date()
        assert len(hist) == len(DATES) + 3

    # second run with identical upstream data: nothing changes
    before = {p.name: p.read_bytes() for p in settings.daily_dir.glob("*.parquet")}
    again = _sync(settings, feed).run()
    after = {p.name: p.read_bytes() for p in settings.daily_dir.glob("*.parquet")}
    assert again.updated == 0
    assert again.unchanged == 3
    assert before == after


def test_overlap_mismatch_triggers_full_refresh(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "AAA.BO", DATES, [100.0 + i for i in range(len(DATES))])
    # upstream rewrote the last stored bars (a split) and added one day
    extra = pd.bdate_range(DATES[-1] + pd.Timedelta(days=1), periods=1)
    rewritten = [50.0 + i for i in range(len(DATES))]
    feed = FakeFeed({"AAA.BO": _frame(list(DATES) + list(extra),
                                      rewritten + [60.0])})

    result = _sync(settings, feed).run()
    assert feed.max_calls == ["AAA.BO"]
    assert result.refreshed == 1
    hist = read_history(settings.daily_dir / "AAA.BO.parquet")
    assert hist["Close"].iloc[0] == pytest.approx(50.0)


def test_stub_refresh_never_destroys_stored_history(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "AAA.BO", DATES, [100.0 + i for i in range(len(DATES))])
    path = settings.daily_dir / "AAA.BO.parquet"
    before = path.read_bytes()
    rewritten = [50.0 + i for i in range(len(DATES))]

    class StubFeed(FakeFeed):
        def single(self, symbol, start, end):
            return _frame(DATES, rewritten)          # overlap mismatch
        def max(self, symbol):
            return _frame(DATES[:5], [50.0] * 5)     # a 5-row stub

    result = _sync(settings, StubFeed({})).run()

    assert result.refreshed == 0
    assert result.unchanged >= 1
    assert path.read_bytes() == before


def test_fetch_failure_is_isolated(tmp_path):
    settings = _settings(tmp_path)
    for sym in ("AAA.BO", "BBB.BO"):
        _seed(settings, sym)
    feed = FakeFeed({s: _frame(DATES, [100.0 + i for i in range(len(DATES))])
                     for s in ("AAA.BO", "BBB.BO")})

    original = feed.single

    def flaky(symbol, start, end):
        if symbol == "BBB.BO":
            raise ValueError("upstream down")
        return original(symbol, start, end)

    feed.single = flaky
    result = _sync(settings, feed).run()
    assert result.failed == 1
    assert result.updated + result.unchanged == 1
    assert any("BBB.BO" in e for e in result.errors)


def test_sync_records_coverage(tmp_path):
    from sta.live.db import Database

    settings = _settings(tmp_path)
    extra = pd.bdate_range(DATES[-1] + pd.Timedelta(days=1), periods=1)
    for sym in ("AAA.BO", "BBB.BO"):
        _seed(settings, sym)
    feed = FakeFeed({sym: _frame(list(DATES) + list(extra),
                                 [100.0 + i for i in range(len(DATES))]
                                 + [LAST_CLOSE + 1])
                     for sym in ("AAA.BO", "BBB.BO")})
    db = Database(":memory:").init()
    _sync(settings, feed, db=db).run()

    coverage = db.get_json("sync.coverage")
    assert coverage["total"] == 2
    assert coverage["at_session"] == 2
    assert coverage["pct"] == 100
    assert coverage["day"] == extra[-1].date().isoformat()


def test_progress_callback_is_reported(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "AAA.BO")
    feed = FakeFeed({"AAA.BO": _frame(
        DATES, [100.0 + i for i in range(len(DATES))])})
    events = []
    _sync(settings, feed,
          progress=lambda phase, cur, total: events.append((phase, cur, total))).run()
    phases = [e[0] for e in events]
    assert "reading" in phases and "writing" in phases and "finalising" in phases
    assert any(cur == total for _, cur, total in events)


def test_quality_gate_flags_unexplained_crash():
    closes = [100, 101, 102, 60, 61]                 # -41% with no split factor
    df = _frame(pd.bdate_range("2024-01-01", periods=5), closes).set_index("Date")
    flags = quality_flags(df)
    assert flags.sum() == 1 and flags.iloc[3]

    split = [100, 101, 102, 51, 52]                  # 2:1 split explained by factor
    adj = [50, 50.5, 51, 51, 52]
    df2 = _frame(pd.bdate_range("2024-01-01", periods=5), split,
                 adj=adj).set_index("Date")
    assert quality_flags(df2).sum() == 0


def test_expected_last_day_ignores_a_minority_outlier():
    from datetime import date
    days = [date(2024, 3, 7)] * 18 + [date(2024, 3, 8)] * 2
    assert expected_last_day(days) == date(2024, 3, 7)
    # a real majority moves the session
    days = [date(2024, 3, 7)] * 10 + [date(2024, 3, 8)] * 10
    assert expected_last_day(days) == date(2024, 3, 8)
    # no majority at all is not a session the cache can support
    assert expected_last_day([date(2024, 3, 7)] * 4 + [date(2024, 3, 8)] * 3) is None


def test_lock_blocks_concurrent_runs(tmp_path):
    settings = _settings(tmp_path)
    with sync_lock(settings.data_dir):
        with pytest.raises(SyncLocked):
            with sync_lock(settings.data_dir):
                pass
    # released afterwards
    with sync_lock(settings.data_dir):
        pass


# ------------------------------------------------------ source lag (E4)

def _log(db, finished_at: str, status: str = "ok", symbols: int = 100,
         failed: int = 0):
    log_id = db.start_sync_log()
    db.finish_sync_log(log_id, finished_at=finished_at, symbols=symbols,
                       failed=failed, status=status)


def test_attempted_since_requires_a_run_after_the_publish_window():
    from datetime import date

    from sta.data.sync import attempted_since
    from sta.live.db import Database

    db = Database(":memory:").init()
    assert attempted_since(db, date(2026, 11, 18)) is False

    # 16:05 is after the close but before the 18:30 publish window: not enough
    _log(db, "2026-11-18T16:05:00", symbols=100)
    assert attempted_since(db, date(2026, 11, 18)) is False

    _log(db, "2026-11-18T18:30:00", symbols=100)
    assert attempted_since(db, date(2026, 11, 18)) is True


def test_attempted_since_ignores_failed_tiny_or_broken_runs():
    from datetime import date

    from sta.data.sync import attempted_since
    from sta.live.db import Database

    for status, symbols, failed in [("failed", 100, 0), ("interrupted", 100, 0),
                                    ("ok", 10, 0), ("partial", 100, 80)]:
        db = Database(":memory:").init()
        _log(db, "2026-11-19T09:00:00", status=status, symbols=symbols, failed=failed)
        assert attempted_since(db, date(2026, 11, 18)) is False, (status, symbols, failed)


def test_catch_up_skips_after_a_verified_source_lag(tmp_path):
    from datetime import timedelta

    from sta.core.market_calendar import session_cutoff
    from sta.data.sync import expected_session
    from sta.live.db import Database

    settings = _settings(tmp_path)
    db = Database(":memory:").init()
    expected = expected_session()
    db.set_state("sync.last_day", (expected - timedelta(days=1)).isoformat())
    published = session_cutoff(expected).replace(hour=20).isoformat(timespec="seconds")
    _log(db, published, status="partial")

    result = DataSync(settings, db).run(catch_up=True)

    assert result.status == "skipped"


def test_limit_run_never_advances_the_sync_cursor(tmp_path):
    settings = _settings(tmp_path)
    for sym in ("AAA.BO", "BBB.BO"):
        _seed(settings, sym)
    feed = FakeFeed({sym: _frame(DATES, [100.0 + i for i in range(len(DATES))])
                     for sym in ("AAA.BO", "BBB.BO")})
    from sta.live.db import Database

    db = Database(":memory:").init()

    result = _sync(settings, feed, db=db).run(limit=1)

    assert result.status == "ok"
    assert db.get_state("sync.last_day") is None
    assert db.get_json("sync.coverage") is None


def test_catch_up_skips_when_the_cache_already_has_the_session(tmp_path):
    from sta.data.sync import expected_session
    from sta.live.db import Database

    settings = _settings(tmp_path)
    db = Database(":memory:").init()
    db.set_state("sync.last_day", expected_session().isoformat())

    assert DataSync(settings, db).run(catch_up=True).status == "skipped"
