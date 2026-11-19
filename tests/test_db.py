"""D8: SQLite CRUD and idempotency."""

from __future__ import annotations

from datetime import date

import pytest

from sta.live.db import Database


@pytest.fixture()
def db():
    return Database(":memory:").init()


def _signal(**over):
    row = {
        "signal_date": date(2024, 3, 1), "symbol": "AAA.BO", "exchange": "BSE",
        "name": "AAA", "score": 0.9, "close": 100.0, "cap_class": "micro",
        "entry_plan": 100.0, "stop_initial": 90.0, "qty": 150, "risk": 1500.0,
        "dist_high": -0.02, "action": "new",
    }
    row.update(over)
    return row


def test_signal_insert_is_idempotent(db):
    first = db.insert_signal(_signal())
    second = db.insert_signal(_signal())
    assert first is not None and second is None
    assert len(db.signals_for(date(2024, 3, 1))) == 1


def test_signal_actions_and_tracking(db):
    sid = db.insert_signal(_signal())
    db.set_signal_action(sid, "track")
    assert db.tracked_signals(date(2024, 3, 1))[0]["id"] == sid
    assert db.signal(sid)["action"] == "track"
    assert db.first_signal_day() == "2024-03-01"
    assert db.latest_signal_day() == "2024-03-01"


def test_trade_lifecycle(db):
    sid = db.insert_signal(_signal())
    trade_id = db.insert_trade({"signal_id": sid, "symbol": "AAA.BO",
                                "status": "suggested",
                                "signal_date": date(2024, 3, 1)})
    assert db.trade_for_signal(sid)["status"] == "suggested"
    assert len(db.suggested_trades()) == 1

    db.update_trade(trade_id, status="open", entry_date=date(2024, 3, 4),
                    entry_price=101.0, shares=148, stop_initial=90.9,
                    stop_live=90.9, target=131.3, high_water=101.0,
                    entry_cost=21.0, pnl=0.0)
    assert db.has_open_trade("AAA.BO")
    assert db.open_symbols() == {"AAA.BO"}
    assert len(db.open_trades()) == 1

    db.update_trade(trade_id, status="closed", exit_date=date(2024, 3, 20),
                    exit_price=120.0, exit_cost=21.0,
                    exit_reason="stop_trailing", pnl=2788.0)
    assert db.open_trades() == []
    closed = db.closed_trades()
    assert closed[0]["exit_reason"] == "stop_trailing"
    assert closed[0]["pnl"] == pytest.approx(2788.0)


def test_marks_upsert_per_day(db):
    db.upsert_mark(1, date(2024, 3, 4), 101.0, 90.9, 101.0, 0.0)
    db.upsert_mark(1, date(2024, 3, 4), 104.0, 92.0, 104.0, 450.0)
    rows = db.marks_for(1)
    assert len(rows) == 1
    assert rows[0]["close"] == 104.0
    assert db.latest_marks()[1]["high_water"] == 104.0


def test_equity_and_days(db):
    db.upsert_equity(date(2024, 3, 4), 45_000, 15_000, 60_000, 1)
    db.upsert_equity(date(2024, 3, 4), 45_000, 16_000, 61_000, 1)
    assert len(db.equity_series()) == 1
    assert db.equity_on(date(2024, 3, 4))["nav"] == 61_000

    db.upsert_days([date(2024, 3, 1), date(2024, 3, 4), "2024-03-05"])
    db.upsert_days([date(2024, 3, 4)])           # duplicate ignored
    assert db.previous_day(date(2024, 3, 4)) == "2024-03-01"
    assert db.next_day(date(2024, 3, 4)) == "2024-03-05"
    assert db.last_day() == "2024-03-05"
    assert db.trading_days("2024-03-04") == ["2024-03-04", "2024-03-05"]


def test_sync_state_and_log(db):
    db.set_sync_state("AAA.BO", date(2024, 3, 5), bars=1200, status="suspicious",
                      error="2 unexplained bars")
    db.set_sync_state("AAA.BO", date(2024, 3, 5), bars=1200, status="ok")
    assert db.sync_state("AAA.BO")["status"] == "ok"
    assert db.flagged_symbols() == set()

    log_id = db.start_sync_log()
    db.finish_sync_log(log_id, symbols=10, updated=9, status="ok", duration_s=3.0)
    assert db.last_sync_log()["updated"] == 9


def test_app_state_json(db):
    assert db.get_state("missing") is None
    db.set_state("cash", 60_000)
    assert float(db.get_state("cash")) == 60_000
    db.set_json("live.settings", {"max_positions": 3, "auto_track": True})
    assert db.get_json("live.settings")["max_positions"] == 3
    assert db.get_json("nope", {}) == {}
