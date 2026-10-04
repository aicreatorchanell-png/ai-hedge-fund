# plan_crypto_v1 result (2026-10-04): no strategy passed

- **Plan:** `252051415d0c2228`, frozen 2026-10-03 and run exactly as written. Validation gates: `2fc90f4ad972f482`, unchanged.
- **Scope:** 504 configurations. The registry holds 640 hash-chained records: 504 at normal cost plus 136 double-cost reruns. It counts 504 trials.
- **Data:** development window only, 2018-07-01 to 2025-08-31. The out-of-sample test windows run from 2020-07 to 2025-06.
- **Holdout:** `active-crypto-final` (2025-09-01 to 2026-08-31) was not read.
- **Result:** none of the 36 strategy-pair lines or 7 combinations passed. Nothing was tuned after seeing results.

## Gates passed, out of 36 lines

| Gate | Lines passed |
|---|---|
| Minimum trades | 36 |
| PBO (overfitting probability) | 35 |
| Max drawdown | 13 |
| Share of positive test periods | 6 |
| Out-of-sample Sharpe | 3 |
| Deflated Sharpe | 0 |
| Bootstrap lower bound > 0 | 0 |

## Best lines out of sample

These are descriptive only; none qualifies.

| Line | Sharpe (annual, out of sample) | Deflated Sharpe | Max drawdown | Sharpe at 2× cost |
|---|---|---|---|---|
| donchian_breakout BNB | 0.67 | 0.055 | 19% | 0.48 |
| ema_trend BTC | 0.67 | 0.058 | 14% | 0.49 |
| volatility_breakout ETH | 0.54 | 0.031 | 32% | 0.21 |

All three fail the deflated Sharpe gate (needs 0.95) and the bootstrap gate: their 95% lower bounds are below zero.

## Clear losers

| Family | Out-of-sample Sharpe | Max drawdown |
|---|---|---|
| bollinger_reversion (5–15 minute signals) | −3.6 to −4.3 | 79–88% |
| opening_range (5 minute) | −2.2 to −4.8 | 53–77% |

These trade often (1,100–1,900 trades out of sample), and fees plus spread dominate.

## Combinations

All fail.

| Combination | Out-of-sample Sharpe |
|---|---|
| Trend families | −0.04 to 0.17 |
| All lines together | −1.76 |
