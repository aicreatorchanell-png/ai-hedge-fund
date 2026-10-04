# funding-crowding-v1: FAIL

Plan hash `393bd1834e34890a`. Hypothesis: H-FUNDING-CROWDING. Development window 2020-01-01 to 2025-08-31; the sealed holdout from 2025-09-01 was not read. Cumulative trial count used for the deflated Sharpe: 552. Gate config hash `2fc90f4ad972f482`.

Decision rule (pre-registered): PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, with the deflated Sharpe computed on the cumulative trial count of every registry under runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.

## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)

| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT-PERP | 52 | 0.54 | 0.01 | 0.43 | +3.8% | +57.1% | deflated_sharpe, walk_forward, bootstrap | fail |
| ETHUSDT-PERP | 169 | 0.39 | 0.01 | 0.01 | +5.2% | +71.4% | oos_sharpe, deflated_sharpe, bootstrap | fail |
| XRPUSDT-PERP | 47 | 0.13 | 0.00 | 0.76 | +3.1% | +28.6% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| LTCUSDT-PERP | 58 | 0.39 | 0.01 | 0.24 | +4.3% | +57.1% | oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |
| ADAUSDT-PERP | 160 | 0.51 | 0.01 | 0.41 | +6.4% | +85.7% | deflated_sharpe, bootstrap | fail |
| BNBUSDT-PERP | 48 | -0.38 | 0.00 | 0.83 | +3.4% | +28.6% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |

## Combinations

| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|
| family:funding_crowding | 534 | 0.59 | 0.02 | +1.7% | deflated_sharpe, bootstrap | fail |
| all | 534 | 0.59 | 0.02 | +1.7% | deflated_sharpe, bootstrap | fail |

## Diagnostics (descriptive; they cannot change the verdict)

| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |
|---|---|---|---|---|---|---|
| BTCUSDT-PERP | +4.8% | 2024-01-01 (-1.4%) | no | 3/1/0 of 7 | 0.97 | +100.0% |
| ETHUSDT-PERP | +5.5% | 2022-01-01 (-1.4%) | no | 7/7/0 of 7 | 0.52 | +100.0% |
| XRPUSDT-PERP | +0.8% | 2024-07-01 (-1.3%) | yes | 6/5/0 of 7 | -0.82 | +20.0% |
| LTCUSDT-PERP | +3.7% | 2023-07-01 (-1.8%) | no | 3/3/0 of 7 | -0.06 | +26.7% |
| ADAUSDT-PERP | +6.8% | 2022-07-01 (-3.2%) | yes | 4/4/0 of 7 | 0.51 | +86.7% |
| BNBUSDT-PERP | -2.3% | 2023-01-01 (-2.0%) | no | 4/3/0 of 7 | 0.27 | +60.0% |

Family combination: OOS return +3.3%, Sharpe 0.59, max drawdown +1.7%; worst fold 2022-07-01; effective independent sources 5.2 of 6.

## Hypothesis predictions (development data, descriptive)

```
{
 "BTCUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.0018718810481248795,
   "after_extreme_positive_mean": 0.004316020439288873,
   "n_positive_events": 125,
   "after_extreme_negative_mean": 0.0021443353046874205,
   "n_negative_events": 118
  },
  "3d": {
   "unconditional_mean": 0.005528532141058175,
   "after_extreme_positive_mean": 0.011059383658461713,
   "n_positive_events": 125,
   "after_extreme_negative_mean": 0.012378799407545134,
   "n_negative_events": 118
  }
 },
 "ETHUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.002687023554266229,
   "after_extreme_positive_mean": -0.0001239234600515121,
   "n_positive_events": 105,
   "after_extreme_negative_mean": 0.003305726790108871,
   "n_negative_events": 341
  },
  "3d": {
   "unconditional_mean": 0.007879920426818645,
   "after_extreme_positive_mean": 0.01655632240804656,
   "n_positive_events": 105,
   "after_extreme_negative_mean": 0.0025642542651660993,
   "n_negative_events": 341
  }
 },
 "XRPUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.0027780944945901267,
   "after_extreme_positive_mean": 0.01437025329011624,
   "n_positive_events": 115,
   "after_extreme_negative_mean": -0.0007785307129515571,
   "n_negative_events": 399
  },
  "3d": {
   "unconditional_mean": 0.008190242933086785,
   "after_extreme_positive_mean": 0.05904835394563776,
   "n_positive_events": 115,
   "after_extreme_negative_mean": -0.0042227003018046215,
   "n_negative_events": 399
  }
 },
 "LTCUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.001592659160524351,
   "after_extreme_positive_mean": 0.0023989794096232996,
   "n_positive_events": 123,
   "after_extreme_negative_mean": 0.0026733257248195584,
   "n_negative_events": 312
  },
  "3d": {
   "unconditional_mean": 0.004428536044502969,
   "after_extreme_positive_mean": 0.009795104836989303,
   "n_positive_events": 123,
   "after_extreme_negative_mean": 0.007053401605728105,
   "n_negative_events": 312
  }
 },
 "ADAUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.0027824231081071075,
   "after_extreme_positive_mean": 0.01266683774739579,
   "n_positive_events": 110,
   "after_extreme_negative_mean": 0.004248120140204468,
   "n_negative_events": 311
  },
  "3d": {
   "unconditional_mean": 0.008112360608378161,
   "after_extreme_positive_mean": 0.017705574013453615,
   "n_positive_events": 110,
   "after_extreme_negative_mean": 0.01771083580084468,
   "n_negative_events": 311
  }
 },
 "BNBUSDT-PERP.BINANCE": {
  "1d": {
   "unconditional_mean": 0.0027751410790174216,
   "after_extreme_positive_mean": 0.0014920014682069942,
   "n_positive_events": 115,
   "after_extreme_negative_mean": 0.0029028066038731533,
   "n_negative_events": 175
  },
  "3d": {
   "unconditional_mean": 0.008208746894685787,
   "after_extreme_positive_mean": 0.02481311748991387,
   "n_positive_events": 115,
   "after_extreme_negative_mean": 0.0047423808822198415,
   "n_negative_events": 175
  }
 }
}
```

## Adversarial audit

Counts: {'PASS': 20, 'WARNING': 2}.

- WARNING `survivorship`: universe crypto-perp-research-6: perpetuals of the crypto-binance-6 set (same selection rule and the same caveat)
- WARNING `liquidity`: max position / median 1-minute traded value: 10.00% (ETHUSDT-PERP.BINANCE); entries capped at 10% of median bar volume

## Verdict: FAIL

Lines passing every gate at all cost multipliers: none. Family combinations passing: none. Audit FAIL checks: none.
