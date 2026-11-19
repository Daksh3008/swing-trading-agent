"""D4: slot allocation, expiry and fill ordering."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from sta.backtest.costs import CostModel
from sta.core.config import DownloadConfig, Settings, UniverseConfig
from sta.live.db import Database
from sta.live.settings import LiveSettings
from sta.live.tracker import Tracker

DAYS = [date(2024, 3, 1) + timedelta(days=i) for i in range(4)]
SYMBOLS = ["AAA.BO", "BBB.BO", "CCC.BO", "DDD.BO", "EEE.BO", "FFF.BO"]


def _bars(price=100.0):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in DAYS])
    return pd.DataFrame({
        "open": price, "high": price * 1.01, "low": price * 0.99,
        "close": price, "volume": 1000, "value": price * 1000,
        "adj_close": price, "prev_close": price,
        "exchange": "BSE", "isin": "", "name": "X",
    }, index=idx)


FREE = CostModel(brokerage_per_order=0.0, stt_pct=0.0, exchange_pct=0.0,
                 sebi_pct=0.0, stamp_pct=0.0, gst_pct=0.0)


@pytest.fixture()
def rig(tmp_path):
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    db = Database(":memory:").init()
    db.upsert_days(DAYS)
    live = LiveSettings(position_size=15_000, max_positions=4, auto_track=False,
                        scan_top_n=10, stop_pct=0.10, target_pct=0.30,
                        trailing_atr_mult=3.0, atr_window=22)
    bars = {s: _bars() for s in SYMBOLS}
    # zero costs isolate slot allocation from the cash arithmetic
    tracker = Tracker(settings, db, live=live, costs=FREE, histories=bars)
    return db, tracker, live


def _queue(db, symbol, score, day=DAYS[0]):
    sid = db.insert_signal({
        "signal_date": day, "symbol": symbol, "exchange": "BSE",
        "name": symbol, "score": score, "close": 100.0, "cap_class": "micro",
        "entry_plan": 100.0, "stop_initial": 90.0, "qty": 150, "risk": 1500.0,
        "dist_high": 0.0, "action": "track"})
    db.insert_trade({"signal_id": sid, "symbol": symbol, "status": "suggested",
                     "signal_date": day})
    return sid


def test_best_ranked_fill_first_and_the_rest_expire(rig):
    db, tracker, _ = rig
    # clicked in ascending score order: the worst click comes first
    for symbol, score in [("FFF.BO", 0.60), ("EEE.BO", 0.62), ("DDD.BO", 0.64),
                          ("CCC.BO", 0.66), ("BBB.BO", 0.68), ("AAA.BO", 0.70)]:
        _queue(db, symbol, score)

    tracker.update_day(DAYS[1])

    opened = sorted(r["symbol"] for r in db.open_trades())
    assert opened == ["AAA.BO", "BBB.BO", "CCC.BO", "DDD.BO"]
    expired = sorted(r["symbol"] for r in db.trades("expired"))
    assert expired == ["EEE.BO", "FFF.BO"]
    assert len(db.open_trades()) == 4
    assert db.get_state("cash") is not None


def test_suggestion_expires_after_one_session(rig):
    db, tracker, _ = rig
    _queue(db, "AAA.BO", 0.7)
    tracker.update_day(DAYS[1])
    _queue(db, "BBB.BO", 0.5, day=DAYS[2])
    tracker.update_day(DAYS[2])
    bbb = db.trade_for_signal(db.signals_for(DAYS[2])[0]["id"])
    assert bbb["status"] == "suggested"      # fills on the session *after* its signal
    tracker.update_day(DAYS[3])
    statuses = {r["symbol"]: r["status"] for r in db.trades()}
    assert statuses["AAA.BO"] == "open"
    assert statuses["BBB.BO"] == "open"


def test_slots_full_expire_the_queue(rig):
    db, tracker, _ = rig
    for i, symbol in enumerate(SYMBOLS):
        _queue(db, symbol, 0.9 - i / 100)
    tracker.update_day(DAYS[1])
    tracker.update_day(DAYS[2])          # engine would also skip: slots full
    assert len(db.open_trades()) == 4
    assert len(db.trades("expired")) == 2


def test_rebase_book_resets_cash_and_equity(tmp_path):
    from sta.live.ops import rebase_book

    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    settings.ensure_dirs()
    db = Database(":memory:").init()
    db.upsert_days([DAYS[3]])
    db.set_state("cash", 60_000.0)
    db.upsert_equity(DAYS[3], 60_000.0, 0.0, 60_000.0, 0)

    result = rebase_book(settings, db, position_size=15_000.0, max_positions=5)

    assert result["book"] == 75_000.0
    assert float(db.get_state("cash")) == 75_000.0
    assert db.equity_series()[-1]["nav"] == 75_000.0


def test_rebase_refuses_with_open_positions(rig):
    from sta.live.ops import rebase_book

    db, tracker, _ = rig
    _queue(db, "AAA.BO", 0.9)
    tracker.update_day(DAYS[1])
    settings = Settings(root=__import__("pathlib").Path("."),
                        universe=UniverseConfig(), download=DownloadConfig())
    with pytest.raises(ValueError):
        rebase_book(settings, db, position_size=15_000.0, max_positions=5)


def _falling_rig(tmp_path, prices):
    """Two-session trade that stops out on the second day."""
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    db = Database(":memory:").init()
    db.upsert_days([date(2024, 3, 1) + timedelta(days=i) for i in range(len(prices))])
    live = LiveSettings(position_size=15_000, max_positions=4, auto_track=True,
                        scan_top_n=10, stop_pct=0.10, target_pct=0.30,
                        trailing_atr_mult=3.0, atr_window=22)
    idx = pd.DatetimeIndex([pd.Timestamp(date(2024, 3, 1) + timedelta(days=i))
                            for i in range(len(prices))])
    px = np.asarray(prices, dtype=float)
    bars = {"AAA.BO": pd.DataFrame({
        "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
        "volume": 1000, "value": px * 1000, "adj_close": px,
        "prev_close": np.concatenate(([px[0]], px[:-1])),
        "exchange": "BSE", "isin": "", "name": "AAA",
    }, index=idx)}
    tracker = Tracker(settings, db, live=live, costs=FREE, histories=bars)
    _queue(db, "AAA.BO", 0.9, day=date(2024, 3, 1))
    return db, tracker, settings


def test_exit_raises_one_alert_on_the_latest_session(tmp_path, monkeypatch):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 85])
    alerts = []
    monkeypatch.setattr("sta.live.ops.alert",
                        lambda settings, title, body: alerts.append((title, body)))

    tracker.update_day(date(2024, 3, 2))       # fills at 100
    assert alerts == []
    tracker.update_day(date(2024, 3, 3))       # opens at 85 -> stop

    assert len(alerts) == 1
    title, body = alerts[0]
    assert "AAA" in title
    assert "stop" in body.lower()


def test_no_alert_when_replaying_an_older_session(tmp_path, monkeypatch):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 85, 85])
    alerts = []
    monkeypatch.setattr("sta.live.ops.alert",
                        lambda settings, title, body: alerts.append((title, body)))

    tracker.update_day(date(2024, 3, 2))
    tracker.update_day(date(2024, 3, 3))       # exit, but day 4 is the latest
    assert db.closed_trades()
    assert alerts == []
