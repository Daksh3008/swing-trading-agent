"""E5: repair one missing BSE session from the company's NSE listing."""

from __future__ import annotations

import pandas as pd

from sta.core.config import DownloadConfig, Settings, UniverseConfig
from sta.data.crosslist import fill_session_gap
from sta.data.sync import read_history, write_history_atomic
from sta.live.db import Database

DATES = pd.bdate_range("2024-01-01", periods=10)
GAP = pd.bdate_range(DATES[-1] + pd.Timedelta(days=1), periods=1)[0].date()


def _settings(tmp_path) -> Settings:
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    settings.ensure_dirs()
    return settings


def _frame(dates, closes) -> pd.DataFrame:
    closes = list(closes)
    return pd.DataFrame({
        "Date": pd.to_datetime(dates),
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes,
        "Volume": [1000] * len(closes), "Adj_Close": closes,
    })


def _seed(settings, symbol="AAA.BO", closes=None, dates=None):
    dates = DATES if dates is None else dates
    closes = closes or [100.0 + i for i in range(len(dates))]
    write_history_atomic(_frame(dates, closes).set_index("Date"),
                         settings.daily_dir / f"{symbol}.parquet")


def _db(tmp_path) -> Database:
    db = Database(":memory:").init()
    db.set_sync_state("AAA.BO", DATES[-1].date(), bars=len(DATES))
    return db


def _feed(ns: pd.DataFrame):
    def fetch(tickers, start, end):
        return {"AAA.NS": ns}
    return fetch


def test_validated_gap_is_filled(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    db = _db(tmp_path)
    closes = [100.0 + i for i in range(len(DATES))]
    ns = _frame(list(DATES) + [GAP], closes + [closes[-1] + 1.0])

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=_feed(ns))

    assert result.filled == 1
    assert result.skipped_mismatch == 0
    hist = read_history(settings.daily_dir / "AAA.BO.parquet")
    assert hist.index[-1].date() == GAP
    assert hist["Close"].iloc[-1] == closes[-1] + 1.0
    assert db.sync_state("AAA.BO")["last_date"] == GAP.isoformat()
    assert db.trading_days()[-1] == GAP.isoformat()


def test_divergent_listing_is_not_used(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    db = _db(tmp_path)
    closes = [100.0 + i for i in range(len(DATES))]
    # the "NSE" series is twice the price: a different instrument, not a listing
    ns = _frame(list(DATES) + [GAP], [c * 2 for c in closes] + [(closes[-1] + 1) * 2])

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=_feed(ns))

    assert result.filled == 0
    assert result.skipped_mismatch == 1
    hist = read_history(settings.daily_dir / "AAA.BO.parquet")
    assert hist.index[-1].date() == DATES[-1].date()


def test_a_gap_bar_that_implies_a_crash_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    db = _db(tmp_path)
    closes = [100.0 + i for i in range(len(DATES))]
    # tracks the listing on the overlap, then prints a 60% one-day fall
    ns = _frame(list(DATES) + [GAP], closes + [closes[-1] * 0.4])

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=_feed(ns))

    assert result.filled == 0
    assert result.skipped_quality == 1
    hist = read_history(settings.daily_dir / "AAA.BO.parquet")
    assert hist.index[-1].date() == DATES[-1].date()


def test_symbols_already_current_are_skipped(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    db = Database(":memory:").init()
    db.set_sync_state("AAA.BO", GAP.isoformat(), bars=len(DATES) + 1)

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=lambda *_: {})

    assert result.candidates == 0
    assert result.filled == 0


def test_a_longer_gap_is_not_filled(tmp_path):
    settings = _settings(tmp_path)
    closes = [100.0 + i for i in range(len(DATES))]
    _seed(settings, closes=closes[:7], dates=DATES[:7])   # ends three sessions early
    db = Database(":memory:").init()
    db.set_sync_state("AAA.BO", DATES[6].date(), bars=7)
    ns = _frame(list(DATES) + [GAP], closes + [closes[-1] + 1.0])

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=_feed(ns))

    assert result.candidates == 0
    assert result.filled == 0
    hist = read_history(settings.daily_dir / "AAA.BO.parquet")
    assert hist.index[-1].date() == DATES[6].date()


def test_missing_listing_counts_as_missing(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    db = _db(tmp_path)

    result = fill_session_gap(settings, db, GAP, ["AAA.BO"],
                              batch_fetch=lambda *_: {})

    assert result.candidates == 1
    assert result.skipped_missing == 1
    assert result.filled == 0


def test_no_database_is_a_noop(tmp_path):
    settings = _settings(tmp_path)
    result = fill_session_gap(settings, None, GAP, ["AAA.BO"])
    assert result.filled == 0 and result.candidates == 0


def test_gap_day_is_a_real_business_day():
    assert GAP.weekday() < 5


