from datetime import date, timedelta

import numpy as np
import pandas as pd

from sta.backtest.costs import CostModel
from sta.backtest.engine import BacktestEngine, ExitReason, StrategyParams
from sta.core.models import Exchange, ScanCandidate


class FakeScanner:
    def __init__(self, symbol, signal_day):
        self.symbol = symbol
        self.signal_day = pd.Timestamp(signal_day)

    def scan(self, day):
        if pd.Timestamp(day) == self.signal_day:
            return [ScanCandidate(symbol=self.symbol, exchange=Exchange.NSE,
                                  isin="INE000000000", name=self.symbol,
                                  score=1.0, close=100.0, volume=1000)]
        return []


def _bars(symbol, prices, spread=0.01):
    idx = pd.DatetimeIndex([date(2024, 1, 1) + timedelta(days=i)
                            for i in range(len(prices))], name="date")
    px = np.asarray(prices, dtype=float)
    df = pd.DataFrame({
        "symbol": symbol, "exchange": "NSE", "isin": "INE000000000",
        "open": px, "high": px * (1 + spread), "low": px * (1 - spread),
        "close": px, "volume": 1000, "value": px * 1000,
        "series": "EQ", "name": symbol,
    }, index=idx)
    df["prev_close"] = df["close"].shift(1)
    return df


def _run(bars, signal_day, params):
    engine = BacktestEngine(
        scanner=FakeScanner("AAA", signal_day),
        bars={"AAA": bars},
        trading_days=list(bars.index),
        params=params,
        costs=CostModel(brokerage_per_order=0.0),
    )
    return engine.run()


def test_profit_floor_locks_five_percent():
    # signal day 0 -> entry day 1 at 100; rally to +13% then reverse
    prices = [100.0, 100.0, 105.0, 110.0, 112.0, 113.0, 104.0, 101.0]
    bars = _bars("AAA", prices)
    result = _run(bars, bars.index[0], StrategyParams(trailing=True))
    closed = [t for t in result.trades if t.exit_date is not None]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.exit_reason == ExitReason.STOP_PROFIT_LOCK
    assert trade.exit_price >= 100.0 * 1.02


def test_winners_run_past_time_stop():
    # steady +0.4%/day for 60 bars: never hits the stop, stays in profit
    prices = [100.0 * (1.004 ** i) for i in range(60)]
    bars = _bars("AAA", prices)
    result = _run(bars, bars.index[0],
                  StrategyParams(trailing=True, max_hold_days=40))
    trade = result.trades[0]
    # in profit at the 40-day mark -> time stop suppressed -> never exits
    assert trade.exit_date is None


def test_no_same_day_lookahead_on_spike():
    # day 2 spikes to 150 intraday then closes at 100. If the engine used the
    # same-day high to set the stop it would exit same day at ~+35%.
    prices = [100.0, 100.0, 100.0, 100.0, 100.0]
    bars = _bars("AAA", prices)
    bars.loc[bars.index[2], "high"] = 150.0
    result = _run(bars, bars.index[0], StrategyParams(trailing=True))
    trade = result.trades[0]
    assert trade.exit_date is None or pd.Timestamp(trade.exit_date) != bars.index[2]


def test_time_exit_when_not_in_profit():
    # slow decline inside the hard stop: no trailing stop in this config,
    # closes below entry -> TIME at day 40
    prices = [100.0 - 0.2 * i for i in range(50)]
    bars = _bars("AAA", prices)
    result = _run(bars, bars.index[0],
                  StrategyParams(trailing=False, max_hold_days=40))
    closed = [t for t in result.trades if t.exit_date is not None]
    assert closed and closed[0].exit_reason == ExitReason.TIME
    assert closed[0].days_held == 40


def test_initial_stop_reason_still_stop():
    prices = [100.0, 100.0, 85.0, 80.0]
    bars = _bars("AAA", prices)
    result = _run(bars, bars.index[0], StrategyParams(trailing=False))
    closed = [t for t in result.trades if t.exit_date is not None]
    assert closed[0].exit_reason == ExitReason.STOP
