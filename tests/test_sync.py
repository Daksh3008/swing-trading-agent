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


def test_expected_last_day_ignores_a_lone_outlier():
    from datetime import date
    days = [date(2024, 3, 7)] * 18 + [date(2024, 3, 8)] * 2
    assert expected_last_day(days) == date(2024, 3, 7)
    days[0] = date(2024, 3, 8)
    days[1] = date(2024, 3, 8)
    days[2] = date(2024, 3, 8)
    assert expected_last_day(days) == date(2024, 3, 8)


def test_lock_blocks_concurrent_runs(tmp_path):
    settings = _settings(tmp_path)
    with sync_lock(settings.data_dir):
        with pytest.raises(SyncLocked):
            with sync_lock(settings.data_dir):
                pass
    # released afterwards
    with sync_lock(settings.data_dir):
        pass
