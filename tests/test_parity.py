"""D2 parity gate: the live tracker must reproduce the backtest exactly.

Both paths replay identical synthetic histories. Any drift in stop logic,
fill order, sizing or exit classification fails this test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sta.backtest.costs import CostModel
from sta.backtest.engine import BacktestEngine, StrategyParams
from sta.core.config import DownloadConfig, Settings, UniverseConfig
from sta.live.db import Database
from sta.live.rules import initial_stop
from sta.live.settings import LiveSettings
from sta.live.tracker import Tracker
from sta.scanner.momentum import MomentumScanner, ScannerParams

SYMBOLS = [f"SYN{i:02d}" for i in range(30)]
DAYS = 320
WARMUP = 170


def _synthetic_bars(seed: int = 11) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=DAYS)
    out: dict[str, pd.DataFrame] = {}
    for i, symbol in enumerate(SYMBOLS):
        mu = 0.0012 + 0.00022 * i            # ranked drift -> clean RS spread
        vol = rng.normal(mu, 0.012, DAYS)
        if i % 3 == 0:                       # a late drawdown exercises exits
            vol[-70:] -= 0.006
        close = 100.0 * np.cumprod(1.0 + vol)
        prev = np.concatenate(([close[0]], close[:-1]))
        open_ = prev * (1 + rng.normal(0, 0.002, DAYS))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, DAYS)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, DAYS)))
        volume = np.full(DAYS, 900.0)
        volume[::7] = 4200.0                 # volume surge days
        volume[:30] = 1000.0
        out[symbol] = pd.DataFrame({
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume.astype(int), "value": close * volume,
            "adj_close": close, "prev_close": prev,
            "exchange": "BSE", "isin": f"INE{i:09d}", "name": symbol,
        }, index=dates)
    return out


@pytest.fixture(scope="module")
def universe():
    bars = _synthetic_bars()
    spans = pd.DataFrame([
        {"symbol": s, "exchange": "BSE", "isin": f"INE{i:09d}",
         "start": bars[s].index[0].date(), "end": bars[s].index[-1].date()}
        for i, s in enumerate(SYMBOLS)
    ])
    scanner = MomentumScanner(bars, spans, ScannerParams(max_candidates=10))
    days = list(bars[SYMBOLS[0]].index)
    return bars, scanner, days


def _engine_trades(bars, scanner, days, params):
    engine = BacktestEngine(scanner=scanner, bars=bars, trading_days=days,
                            params=params, costs=CostModel())
    result = engine.run()
    return result.trades


def _tracker_trades(bars, scanner, days, params, tmp_path):
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    db = Database(":memory:").init()
    live = LiveSettings(
        position_size=params.position_size, max_positions=params.max_positions,
        auto_track=True, scan_top_n=10, stop_pct=params.stop_pct,
        target_pct=params.target_pct,
        trailing_atr_mult=params.trailing_atr_mult, atr_window=params.atr_window)
    tracker = Tracker(settings, db, live=live, costs=CostModel(),
                      histories=bars)
    db.upsert_days([d.date() for d in days])

    for i, day in enumerate(days):
        if i == 0:
            continue
        prev = days[i - 1]
        for cand in scanner.scan(prev.date()):
            close = float(cand.close)
            signal_id = db.insert_signal({
                "signal_date": prev.date(), "symbol": cand.symbol,
                "exchange": cand.exchange.value, "name": cand.name,
                "score": cand.score, "close": close, "cap_class": "micro",
                "entry_plan": close,
                "stop_initial": initial_stop(close, params.stop_pct),
                "qty": int(params.position_size // close),
                "risk": 0.0, "dist_high": 0.0, "action": "track"})
            if signal_id:
                db.insert_trade({"signal_id": signal_id, "symbol": cand.symbol,
                                 "exchange": cand.exchange.value,
                                 "status": "suggested",
                                 "signal_date": prev.date()})
        tracker.update_day(day.date())
    return db.closed_trades()


def _key(trade) -> tuple:
    if isinstance(trade, dict) or hasattr(trade, "keys"):
        reason = trade["exit_reason"]
        return (trade["symbol"], str(trade["entry_date"])[:10],
                float(trade["entry_price"]), str(trade["exit_date"])[:10],
                float(trade["exit_price"]), reason,
                int(trade["shares"]))
    reason = (trade.exit_reason.value if hasattr(trade.exit_reason, "value")
              else str(trade.exit_reason))
    return (trade.symbol, str(trade.entry_date)[:10], float(trade.entry_price),
            str(trade.exit_date)[:10], float(trade.exit_price), reason, trade.shares)


def test_tracker_matches_engine_trade_for_trade(universe, tmp_path):
    bars, scanner, days = universe
    params = StrategyParams(trailing=True, max_positions=4,
                            position_size=15_000.0)
    engine_trades = [t for t in _engine_trades(bars, scanner, days, params)
                     if t.exit_date is not None]
    tracker_trades = _tracker_trades(bars, scanner, days, params, tmp_path)

    assert engine_trades, "synthetic universe produced no trades — fix the fixture"
    engine_keys = sorted(_key(t) for t in engine_trades)
    tracker_keys = sorted(_key(t) for t in tracker_trades)
    assert tracker_keys == engine_keys


def test_tracker_pnl_matches_engine(universe, tmp_path):
    bars, scanner, days = universe
    params = StrategyParams(trailing=True, max_positions=4,
                            position_size=15_000.0)
    engine = [t for t in _engine_trades(bars, scanner, days, params)
              if t.exit_date is not None]
    tracker = _tracker_trades(bars, scanner, days, params, tmp_path)
    engine_pnl = sorted(round(t.pnl, 4) for t in engine)
    tracker_pnl = sorted(round(float(r["pnl"]), 4) for r in tracker)
    assert tracker_pnl == engine_pnl


def test_catch_up_equals_daily_runs(universe, tmp_path):
    """A 3-day laptop gap must leave the same state as three daily runs."""
    bars, scanner, days = universe
    settings = Settings(root=tmp_path, universe=UniverseConfig(),
                        download=DownloadConfig())
    live = LiveSettings(position_size=15_000, max_positions=4, auto_track=True,
                        scan_top_n=10, stop_pct=0.10, target_pct=0.30,
                        trailing_atr_mult=3.0, atr_window=22)

    # one tracker runs every day; the other is only started at the end
    db_a = Database(":memory:").init()
    db_b = Database(":memory:").init()
    for db in (db_a, db_b):
        db.upsert_days([d.date() for d in days])

    window = [d.date() for d in days[-60:]]
    # suggestions are produced at each close, exactly like the live app
    for day in window[:-1]:
        for cand in scanner.scan(day):
            close = float(cand.close)
            for db in (db_a, db_b):
                sid = db.insert_signal({
                    "signal_date": day, "symbol": cand.symbol,
                    "exchange": cand.exchange.value, "name": cand.name,
                    "score": cand.score, "close": close, "cap_class": "micro",
                    "entry_plan": close,
                    "stop_initial": initial_stop(close, 0.10),
                    "qty": int(15_000 // close), "risk": 0.0, "dist_high": 0.0,
                    "action": "track"})
                if sid:
                    db.insert_trade({"signal_id": sid, "symbol": cand.symbol,
                                     "exchange": cand.exchange.value,
                                     "status": "suggested",
                                     "signal_date": day})

    ta = Tracker(settings, db_a, live=live, costs=CostModel(), histories=bars)
    tb = Tracker(settings, db_b, live=live, costs=CostModel(), histories=bars)
    first = db_a.first_signal_day()
    for day in window:
        if day.isoformat() >= first:      # cold start begins at the first signal
            ta.update_day(day)
    tb.catch_up(window[-1])

    def state(db):
        return {
            "open": sorted((r["symbol"], round(float(r["entry_price"]), 6),
                            round(float(r["stop_live"] or 0), 6))
                           for r in db.open_trades()),
            "closed": sorted((r["symbol"], str(r["exit_date"]),
                              round(float(r["exit_price"]), 6), r["exit_reason"])
                             for r in db.closed_trades()),
            "cash": round(float(db.get_state("cash")), 6),
            "equity": [(r["day"], round(r["nav"], 6)) for r in db.equity_series()],
            "suggested": [(r["symbol"], r["status"]) for r in db.suggested_trades()],
        }

    a = state(db_a)
    assert a["equity"], "the gap window must produce equity rows"
    assert a["closed"] or a["open"], "the gap window must produce positions"
    assert a == state(db_b)
