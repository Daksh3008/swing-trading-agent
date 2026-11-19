"""Per-symbol feature computation (vectorized, cacheable).

One function per family of momentum features; all operate on a single
symbol's history (date-indexed DataFrame) and return a date-indexed
frame of the same length. Cross-sectional ranking happens in the
scanner layer, not here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_TREND_WINDOW_LONG = 150
_TREND_WINDOW_SHORT = 50
_SLOPE_WINDOW = 20       # lookback for MA slope / acceleration estimates
_RET_WINDOW = 63          # ~3 months
_HIGH_WINDOW = 252        # 52 trading weeks
_VOL_WINDOW = 20


def kama(close: pd.Series, length: int = 10, fast: int = 2,
         slow: int = 30) -> pd.Series:
    """Kaufman Adaptive Moving Average (Wilder-style adaptive smoothing).

    Smoothing constant adapts to the efficiency ratio: flat in chop (fewer
    false trend gates), fast in clean trends (earlier gate).
    """
    import numpy as np

    vals = close.to_numpy(dtype=float)
    n = len(vals)
    out = np.full(n, np.nan)
    if n <= length:
        return pd.Series(out, index=close.index)
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    prev = vals[length]
    out[length] = prev
    for i in range(length + 1, n):
        change = abs(vals[i] - vals[i - length])
        vol = np.abs(np.diff(vals[i - length:i + 1])).sum()
        er = change / vol if vol > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = prev + sc * (vals[i] - prev)
        out[i] = prev
    return pd.Series(out, index=close.index)


def rma(close: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average: EMA with alpha = 1/length.

    Heavier smoothing than SMA of the same nominal period
    (RMA(n) ~ SMA(2n-1) in responsiveness).
    """
    return close.ewm(alpha=1.0 / length, adjust=False).mean()


RMA_LENGTHS = (15, 20, 25, 30, 40, 50, 60, 75, 90, 120, 150, 180, 200)


def trend_features(hist: pd.DataFrame, *, include_alt_gates: bool = True) -> pd.DataFrame:
    """MA structure: price above rising short MA, which is above rising long MA.

    Also exposes MA slope rates and their difference (acceleration):
    ``slope_50``, ``slope_150`` are percentage changes of the MAs over
    ``_SLOPE_WINDOW`` days (comparable across price levels); ``accel`` is
    short-trend rate minus long-trend rate (steepening vs coasting).
    """
    short_ma = hist["close"].rolling(_TREND_WINDOW_SHORT).mean()
    long_ma = hist["close"].rolling(_TREND_WINDOW_LONG).mean()
    short_up = short_ma > short_ma.shift(_VOL_WINDOW)
    long_up = long_ma > long_ma.shift(_VOL_WINDOW)
    slope_50 = short_ma / short_ma.shift(_SLOPE_WINDOW) - 1.0
    slope_150 = long_ma / long_ma.shift(_SLOPE_WINDOW) - 1.0
    out = pd.DataFrame({
        "short_ma": short_ma,
        "long_ma": long_ma,
        "slope_50": slope_50,
        "slope_150": slope_150,
        "accel": slope_50 - slope_150,
        "trend_ok": (hist["close"] > short_ma) & short_up & (short_ma > long_ma) & long_up,
    })
    if not include_alt_gates:
        return out
    # KAMA-based trend gate (Variant A) — same shape as the SMA gate
    kama_fast = kama(hist["close"], length=10, fast=2, slow=30)
    kama_slow = kama(hist["close"], length=30, fast=2, slow=30)
    out["trend_ok_kama"] = ((hist["close"] > kama_fast)
                            & (kama_fast > kama_fast.shift(_SLOPE_WINDOW))
                            & (kama_fast > kama_slow)
                            & (kama_slow > kama_slow.shift(_SLOPE_WINDOW)))
    for n in RMA_LENGTHS:
        series = rma(hist["close"], n)
        out[f"rma_{n}"] = series
        out[f"rma_{n}_up"] = series > series.shift(_SLOPE_WINDOW)
    return out


def momentum_features(hist: pd.DataFrame) -> pd.DataFrame:
    """52-week-high proximity, 3-month return, volume surge, turnover."""
    high_52w = hist["high"].rolling(_HIGH_WINDOW, min_periods=_HIGH_WINDOW // 2).max()
    vol_sma = hist["volume"].rolling(_VOL_WINDOW).mean()
    turnover = hist["value"].rolling(_VOL_WINDOW).mean()
    return pd.DataFrame({
        "ret_3m": hist["close"].pct_change(_RET_WINDOW),
        "pct_from_high": hist["close"] / high_52w - 1.0,
        "new_high": hist["close"] >= high_52w * 0.999,
        "vol_ratio": hist["volume"] / vol_sma,
        "turnover_20d": turnover,
    })


def compute_features(hist: pd.DataFrame, *, include_alt_gates: bool = True) -> pd.DataFrame:
    """All per-symbol features joined on the symbol's own date index.

    ``include_alt_gates=False`` skips the KAMA/RMA columns (they are only used
    by the ``kama``/``rma`` scanner variants). A one-day live scan uses this:
    the exponential averages depend on full history and are useless for a
    single-day readout, and they dominate the compute cost.
    """
    if hist.empty:
        return pd.DataFrame()
    feats = trend_features(hist, include_alt_gates=include_alt_gates).join(
        momentum_features(hist), how="outer")
    feats["exchange"] = hist["exchange"].iloc[0]
    feats["isin"] = hist["isin"].iloc[0]
    return feats


def universe_membership_mask(hist_index: pd.DatetimeIndex,
                             spans: pd.DataFrame,
                             symbol: str, exchange: str) -> np.ndarray:
    """True where the symbol was inside the tradeable universe."""
    mask = np.zeros(len(hist_index), dtype=bool)
    if spans.empty:
        return mask
    rows = spans[(spans["symbol"] == symbol) & (spans["exchange"] == exchange)]
    dates = np.array([t.date() if hasattr(t, "date") else t for t in hist_index])
    for span in rows.itertuples(index=False):
        mask |= (dates >= span.start) & (dates <= span.end)
    return mask
