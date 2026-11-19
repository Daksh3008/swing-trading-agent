"""Backtest engine: daily-bar event simulation of the momentum strategy.

Execution model (deliberately conservative, no look-ahead):

- signals computed on day D close -> order placed -> filled at D+1 open
- exits are evaluated with information available *before* the current bar
  finishes: the trailing stop for day D is derived from the highest high
  through D-1 and ATR through D-1, then tested against D's open/low
- stop: if the open is below the stop, fill at the OPEN (gap-through);
  otherwise fill at the stop when the low touches it
- limit-down day (close ~ prev_close x (1 - band)): cannot exit; carry
  to the next tradable day (microcap lower-circuit freezes)
- target: filled at the target price when touched (non-trailing mode only)
- time stop: exit at close after ``max_hold_days`` trading days, but only
  when the position is NOT in profit (winning horses run)
- trailing stop (chandelier): ``prev_high_water - mult * prev_ATR``,
  ratcheted monotonically; once max profit exceeds ``profit_lock_at``
  (10%) the stop is floored at ``profit_floor`` (5%) until max profit
  exceeds ``profit_lock_release`` (14%), after which the chandelier
  governs (the monotonic ratchet keeps the locked gain)
- costs applied per fill via CostModel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as Date

import numpy as np
import pandas as pd

from ..core.models import ScanCandidate
from ..live.rules import (
    ExitReason,
    PositionView,
    RuleParams,
    atr_series,
    exit_signal,
    initial_stop,
    initial_target,
    shares_for,
    update_trailing_stop,
)
from ..scanner.momentum import Scanner
from .costs import CostModel

log = logging.getLogger(__name__)

__all__ = ["BacktestEngine", "BacktestResult", "ExitReason", "StrategyParams", "Trade"]


@dataclass(frozen=True)
class StrategyParams:
    position_size: float = 15_000.0
    max_positions: int = 4
    stop_pct: float = 0.10
    target_pct: float = 0.30
    max_hold_days: int = 40            # trading days (~2 months)
    limit_band: float = 0.195          # circuit freeze detection band
    start_date: Date | None = None
    end_date: Date | None = None
    # trailing-stop variant
    trailing: bool = False
    trailing_atr_mult: float = 3.0
    atr_window: int = 22
    # profit protection while trailing
    profit_lock_at: float = 0.10       # max profit that activates the floor
    profit_floor: float = 0.05         # minimum locked gain once activated
    profit_lock_release: float = 0.14  # max profit after which chandelier governs
    # winners run: time stop only when the position is not in profit
    let_winners_run: bool = True
    # trend exit (close below rolling MA)
    ma_exit_window: int | None = None

    @property
    def rules(self) -> RuleParams:
        """The same knobs in the shared, I/O-free rules form."""
        return RuleParams(
            position_size=self.position_size,
            max_positions=self.max_positions,
            stop_pct=self.stop_pct,
            target_pct=self.target_pct,
            max_hold_days=self.max_hold_days,
            limit_band=self.limit_band,
            trailing=self.trailing,
            trailing_atr_mult=self.trailing_atr_mult,
            atr_window=self.atr_window,
            profit_lock_at=self.profit_lock_at,
            profit_floor=self.profit_floor,
            profit_lock_release=self.profit_lock_release,
            let_winners_run=self.let_winners_run,
            ma_exit_window=self.ma_exit_window,
        )


@dataclass
class Trade:
    symbol: str
    exchange: str
    entry_date: Date
    entry_price: float
    shares: int
    stop: float
    target: float
    entry_cost: float
    exit_date: Date | None = None
    exit_price: float | None = None
    exit_cost: float = 0.0
    exit_reason: ExitReason | None = None
    days_held: int = 0
    stop_at_exit: float = 0.0
    high_water: float = 0.0
    atr_at_exit: float = 0.0

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        gross = (self.exit_price - self.entry_price) * self.shares
        return gross - self.entry_cost - self.exit_cost

    @property
    def return_pct(self) -> float:
        if self.exit_price is None:
            return 0.0
        return self.pnl / (self.entry_price * self.shares)


@dataclass
class OpenPosition:
    trade: Trade
    bars_held: int = 0
    frozen_days: int = 0
    high_water: float = 0.0
    stop_live: float = 0.0


@dataclass
class BacktestResult:
    nav: pd.Series
    trades: list[Trade]
    daily_exposure: pd.Series

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_date is not None]
        returns = self.nav.pct_change().dropna()
        if len(closed) == 0 or len(returns) < 2 or returns.std() == 0:
            return {"trades": len(closed)}
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        avg_win = np.mean([t.return_pct for t in wins]) if wins else 0.0
        avg_loss = np.mean([t.return_pct for t in losses]) if losses else 0.0
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        cagr_days = max((self.nav.index[-1] - self.nav.index[0]).days, 1)
        sharpe = float(returns.mean() / returns.std() * np.sqrt(252))
        return {
            "trades": len(closed),
            "win_rate": len(wins) / len(closed),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": len(wins) / len(closed) * avg_win
                          + len(losses) / len(closed) * avg_loss,
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "sharpe": sharpe,
            "cagr": float(self.nav.iloc[-1] / self.nav.iloc[0]) ** (365.0 / cagr_days) - 1.0,
            "max_drawdown": float((self.nav / self.nav.cummax() - 1).min()),
            "avg_hold_days": float(np.mean([t.days_held for t in closed])),
            "final_nav": float(self.nav.iloc[-1]),
        }


class BacktestEngine:
    def __init__(self, scanner: Scanner, bars: dict[str, pd.DataFrame],
                 trading_days: list[Date],
                 params: StrategyParams | None = None,
                 costs: CostModel | None = None):
        """``bars`` maps ``SYMBOL`` -> date-indexed history (raw, with
        prev_close) for every symbol the scanner can emit."""
        self.scanner = scanner
        self.bars = bars
        self.days = [pd.Timestamp(d) for d in sorted(trading_days)]
        self.params = params or StrategyParams()
        self.rules = self.params.rules
        self.costs = costs or CostModel()
        self._atr: dict[str, pd.Series] = {}
        self._exit_ma: dict[str, pd.Series] = {}
        self._exited_today: set[str] = set()
        if self.params.trailing:
            for sym, df in bars.items():
                # shift(1): the stop for day D uses ATR through day D-1 only
                self._atr[sym] = atr_series(df, self.params.atr_window)
        if self.params.ma_exit_window:
            for sym, df in bars.items():
                self._exit_ma[sym] = df["close"].rolling(
                    self.params.ma_exit_window).mean()

    def run(self) -> BacktestResult:
        p = self.params
        cash = p.position_size * p.max_positions
        open_positions: dict[str, OpenPosition] = {}
        trades: list[Trade] = []
        nav_dates: list[Date] = []
        nav_values: list[float] = []
        exposure: list[float] = []

        for i, day in enumerate(self.days):
            self._exited_today = set()
            cash, open_positions, trades = self._process_day(
                day, cash, open_positions, trades)

            # entries from yesterday's signals, filled at today's open
            if i > 0:
                yesterday = self.days[i - 1]
                cash, open_positions, trades = self._try_entries(
                    day, yesterday, cash, open_positions, trades)

            mark = self._mark_to_market(day, open_positions)
            nav_dates.append(day)
            nav_values.append(cash + mark)
            exposure.append(mark / max(cash + mark, 1e-9))

        nav = pd.Series(nav_values, index=pd.DatetimeIndex(nav_dates), name="nav")
        return BacktestResult(nav=nav, trades=trades,
                              daily_exposure=pd.Series(exposure, index=nav.index))

    # ------------------------------------------------------------- internals

    def _process_day(self, day, cash, positions, trades):
        for symbol in list(positions):
            pos = positions[symbol]
            hist = self.bars.get(pos.trade.symbol)
            bar = self._bar(hist, day)
            if bar is None:  # suspended/limit-locked: no exit possible
                pos.bars_held += 1
                pos.frozen_days += 1
                continue
            pos.bars_held += 1

            entry = pos.trade.entry_price
            atr_prev = self._series_at(self._atr.get(pos.trade.symbol), day)

            # Ratchet the stop using ONLY information from before today's bar:
            # high-water through yesterday and ATR through yesterday.
            pos.stop_live = update_trailing_stop(
                entry_price=entry, high_water=pos.high_water,
                stop_live=pos.stop_live, atr_prev=atr_prev, params=self.rules)

            view = PositionView(
                entry_price=entry, stop_initial=pos.trade.stop,
                stop_live=pos.stop_live, target=pos.trade.target,
                high_water=pos.high_water, bars_held=pos.bars_held)
            exit = exit_signal(
                view, bar, self.rules, atr_prev=atr_prev,
                ma_value=self._series_at(self._exit_ma.get(pos.trade.symbol), day))
            if exit is not None:
                price, reason = exit
                value = price * pos.trade.shares
                cost = self.costs.sell_cost(value)
                pos.trade.exit_date = day
                pos.trade.exit_price = price
                pos.trade.exit_cost = cost
                pos.trade.exit_reason = reason
                pos.trade.days_held = pos.bars_held
                pos.trade.stop_at_exit = pos.stop_live
                pos.trade.high_water = pos.high_water
                pos.trade.atr_at_exit = atr_prev if np.isfinite(atr_prev) else 0.0
                cash += value - cost
                del positions[symbol]
                self._exited_today.add(_trade_key(pos.trade))
            else:
                # end of session: fold today's high into the high-water mark
                pos.high_water = max(pos.high_water, float(bar["high"]))
        return cash, positions, trades

    @staticmethod
    def _series_at(series: pd.Series | None, day) -> float:
        if series is None:
            return float("nan")
        try:
            return float(series.loc[pd.Timestamp(day)])
        except KeyError:
            return float("nan")

    def _try_entries(self, day, signal_day, cash, positions, trades):
        p = self.params
        if len(positions) >= p.max_positions or cash < p.position_size * 0.98:
            return cash, positions, trades
        for cand in self.scanner.scan(signal_day):
            key = _key(cand)
            if key in positions or key in self._exited_today:
                continue
            if len(positions) >= p.max_positions:
                continue
            hist = self.bars.get(cand.symbol)
            bar = self._bar(hist, day)
            if bar is None or not np.isfinite(bar["open"]):
                continue
            shares = shares_for(p.position_size, float(bar["open"]))
            if shares <= 0:
                continue
            value = shares * bar["open"]
            cost = self.costs.buy_cost(value)
            if value + cost > cash:
                continue
            trade = Trade(
                symbol=cand.symbol, exchange=cand.exchange, entry_date=day,
                entry_price=bar["open"], shares=shares,
                stop=initial_stop(bar["open"], p.stop_pct),
                target=initial_target(bar["open"], p.target_pct),
                entry_cost=cost)
            positions[key] = OpenPosition(
                trade=trade,
                high_water=float(bar["open"]),
                stop_live=trade.stop)
            cash -= value + cost
            trades.append(trade)
        return cash, positions, trades

    def _mark_to_market(self, day, positions):
        mark = 0.0
        for pos in positions.values():
            hist = self.bars.get(pos.trade.symbol)
            bar = self._bar(hist, day)
            if bar is not None:
                mark += bar["close"] * pos.trade.shares
            else:
                mark += pos.trade.entry_price * pos.trade.shares
        return mark

    @staticmethod
    def _bar(hist, day):
        if hist is None:
            return None
        ts = pd.Timestamp(day)
        if ts not in hist.index:
            return None
        return hist.loc[ts]


def _key(cand: ScanCandidate) -> str:
    return f"{cand.exchange}:{cand.symbol}"


def _trade_key(trade: Trade) -> str:
    return f"{trade.exchange}:{trade.symbol}"
