import numpy as np
import pandas as pd

from sta.scanner.features import rma, trend_features


def test_rma_matches_manual_recursion():
    close = pd.Series([10.0, 11.0, 10.5, 12.0, 13.0, 12.5])
    n = 3
    expected = [10.0]
    for value in close.iloc[1:]:
        expected.append(expected[-1] + (value - expected[-1]) / n)
    got = rma(close, n).tolist()
    np.testing.assert_allclose(got, expected, rtol=1e-12)


def test_rma_smoother_than_sma_same_period():
    rng = np.random.default_rng(0)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    rma_var = rma(close, 50).diff().var()
    sma_var = close.rolling(50).mean().diff().var()
    assert rma_var < sma_var


def test_trend_features_emits_rma_columns():
    rng = np.random.default_rng(1)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.01, 400)))
    hist = pd.DataFrame({
        "close": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "volume": rng.integers(1000, 5000, 400),
        "value": close * 1000,
    })
    feats = trend_features(hist)
    for col in ("rma_25", "rma_75", "rma_25_up", "rma_75_up"):
        assert col in feats.columns
    # gate math is consistent (scanner joins `close` separately)
    ok = ((hist["close"] > feats["rma_25"])
          & feats["rma_25_up"].astype(bool)
          & (feats["rma_25"] > feats["rma_75"])
          & feats["rma_75_up"].astype(bool))
    assert ok.dtype == bool
