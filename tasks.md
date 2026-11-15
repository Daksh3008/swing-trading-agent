# STA — Task List (data pipeline rebuild)

## Context
- Old bhavcopy pipeline deleted; new yfinance per-ticker parquet pipeline in place.
- Yahoo purged ~2,400 live BSE tickers from quoteSummary (metadata) but still
  serves their history on the chart endpoint (verified: 360ONE, 5PAISA, 7NR
  current to 2026-11-13). Current universe (1,996) is undercounted.
- Yahoo rate-limited as of 15:55 — cooldown observed before Phase A.

## Phase A — Complete the universe (mcap <= Rs 50,000 cr)
- [x] Test `v7/finance/quote` batch endpoint — RETIRED by Yahoo (401), unusable
- [x] Confirmed `fast_info.market_cap` (shares x last price) works for the
      "purged" tickers (360ONE Rs 50,436 cr, 5PAISA Rs 1,658 cr, 20MICRONS Rs 648 cr)
- [x] Switched market-cap path to fast_info-first; dead detection via
      `currentTradingPeriod`/`delisted`/404
- [x] Full rescan at 6+4 workers -> 4,331 ok, 85 dead, 304 transient
- [x] Universe rebuilt: 4,133 symbols (3,572 micro / 451 small / 110 mid)
- [x] Sanity checks pass (ABFRL/ASTRAZEN/5PAISA/20MICRONS in; 360ONE at
      Rs 50,436 cr correctly out; RELIANCE/TCS/HDFCBANK out)
- [ ] Optional: one more scan pass later to resolve the 304 transient stragglers
- [ ] Rebuild `membership.parquet`; verify ~4,200+ symbols; sanity-check
      (ABFRL/ASTRAZEN in, RELIANCE/TCS out; 360ONE/5PAISA in)
- [ ] Mark truly-dead tickers (`MIRCELECTR.BO` class) as `dead` in cache

## Phase B — Download max history (parquet)
- [x] `python -m sta download` for full universe — chart endpoint, resumable
- [x] 4,133 symbols downloaded; stub-guard (<30 rows) + retry passes working
- [x] Discovery: Yahoo's BSE daily feed is broken for ~300 symbols (returns
      only the latest bar or monthly aggregates). Added NSE-listing fallback
      to `fetch.py` (TICKER.BO -> TICKER.NS, same company), substitutions
      logged to `data/universe/ns_fallbacks.csv`.
- [x] Fixed stub-cache bug: download_ticker now re-fetches stub caches itself
- [x] Final repair pass for the ~300 BSE-broken symbols -> 303 fixed via NSE
- [x] Verified: 4,133 files, 16.99M rows, 1991 -> 2026-11-13; median 4,657
      rows/symbol; 7 stubs left (0.17%: CIGNITITEC, GSPL, HINDMOTORS,
      JBCHEPHARM, SANGHIIND, SNLFPP - will retry on next download run)
- [x] Spot-checks vs reference CSVs pass (ABFRL, ASTRAZEN, 5PAISA, 7NR,
      ZUARI, SUMMITSEC, ACC, 20MICRONS)
- [x] 303 NSE substitutions recorded in `data/universe/ns_fallbacks.csv`

## Phase C — Strategy experiments (walk-forward, net of costs)
- [x] Engine v2 (2026-11-14): richer exit reasons (`stop_initial`,
      `stop_trailing`, `stop_profit_lock`, `stop_failed_breakout`,
      `stop_atr_collapsed`, `time`, `target`, `trend`), winners-run time
      stop (only exits at 40d when NOT in profit), profit protection
      (>=10% max profit -> stop floored at +5%; >14% -> chandelier governs,
      monotonic), strict no-lookahead (stop from prior-day high-water and
      prior-day ATR; high-water updated after the exit check).
      Trade log now carries stop_at_exit / high_water / atr_at_exit.
- [ ] Rerun walk-forward with engine v2: data/backtest_v2_full (2005-2026)
      and data/backtest_v2_2015 (2015-2026) — running
- [ ] Export v2 CSVs to data/analysis/v2_full_history and v2_clean_2015
- [x] Add features: `slope_50`, `slope_150` (pct over 20d), `accel =
      slope_50 - slope_150` (computed, not gated) — tests pass
- [x] Variant A: KAMA trend gate (KAMA 10/30, same shape as SMA gate) —
      `--variant kama`
- [x] Variant B: chandelier trailing stop (3xATR(22), RMA/EWM-smoothed)
      replacing fixed +30% target — `--variant trailing` (optionally `_kama`)
- [x] Variant C: MA20 trend exit replacing time stop — `--variant trend_exit`
- [x] Adapted runner/scanner/store to yfinance schema (spans synthesized from
      history coverage; lowercase bars; derived value/prev_close)
- [x] Fixed two engine bugs found by validation: tz shift (18:30 index vs
      midnight lookups) and ATR/MA series looked up with the composite
      position key instead of the plain symbol (trailing stop + MA exit
      were silently disabled)
- [x] 400-symbol, 2020-2026 results (net of costs, after bug fixes):
      baseline: Sharpe 2.78 / CAGR 33.9% / DD -15.4% / exp +5.9%
      kama gate: 1.28 / 19.8% / -25.6% / +2.0%   (worse)
      trailing 3xATR22: 4.78 / 53.7% / -8.9% / +14.7%   (WINNER)
      trend exit MA20: 3.32 / 40.4% / -12.5% / +6.0%
      trailing+kama: 3.80 / 41.3% / -10.1% / +6.8%
      -> KAMA gate consistently hurts; ATR trailing stop is the real edge
- [ ] Run baseline + trailing on the full 4,133-symbol universe
      (memory risk: scanner builds all features in memory; chunk if OOM)
- [x] Implemented RMA (Wilder) gate: `rma()` + `rma_{n}`/`rma_{n}_up`
      columns for the full sweep (15..150), configurable scanner gate
      (`trend_mode="rma"`, `rma_fast`/`rma_slow`), CLI `--variant rma`
      and `--variant trailing_rma --rma-fast --rma-slow`; 3 new tests
- [x] Full-universe (4,133 symbols) confirmation, 2020-2026, net of costs:
      baseline Sharpe 2.03 / CAGR 31.0% / DD -21.6% / exp +4.5%
      trailing 3xATR22 Sharpe 4.90 / CAGR 59.5% / DD -13.0% / exp +15.9%
      -> trailing edge confirmed and widened vs the 400-symbol sample
- [ ] Stage 1: RMA gate alone — configs (25,75), (50,150), (20,60)
- [x] Stage 1: RMA gate alone (400 sym, 2020-26) — (20,60) Sharpe 2.64;
      (25,75) 2.77; (50,150) 2.88 vs SMA baseline 2.78; KAMA 1.28.
      Heavier smoothing wins; sweep upward from 50/150.
- [ ] Stage 2: RMA finetune grid fast 40/50/60 x slow 120/150/180 (running)
- [ ] Split-sample validate top-2 (2020-22 select -> 2023-26 check)
- [x] Stage 2: RMA finetune grid (400 sym, 2020-26) — longer fast RMA wins:
      60/150 Sharpe 3.25 (best), 60/120 3.11, 50/180 3.10; all above
      baseline 2.78. Top-2 = (60,150) and (60,120).
- [x] Stage 3 (400 sym, full period): trailing+RMA(60,120) Sharpe 5.13 /
      CAGR 53.6% / DD -8.4% vs trailing-only 4.78 / 53.7% / -8.9%;
      trailing+RMA(60,150) 4.35 (worse). Inconsistent winners -> caution.
- [x] Split-sample RMA standalone: (60,150) 3.80 -> 2.53; (60,120) 3.53 -> 2.56
- [x] Split-sample decision: trailing-only 2020-22 Sharpe 5.68 / 2023-26 6.06;
      trailing+RMA(60,120) 6.36 / 5.09. RMA wins in-sample, LOSES OOS ->
      REJECTED. Conclusion: exits carry the edge; gate flavor (SMA/KAMA/RMA)
      does not matter durably.
- [x] CLI default variant set to `trailing` (full-universe validated: Sharpe
      4.90, CAGR 59.5%, DD -13.0% vs baseline 2.03/31.0%/-21.6%)
- [x] Full-history walk-forward (2005-2026, 4,133 symbols, trailing): 1,966
      trades, PF 2.59, exp +8.4%, but 2005-2014 contaminated by bad Yahoo
      BSE bars (11,104 suspicious 1-day crashes >35%, 7,898 of them in
      2007-08; max DD -63.7% was a single-day artifact 2005-07-28/29)
- [ ] Clean-era walk-forward 2015-2026 (running) -> publish yearly table
- [ ] Candidate hardening: drop bars with |1d ret|>35% lacking a split
      factor before backtesting, or use Adj_Close split factors
- [ ] Full-universe confirmation of the overall winner
- [ ] Consider making trailing the CLI default after full-universe validation
- [ ] KAMA loop is slow (~1.5 min/120 symbols) — optimize if full-universe
      runs are too slow (numba or reduced gate usage)
- [ ] Note survivorship-bias caveat in every published result
