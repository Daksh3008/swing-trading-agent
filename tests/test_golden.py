"""D8 golden test: tracker vs backtest on the real cache, last 12 months.

Opt-in because it reads (and scans) real data:

    $env:STA_GOLDEN = "1"
    python -m pytest tests/test_golden.py -q

Skipped by default so the normal suite stays fast and offline.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pandas as pd
import pytest

from sta.backtest.costs import CostModel
from sta.backtest.engine import BacktestEngine, StrategyParams
from sta.core.config import load_settings
from sta.data.store import ParquetBarStore
from sta.data.universe import membership_spans
from sta.live.db import Database
from sta.live.rules import initial_stop
from sta.live.settings import LiveSettings
from sta.live.tracker import Tracker
from sta.scanner.momentum import MomentumScanner, ScannerParams

pytestmark = pytest.mark.skipif(
    os.environ.get("STA_GOLDEN") != "1",
    reason="set STA_GOLDEN=1 to run against the real download cache")

SYMBOL_CAP = 120


def _load():
    settings = load_settings()
    membership = pd.read_parquet(settings.universe_dir / "membership.parquet")
    store = ParquetBarStore(settings.data_dir)
    symbols = sorted(membership["symbol"].astype(str))
    step = max(1, len(symbols) // SYMBOL_CAP)
    picked = symbols[::step][:SYMBOL_CAP]
    histories = {}
    for sym in picked:
        hist = store.load_symbol(sym)
        if hist is not None and len(hist) >= 300:
            histories[sym] = hist
    if len(histories) < 10:
        pytest.skip("not enough cached history on this machine")
    last = max(h.index[-1] for h in histories.values())
    start = last - timedelta(days=365)
    days = sorted({d for h in histories.values()
                   for d in h.index if d >= start})
    return settings, membership, histories, days


def test_tracker_reproduces_last_12_months(tmp_path):
    settings, membership, histories, days = _load()
    spans = membership_spans(membership, histories)
    scanner = MomentumScanner(histories, spans, ScannerParams(max_candidates=10))
    params = StrategyParams(trailing=True, max_positions=4, position_size=15_000)

    engine = BacktestEngine(scanner=scanner, bars=histories, trading_days=days,
                            params=params, costs=CostModel())
    expected = [t for t in engine.run().trades if t.exit_date is not None]

    db = Database(tmp_path / "golden.db").init()
    live = LiveSettings(position_size=15_000, max_positions=4, auto_track=True,
                        scan_top_n=10, stop_pct=params.stop_pct,
                        target_pct=params.target_pct,
                        trailing_atr_mult=params.trailing_atr_mult,
                        atr_window=params.atr_window)
    tracker = Tracker(settings, db, live=live, costs=CostModel(),
                      histories=histories)
    db.upsert_days([d.date() for d in days])
    for i, day in enumerate(days):
        if i == 0:
            continue
        prev = days[i - 1].date()
        for cand in scanner.scan(prev):
            close = float(cand.close)
            sid = db.insert_signal({
                "signal_date": prev, "symbol": cand.symbol,
                "exchange": cand.exchange.value, "name": cand.name,
                "score": cand.score, "close": close, "cap_class": "",
                "entry_plan": close,
                "stop_initial": initial_stop(close, params.stop_pct),
                "qty": int(15_000 // close), "risk": 0.0, "dist_high": 0.0,
                "action": "track"})
            if sid:
                db.insert_trade({"signal_id": sid, "symbol": cand.symbol,
                                 "exchange": cand.exchange.value,
                                 "status": "suggested", "signal_date": prev})
        tracker.update_day(day.date())

    actual = db.closed_trades()
    assert expected, "no closed trades in the window"
    assert len(actual) == len(expected)

    def key_engine(t):
        return (t.symbol, str(t.entry_date)[:10], round(t.entry_price, 4),
                str(t.exit_date)[:10], round(t.exit_price, 4),
                t.exit_reason.value, t.shares)

    def key_db(row):
        return (row["symbol"], str(row["entry_date"])[:10],
                round(float(row["entry_price"]), 4),
                str(row["exit_date"])[:10], round(float(row["exit_price"]), 4),
                row["exit_reason"], int(row["shares"]))

    assert sorted(map(key_db, actual)) == sorted(map(key_engine, expected))
