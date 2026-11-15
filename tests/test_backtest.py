from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from sta.backtest.costs import CostModel
from sta.backtest.engine import BacktestEngine, ExitReason, StrategyParams
from sta.core.models import Exchange, ScanCandidate


class FakeScanner:
    """Emits a fixed candidate on chosen signal days (engine-mechanics tests)."""

    def __init__(self, symbol, signal_day):
        self.symbol = symbol
        self.signal_day = pd.Timestamp(signal_day)

    def scan(self, day):
        if pd.Timestamp(day) == self.signal_day:
            return [ScanCandidate(symbol=self.symbol, exchange=Exchange.NSE,
                                  isin="INE000000000", name=self.symbol,
                                  score=1.0, close=100.0, volume=1000)]
        return []


def _bars(symbol, prices):
    idx = pd.DatetimeIndex([date(2024, 1, 1) + timedelta(days=i)
                            for i in range(len(prices))], name="date")
    px = np.asarray(prices, dtype=float)
    return pd.DataFrame({
        "symbol": symbol, "exchange": "NSE", "isin": "INE000000000",
        "open": px, "high": px * 1.02, "low": px * 0.98, "close": px,
        "prev_close": np.roll(px, 1), "volume": 1000, "value": px * 1000,
        "series": "EQ", "name": symbol,
    }, index=idx).pipe(lambda f: f.__setitem__(("prev_close"), f["close"].shift(1)) or f)


def _run(bars, signal_day, params=None):
    engine = BacktestEngine(
        scanner=FakeScanner("AAA", signal_day),
        bars={"AAA": bars},
        trading_days=list(bars.index),
        params=params or StrategyParams(max_positions=4),
        costs=CostModel(brokerage_per_order=0.0),
    )
    return engine.run()


def test_target_exit_after_entry_next_open():
    prices = [100.0] * 12 + [100 + 5 * i for i in range(10)]  # rises 5/day after signal
    bars = _bars("AAA", prices)
    result = _run(bars, signal_day=bars.index[10])
    closed = [t for t in result.trades if t.exit_date is not None]
    assert len(closed) == 1
    trade = closed[0]
    assert trade.entry_date == bars.index[11]       # next day after signal
    assert trade.entry_price == 100.0
    assert trade.exit_reason == ExitReason.TARGET
    assert trade.exit_price == pytest.approx(130.0)
    assert trade.pnl > 0


def test_stop_gap_through_fills_at_open():
    # enter day 1 (signal day 0), then open gaps far below stop
    prices = [100.0, 100.0, 60.0, 60.0, 60.0]
    bars = _bars("AAA", prices)
    result = _run(bars, signal_day=bars.index[0])
    closed = [t for t in result.trades if t.exit_date is not None]
    assert closed[0].exit_reason == ExitReason.STOP
    assert closed[0].exit_price == pytest.approx(60.0)  # open, not the 90 stop


def test_limit_down_freeze_blocks_exit():
    # enter day 1; day 2 locked at -20%; day 3 locked again at its own floor;
    # day 4 reopens below the stop -> exit
    floor2 = 100.0 * (1 - 0.195)
    floor3 = floor2 * (1 - 0.195)
    prices = [100.0, 100.0, floor2, floor3, 79.0]
    bars = _bars("AAA", prices)
    for idx in (2, 3):  # close == low == circuit floor on locked days
        bars.loc[bars.index[idx], "low"] = bars.loc[bars.index[idx], "close"]
    result = _run(bars, signal_day=bars.index[0])
    closed = [t for t in result.trades if t.exit_date is not None]
    assert closed[0].exit_date == bars.index[4]  # not on the frozen days
    assert closed[0].exit_reason == ExitReason.STOP


def test_costs_applied_to_pnl():
    prices = [100.0, 100.0, 140.0, 140.0, 140.0]
    bars = _bars("AAA", prices)
    result = _run(bars, signal_day=bars.index[0])
    closed = [t for t in result.trades if t.exit_date is not None]
    trade = closed[0]
    assert trade.entry_cost > 0
    assert trade.exit_cost > 0
    assert trade.return_pct < (140 / 100 - 1)  # net of costs


def test_max_positions_gate():
    prices = [100.0, 100.0, 140.0, 140.0, 140.0]
    bars = _bars("AAA", prices)
    engine = BacktestEngine(
        scanner=FakeScanner("AAA", bars.index[0]),
        bars={"AAA": bars},
        trading_days=list(bars.index),
        params=StrategyParams(max_positions=1, position_size=15_000),
        costs=CostModel(brokerage_per_order=0.0),
    )
    result = engine.run()
    open_trades = [t for t in result.trades if t.exit_date is None]
    assert len(open_trades) <= 1
