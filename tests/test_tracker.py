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


def test_no_cash_expires_the_queue_instead_of_lingering(rig, tmp_path):
    """A suggestion whose fill day cannot afford it is expired, not left queued."""
    db, tracker, _ = rig
    _queue(db, "AAA.BO", 0.9)
    db.set_state("cash", 100.0)          # far below one slot
    tracker.update_day(DAYS[1])
    assert db.open_trades() == []
    assert len(db.trades("expired")) == 1
    assert len(db.suggested_trades()) == 0


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


# ------------------------------------------ trusted session + marking (E6)

def _coverage(db, day):
    db.set_json("sync.coverage", {"day": day.isoformat(), "at_session": 1,
                                  "total": 1, "pct": 100})


def test_cursor_does_not_advance_past_the_trusted_session(rig):
    db, tracker, _ = rig
    _coverage(db, DAYS[1])
    _queue(db, "AAA.BO", 0.9)

    first = tracker.update_day(DAYS[1])
    assert not first.skipped
    assert db.get_state("tracker.last_day") == DAYS[1].isoformat()

    blocked = tracker.update_day(DAYS[2])
    assert blocked.skipped
    assert blocked.reason and "covered through" in blocked.reason
    assert db.get_state("tracker.last_day") == DAYS[1].isoformat()
    assert db.equity_on(DAYS[2]) is None


def test_catch_up_stops_at_the_trusted_session(rig):
    db, tracker, _ = rig
    _coverage(db, DAYS[1])
    _queue(db, "AAA.BO", 0.9)

    outcomes = tracker.catch_up()

    assert outcomes[-1].day == DAYS[1]
    assert all(o.day <= DAYS[1] for o in outcomes)
    assert db.get_state("tracker.last_day") == DAYS[1].isoformat()


def test_position_without_a_bar_is_valued_at_its_last_close(tmp_path):
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    db = Database(":memory:").init()
    db.upsert_days(DAYS)
    live = LiveSettings(position_size=15_000, max_positions=4, auto_track=False,
                        scan_top_n=10, stop_pct=0.10, target_pct=0.30,
                        trailing_atr_mult=3.0, atr_window=22)
    idx = pd.DatetimeIndex([pd.Timestamp(DAYS[0]), pd.Timestamp(DAYS[1]),
                            pd.Timestamp(DAYS[2])])          # no DAYS[3] bar
    prices = np.array([100.0, 100.0, 110.0])
    bars = {"AAA.BO": pd.DataFrame({
        "open": prices, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices, "volume": 1000, "value": prices * 1000,
        "adj_close": prices, "prev_close": prices,
        "exchange": "BSE", "isin": "", "name": "AAA",
    }, index=idx)}
    tracker = Tracker(settings, db, live=live, costs=FREE, histories=bars)
    _queue(db, "AAA.BO", 0.9, day=DAYS[0])

    tracker.update_day(DAYS[1])          # fills at 100, marks 100
    tracker.update_day(DAYS[2])          # marks 110
    outcome = tracker.update_day(DAYS[3])   # no bar: must still value at 110

    trade = db.open_trades()[0]
    assert trade["shares"] == 150
    assert outcome.nav == pytest.approx(60_000 - 15_000 + 110.0 * 150)
    assert db.equity_on(DAYS[3])["positions_value"] == pytest.approx(110.0 * 150)
    marks = db.marks_for(trade["id"])
    assert marks[-1]["date"] == DAYS[2].isoformat()   # no fake mark row


def test_reconcile_rolls_back_a_phantom_session(tmp_path):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 100, 100])
    _coverage(db, DAYS[1])
    tracker.update_day(DAYS[1])              # fills at 100

    trade = db.open_trades()[0]
    db.update_trade(trade["id"], days_held=2)       # the old bug aged it
    db.set_state("tracker.last_day", DAYS[3].isoformat())
    db.upsert_equity(DAYS[3], 0.0, 0.0, 123.0, 1)

    result = tracker.reconcile()

    assert result["changed"] is True
    assert result["cursor"] == DAYS[1].isoformat()
    assert db.equity_on(DAYS[3]) is None
    assert db.open_trades()[0]["days_held"] == 0
    assert db.get_state("tracker.last_day") == DAYS[1].isoformat()


def test_reconcile_is_a_noop_when_the_cursor_is_safe(rig):
    db, tracker, _ = rig
    _coverage(db, DAYS[2])
    db.set_state("tracker.last_day", DAYS[2].isoformat())

    assert tracker.reconcile()["changed"] is False
    assert db.get_state("tracker.last_day") == DAYS[2].isoformat()


def test_position_is_never_judged_on_its_entry_session(tmp_path):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 80, 80])
    _coverage(db, DAYS[2])
    db.set_state("tracker.last_day", DAYS[1].isoformat())
    signal_id = _queue(db, "AAA.BO", 0.9, day=DAYS[1])
    trade_id = db.trade_for_signal(signal_id)["id"]
    # simulate an interrupted replay: the entry landed, the cursor did not move
    db.update_trade(trade_id, status="open", entry_date=DAYS[2], entry_price=100.0,
                    shares=150, stop_initial=90.0, stop_live=90.0, target=130.0,
                    high_water=100.0, entry_cost=0.0, days_held=0, pnl=0.0)

    tracker.update_day(DAYS[2])          # opens at 80, below the stop

    assert db.open_trades(), "a same-day position must not be exited"
    assert not db.closed_trades()


def test_reconcile_rolls_back_an_entry_after_the_trusted_session(tmp_path):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 100, 100])
    _coverage(db, DAYS[1])
    signal_id = _queue(db, "AAA.BO", 0.9, day=DAYS[1])
    trade_id = db.trade_for_signal(signal_id)["id"]
    db.update_trade(trade_id, status="open", entry_date=DAYS[2], entry_price=100.0,
                    shares=150, stop_initial=90.0, stop_live=90.0, target=130.0,
                    high_water=100.0, entry_cost=1.0, days_held=0, pnl=0.0)
    db.set_state("cash", 44_999.0)           # 60,000 - 15,000 - 1 cost

    result = tracker.reconcile()

    assert result["changed"] is True
    assert result["entries_rolled_back"] == 1
    trade = db.trade(trade_id)
    assert trade["status"] == "suggested"
    assert trade["entry_price"] is None
    assert float(db.get_state("cash")) == pytest.approx(60_000.0)


def test_reconcile_reopens_an_exit_after_the_trusted_session(tmp_path):
    db, tracker, _ = _falling_rig(tmp_path, [100, 100, 80, 80])
    _coverage(db, DAYS[1])
    tracker.update_day(DAYS[1])              # fills 150 at 100, cash 45,000
    trade = db.open_trades()[0]
    # the old build closed it on DAYS[2] and banked the proceeds
    db.update_trade(trade["id"], status="closed", exit_date=DAYS[2],
                    exit_price=80.0, exit_cost=0.0, exit_reason="stop_initial",
                    pnl=-3_000.0)
    db.set_state("cash", 57_000.0)           # 45,000 + 80 * 150

    result = tracker.reconcile()

    assert result["exits_reopened"] == 1
    reopened = db.trade(trade["id"])
    assert reopened["status"] == "open"
    assert reopened["exit_price"] is None and reopened["pnl"] is None
    assert float(db.get_state("cash")) == pytest.approx(45_000.0)
    assert reopened["days_held"] == 0


def test_trusted_day_ignores_a_sparse_sync_state(tmp_path):
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    settings.ensure_dirs()
    for i in range(4):
        (settings.daily_dir / f"S{i}.BO.parquet").write_bytes(b"")
    db = Database(tmp_path / "data" / "app.db").init()
    db.set_sync_state("S0.BO", "2024-03-02")     # one of four symbols

    assert db.trusted_day() is None
