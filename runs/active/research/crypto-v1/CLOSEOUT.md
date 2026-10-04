# plan_crypto_v1 closeout (2026-10-04)

The results were read from the stored cache only, with no rerun. A recomputation reproduces `summary.json` exactly. Files are unchanged; see `MANIFEST.json`.

## 504 configurations, in sample (development 2018-07 to 2025-08, after costs)

- **Positive total return:** 67 of 504.
- **Total return:** median −66.5%, best +256% (selection-biased: the best of 504 in sample), worst −91%.
- **Max drawdown:** median 70% (range 13–91%).

| Family | Positive configurations |
|---|---|
| Bollinger reversion | 0 of 96 |
| Opening range | 0 of 48 |
| Donchian breakout | 23 of 96 |
| Volatility breakout | 20 of 96 |
| EMA trend | 13 of 96 |
| Time-series momentum | 11 of 72 |

## Out of sample (walk-forward, test windows 2020-07 to 2025-06)

- **Passed all gates:** 0 of 36 strategy-pair lines and 0 of 7 combinations.
- **Positive out-of-sample return:** 12 of 36.

The best out-of-sample lines (none passes):

| Line | OOS return | Sharpe | Max drawdown | Deflated Sharpe | Failed gates |
|---|---|---|---|---|---|
| volatility_breakout BNB | +75% | 0.46 | 59% | — | — |
| volatility_breakout ETH | +57% | 0.54 | 32% | — | — |
| donchian_breakout BNB | +53% | 0.67 | 19% | 0.06 | deflated Sharpe, bootstrap |
| ema_trend BTC | +34% | 0.67 | 14% | 0.06 | deflated Sharpe, bootstrap |

The worst lines lost about 87–88% (Bollinger reversion on ADA, BTC and XRP).

## Fixes after the experiment (do not change its results)

- **Liquidity:** an entry is capped at 10% of the median volume of the last 60 one-minute bars (`GuardedStrategy`). The audit's liquidity check moves from FAIL (684%) to WARNING (10%).
- **Financing:** margin positions are charged swap/borrow costs per day held.
  - FX: 1.0 bp/day, both directions.
  - Index CFDs: 1.5 bp/day long, 0.5 bp/day short.
  - Spot crypto is cash-only and long-only, so it borrows nothing.
  - The audit fails any margin market without financing costs.
- **Skipped tests:** all 65 are live network tests that need keys. 38 are for the forbidden Financial Datasets and 27 for live Tiingo/SEC. None covers the engine, the holdout, costs or risk.

## Decision

There is no candidate for further evaluation. The sealed holdout stays closed.
