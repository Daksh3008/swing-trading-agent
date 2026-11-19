"""Single source of truth for stop and exit rules.

The backtest engine (``sta.backtest.engine``) and the live tracker
(``sta.live.tracker``) both import from this module, so a rule can never drift
between research and paper trading. A parity test replays identical histories
through both paths and fails on any difference.

Everything here is a pure function of (position state, day bar, parameters).
No I/O, no database, no clock.

Execution model (conservative, no look-ahead):

- the stop for day D is derived from the high-water mark through D-1 and ATR
  through D-1, then tested against D's open and low
- if the open gaps below the stop, the fill is the OPEN; otherwise the fill is
  the stop price when the low touches it
- a lower-circuit-locked day cannot be exited (micro-cap freezes)
- the time stop fires at ``max_hold_days`` only when the position is NOT in
  profit (winning horses run)
- the trailing stop (chandelier) is ``prev_high_water - mult * prev_ATR``,
  ratcheted monotonically; once max profit exceeds ``profit_lock_at`` (10%)
  the stop is floored at ``profit_floor`` (5%) until max profit exceeds
  ``profit_lock_release`` (14%), after which the chandelier governs
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class ExitReason(str, Enum):
    STOP = "stop_initial"                  # initial -stop_pct stop
    STOP_TRAILING = "stop_trailing"        # chandelier ratchet
    STOP_PROFIT_LOCK = "stop_profit_lock"  # +5% floor after a >10% run
    STOP_FAILED_BREAKOUT = "stop_failed_breakout"  # gave back a >10% run
    STOP_ATR_COLLAPSED = "stop_atr_collapsed"      # volatility compression
    TARGET = "target"
    TIME = "time"
    TREND = "trend"
    END = "end"

    @property
    def is_stop(self) -> bool:
        return self.value.startswith("stop_")


@dataclass(frozen=True)
class RuleParams:
    """The behavioral knobs shared by the engine and the tracker."""

    position_size: float = 15_000.0
    max_positions: int = 4
    stop_pct: float = 0.10
    target_pct: float = 0.30
    max_hold_days: int = 40            # trading days (~2 months)
    limit_band: float = 0.195          # circuit freeze detection band
    trailing: bool = True
    trailing_atr_mult: float = 3.0
    atr_window: int = 22
    profit_lock_at: float = 0.10
    profit_floor: float = 0.05
    profit_lock_release: float = 0.14
    let_winners_run: bool = True
    ma_exit_window: int | None = None


@dataclass(frozen=True)
class PositionView:
    """Immutable snapshot of one open position at the start of a session."""

    entry_price: float
    stop_initial: float
    stop_live: float
    target: float
    high_water: float
    bars_held: int


# --------------------------------------------------------------------- series


def atr_series(hist: pd.DataFrame, window: int = 22) -> pd.Series:
    """Wilder ATR (RMA-smoothed true range) **shifted one session**.

    The shift is the no-look-ahead guarantee: the stop used to test day D is
    built from data observable before D's bar prints.
    """
    prev = hist["close"].shift(1)
    tr = np.maximum(
        hist["high"] - hist["low"],
        np.maximum((hist["high"] - prev).abs(), (hist["low"] - prev).abs()))
    return tr.ewm(alpha=1.0 / window, adjust=False).mean().shift(1)


def initial_stop(entry_price: float, stop_pct: float = 0.10) -> float:
    return float(entry_price) * (1.0 - stop_pct)


def initial_target(entry_price: float, target_pct: float = 0.30) -> float:
    return float(entry_price) * (1.0 + target_pct)


def shares_for(position_size: float, price: float) -> int:
    """Whole shares (the engine floors to an integer position)."""
    if not np.isfinite(price) or price <= 0:
        return 0
    return int(position_size // price)


# ---------------------------------------------------------------------- stops


def update_trailing_stop(*, entry_price: float, high_water: float,
                         stop_live: float, atr_prev: float,
                         params: RuleParams) -> float:
    """Ratchet the stop forward using only pre-session information.

    Monotonic: the returned stop is never below ``stop_live``.
    """
    if not params.trailing:
        return stop_live
    candidate = -np.inf
    if np.isfinite(atr_prev):
        candidate = high_water - params.trailing_atr_mult * atr_prev
    max_profit = (high_water / entry_price - 1.0) if entry_price > 0 else 0.0
    if max_profit > params.profit_lock_at:
        if max_profit > params.profit_lock_release:
            pass  # chandelier governs above the release level
        else:
            candidate = max(candidate, entry_price * (1.0 + params.profit_floor))
    return float(max(stop_live, candidate))


def is_limit_locked(*, prev_close: float, low: float, close: float,
                    limit_band: float = 0.195) -> bool:
    """True when the bar is pinned at the lower circuit (no exit possible)."""
    if not np.isfinite(prev_close) or prev_close <= 0:
        return False
    floor = prev_close * (1.0 - limit_band)
    return bool(low <= floor * 1.01 and abs(close - floor) < 0.01 * prev_close)


def classify_stop(pos: PositionView, exit_price: float, atr_prev: float,
                  bar: Mapping) -> ExitReason:
    """Name a stop fill by *why* it happened (attribution, not behavior)."""
    entry = pos.entry_price
    if exit_price >= entry * 1.02:
        return ExitReason.STOP_PROFIT_LOCK
    if pos.high_water >= entry * 1.10:
        return ExitReason.STOP_FAILED_BREAKOUT
    high, low, close = (_f(bar, "high"), _f(bar, "low"), _f(bar, "close"))
    rng = (high - low) / max(close, 1e-9)
    if ((np.isfinite(atr_prev) and entry > 0 and atr_prev / entry < 0.005)
            or (np.isfinite(rng) and rng < 0.002)):
        return ExitReason.STOP_ATR_COLLAPSED
    if pos.stop_live <= entry * (1.0 - 0.10) * 1.001:
        return ExitReason.STOP
    return ExitReason.STOP_TRAILING


# ---------------------------------------------------------------------- exits


def exit_signal(pos: PositionView, bar: Mapping, params: RuleParams,
                *, atr_prev: float = float("nan"),
                ma_value: float = float("nan")) -> tuple[float, ExitReason] | None:
    """Return ``(fill_price, reason)`` when the session forces an exit.

    ``pos`` must already carry today's ratcheted ``stop_live``; ``bar`` is
    today's OHLC (missing ``prev_close`` simply disables the circuit check).
    """
    stop = pos.stop_live if pos.stop_live else pos.stop_initial
    o, h, low = _f(bar, "open"), _f(bar, "high"), _f(bar, "low")
    prev_close = _f(bar, "prev_close")
    close = _f(bar, "close")

    if not is_limit_locked(prev_close=prev_close, low=low, close=close,
                           limit_band=params.limit_band):
        if o <= stop:                       # gap through the stop
            return float(o), classify_stop(pos, float(o), atr_prev, bar)
        if low <= stop:
            return float(stop), classify_stop(pos, float(stop), atr_prev, bar)
        if params.ma_exit_window and np.isfinite(ma_value) and close < ma_value:
            return float(close), ExitReason.TREND
        if not params.trailing and h >= pos.target:
            # gap up above the target, or an intraday touch
            return float(max(o, min(pos.target, h))), ExitReason.TARGET

    if pos.bars_held >= params.max_hold_days:
        if params.let_winners_run and close > pos.entry_price:
            return None
        return float(close), ExitReason.TIME
    return None


# --------------------------------------------------------------------- helpers


def _f(bar: Mapping, key: str) -> float:
    """Float-field access that tolerates Series, dicts and missing keys."""
    try:
        value = bar[key]
    except (KeyError, IndexError, TypeError):
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def pnl(entry_price: float, exit_price: float, shares: int,
        entry_cost: float, exit_cost: float) -> float:
    """Net rupee P&L for a closed trade."""
    return (exit_price - entry_price) * shares - entry_cost - exit_cost
