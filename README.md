# Swing Trading Agent (STA)

Research system for momentum swing trading on Indian mid-cap, small-cap, and
micro-cap equities (NSE + BSE), targeting few-but-large wins: cut losers at
-10%, let winners run to +30%, 1 week to 2 months hold, ₹15k per position,
₹60k book (4 concurrent slots).

**Status: research only — no live trading, no order execution.**

## Commands

```bash
python -m sta universe                     # market-cap scan -> data/universe/membership.parquet
python -m sta download                     # max-history parquet per universe symbol -> data/daily/
python -m sta backtest --start 2019-06-01 --end 2026-10-31
```

Strategy variants (same data, comparable outputs):

```bash
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant baseline
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant kama        # KAMA trend gate
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant trailing    # 3xATR chandelier, no fixed target
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant trailing_kama
python -m sta backtest --start 2020-01-01 --end 2026-10-31 --variant trend_exit  # exit on close < MA20
```

Add `--limit N` for fast smoke tests. Results land in `data/backtest_<variant>/`.

All commands accept `-v` for debug logging and `--config` for an alternate
`settings.toml` (flags may appear before or after the subcommand).

## Layout

```
src/sta/core/       domain models, interfaces (Protocols), config  — no I/O
src/sta/data/       yfinance per-ticker fetcher (parquet cache),
                    market-cap universe builder, store shim
src/sta/scanner/    per-symbol momentum features + cross-sectional scanner
src/sta/backtest/   cost model, event engine (stop/target/circuit rules), runner
src/sta/report/     daily research reports (planned)
```

## Data notes

- Pipeline replicates `updated_stock_finder/src/fetch.py`: yfinance
  `Ticker.history(period="max", interval="1d", auto_adjust=False)` per
  ticker — OHLCV + `Adj_Close`, cached as **parquet** (`data/daily/TICKER.parquet`)
  instead of CSV, resumable (existing files are skipped unless `--force`).
- Universe: `Equity.csv` (4,720 BSE `.BO` tickers, reference project) filtered
  by market cap to micro/small/mid caps ≤ ₹50,000 cr
  (`data/universe/membership.parquet`; raw caps cached in
  `data/universe/market_caps.parquet`). Cap bands: micro < ₹5,000 cr,
  small < ₹25,000 cr, mid ≤ ₹50,000 cr.
- Known Yahoo glitch: some tickers occasionally return stub responses;
  the fetcher retries and keeps the longest response, and stub caches
  (<30 rows) are treated as missing.
- Caveat: yfinance history has survivorship bias — delisted tickers are
  absent. The previous bhavcopy pipeline (deleted 2026-11-14) was the
  anti-survivorship source; see `tasks.md`.

## Backtest realism (v1)

- Signals on day-D close → entries at D+1 open.
- Stops fill at the stop price, or at the open when gapped through.
- Lower-circuit lock days cannot be exited (position freezes).
- Costs: STT 0.1% both sides (delivery), statutory charges, ₹20/order,
  GST — see `backtest/costs.py`.
- Metrics: net Sharpe, CAGR, max drawdown, win rate, expectancy, profit
  factor — reported net of all costs.

## Strategy (validated)

Default variant is **trailing**: the Phase-1 momentum scanner (52w-high
proximity, top-15% 3-month RS, 2x volume surge, rising MA structure) with a
**3xATR(22) chandelier trailing stop** in place of the fixed +30% target
(the 10% hard stop stays until the trailing stop ratchets above it).

Full universe (4,133 symbols), 2020-06 → 2026-10, net of costs:

| Variant | Sharpe | CAGR | Max DD |
|---|---|---|---|
| Baseline (+30% target) | 2.03 | 31.0% | -21.6% |
| Trailing 3xATR22 | **4.90** | **59.5%** | **-13.0%** |

Gate-flavor experiments (KAMA, RMA, RMA finetuned) were rejected: none
survived out-of-sample split validation against trailing-only. Conclusion:
exits carry the edge; MA flavor does not.

Caveat: yfinance history has survivorship bias and fills are idealized
(stops at the stop price, target at the target), so treat absolute Sharpe
as optimistic. Relative ordering has been stable across every slice tested.

## Testing

```bash
python -m pytest tests/ -q
```

8 unit tests cover the momentum scanner and the backtest engine (stops,
targets, circuit freezes, costs). Tests for the deleted bhavcopy pipeline
were removed with it.
