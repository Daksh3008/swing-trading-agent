"""D8: rule-level unit tests — the invariants that protect paper capital."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from sta.backtest.costs import CostModel
from sta.backtest.engine import BacktestEngine, ExitReason, StrategyParams
from sta.core.models import Exchange, ScanCandidate
from sta.live.rules import (
    PositionView,
    RuleParams,
    atr_series,
    classify_stop,
    exit_signal,
    initial_stop,
    initial_target,
    shares_for,
    update_trailing_stop,
)

P = RuleParams(trailing=True, trailing_atr_mult=3.0, atr_window=22)


def _view(entry=100.0, stop=90.0, live=90.0, target=130.0, hw=100.0, held=0):
    return PositionView(entry_price=entry, stop_initial=stop, stop_live=live,
                        target=target, high_water=hw, bars_held=held)


def _bar(o=100.0, h=101.0, lo=99.0, c=100.0, prev=100.0):
    return {"open": o, "high": h, "low": lo, "close": c, "prev_close": prev}


# ---------------------------------------------------------------- arithmetic


def test_initial_stop_and_target():
    assert initial_stop(100.0, 0.10) == pytest.approx(90.0)
    assert initial_target(100.0, 0.30) == pytest.approx(130.0)
    assert shares_for(15_000, 99.5) == 150
    assert shares_for(15_000, 0) == 0


def test_atr_uses_only_prior_sessions():
    dates = pd.bdate_range("2024-01-01", periods=30)
    closes = np.full(30, 100.0)
    df = pd.DataFrame({"high": closes + 1, "low": closes - 1, "close": closes},
                      index=dates)
    df.loc[dates[20], "high"] = 400.0            # an artificial spike
    atr = atr_series(df, 22)
    # the spike is visible on the *next* session only
    assert atr.loc[dates[20]] == pytest.approx(atr.loc[dates[19]])
    assert atr.loc[dates[21]] > atr.loc[dates[20]]


# --------------------------------------------------------------------- stops


def test_trailing_stop_ratchets_monotonically():
    stop = 90.0
    for hw, atr in [(100, 3), (105, 2), (104, 5), (110, 1), (108, 9)]:
        updated = update_trailing_stop(entry_price=100, high_water=hw,
                                       stop_live=stop, atr_prev=atr, params=P)
        assert updated >= stop
        stop = updated
    assert stop > 90.0


def test_profit_floor_locks_five_percent():
    stop = update_trailing_stop(entry_price=100, high_water=112,  # +12% run
                                stop_live=99, atr_prev=10, params=P)
    assert stop == pytest.approx(105.0)          # chandelier would be 82


def test_chandelier_governs_above_release_but_never_gives_back():
    locked = update_trailing_stop(entry_price=100, high_water=112,
                                  stop_live=99, atr_prev=10, params=P)
    above = update_trailing_stop(entry_price=100, high_water=115,  # > +14%
                                 stop_live=locked, atr_prev=10, params=P)
    assert above == pytest.approx(locked)        # monotonic floor survives


def test_no_atr_leaves_stop_untouched():
    stop = update_trailing_stop(entry_price=100, high_water=150, stop_live=97,
                                atr_prev=float("nan"), params=P)
    assert stop == 97


# --------------------------------------------------------------------- exits


def test_gap_through_stop_fills_at_open():
    signal = exit_signal(_view(), _bar(o=85, h=86, lo=84, c=85, prev=100), P)
    assert signal == (85.0, ExitReason.STOP)


def test_touch_fills_at_stop():
    signal = exit_signal(_view(), _bar(o=100, h=101, lo=88, c=95, prev=100), P)
    price, reason = signal
    assert price == 90.0
    assert reason == ExitReason.STOP


def test_limit_locked_day_cannot_exit():
    floor = 100 * (1 - 0.195)
    signal = exit_signal(_view(), _bar(o=floor, h=floor, lo=floor, c=floor,
                                       prev=100), P)
    assert signal is None


def test_time_stop_suppressed_in_profit():
    view = _view(hw=110, live=95, held=40)
    assert exit_signal(view, _bar(o=110, h=111, lo=109, c=110, prev=110), P) is None


def test_time_stop_fires_when_not_in_profit():
    view = _view(hw=100, live=90, held=40)
    price, reason = exit_signal(view, _bar(o=95, h=96, lo=94, c=94, prev=95), P)
    assert reason == ExitReason.TIME
    assert price == 94.0


def test_classify_profit_lock_and_volatility_collapse():
    assert classify_stop(_view(), 105.0, 2.0, _bar()) == ExitReason.STOP_PROFIT_LOCK
    assert classify_stop(_view(hw=100), 100.0, 0.1, _bar()) == ExitReason.STOP_ATR_COLLAPSED


# --------------------------------------------------------- same-day re-entry


class AlwaysScanner:
    """Emits the same symbol on every session (worst case for re-entry)."""

    def __init__(self, symbol="AAA"):
        self.symbol = symbol

    def scan(self, day):
        return [ScanCandidate(symbol=self.symbol, exchange=Exchange.NSE,
                              isin="INE000000000", name=self.symbol,
                              score=1.0, close=100.0, volume=1000)]


def _bars(prices):
    idx = pd.DatetimeIndex([date(2024, 1, 1) + timedelta(days=i)
                            for i in range(len(prices))], name="date")
    px = np.asarray(prices, dtype=float)
    df = pd.DataFrame({
        "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
        "volume": 1000, "value": px * 1000, "exchange": "NSE",
        "isin": "INE000000000", "name": "AAA",
    }, index=idx)
    df["prev_close"] = df["close"].shift(1)
    return df


def test_engine_blocks_same_day_reentry():
    bars = _bars([100, 100, 80, 80, 80, 80])
    engine = BacktestEngine(scanner=AlwaysScanner(), bars={"AAA": bars},
                            trading_days=list(bars.index),
                            params=StrategyParams(trailing=True),
                            costs=CostModel(brokerage_per_order=0.0))
    trades = engine.run().trades
    exits = {t.exit_date for t in trades if t.exit_date is not None}
    reentries = [t for t in trades if t.entry_date in exits]
    assert not reentries
    # the stop-out happened on day 2, so no fill on that same session
    assert all(str(t.entry_date) != str(bars.index[2]) for t in trades)
