# vol-trend-v1: FAIL

Plan hash `5886badfa1d43e65`. Hypothesis: H-VOL-SCALED-TREND. Development window 2020-01-01 to 2025-08-31; the sealed holdout from 2025-09-01 was not read. Cumulative trial count used for the deflated Sharpe: 624. Gate config hash `2fc90f4ad972f482`.

Decision rule (pre-registered): PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, with the deflated Sharpe computed on the cumulative trial count of every registry under runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.

## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)

| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|---|---|
| BCHUSDT-PERP | 37 | 0.33 | 0.01 | 0.73 | +5.0% | +57.1% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| EOSUSDT-PERP | 57 | -0.47 | 0.00 | 0.96 | +13.9% | +42.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| TRXUSDT-PERP | 68 | 0.15 | 0.00 | 0.59 | +5.6% | +42.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| ETCUSDT-PERP | 68 | -0.83 | 0.00 | 0.74 | +9.4% | +0.0% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| LINKUSDT-PERP | 62 | -0.15 | 0.00 | 0.44 | +4.4% | +28.6% | oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |
| XLMUSDT-PERP | 49 | 0.73 | 0.02 | 0.27 | +3.7% | +57.1% | deflated_sharpe, walk_forward, bootstrap | fail |
| EURUSD | 36 | -0.05 | 0.00 | 0.57 | +4.3% | +42.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| GBPUSD | 36 | 0.07 | 0.00 | 0.17 | +4.4% | +57.1% | oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |
| USDJPY | 20 | 0.45 | 0.01 | 0.41 | +4.6% | +42.9% | min_trades, oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |
| AUDUSD | 60 | -0.87 | 0.00 | 0.56 | +9.2% | +28.6% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| USA500IDXUSD | 34 | 0.26 | 0.00 | 0.90 | +4.1% | +57.1% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| DEUIDXEUR | 29 | -0.32 | 0.00 | 0.56 | +7.3% | +42.9% | min_trades, oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |

## Combinations

| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|
| family:vol_managed_trend | 556 | -0.10 | 0.00 | +3.2% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| all | 556 | -0.10 | 0.00 | +3.2% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |

## Diagnostics (descriptive; they cannot change the verdict)

| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |
|---|---|---|---|---|---|---|
| BCHUSDT-PERP | +3.2% | 2023-07-01 (-1.4%) | no | 7/4/0 of 7 | -0.16 | +46.7% |
| EOSUSDT-PERP | -6.8% | 2024-01-01 (-7.7%) | no | 6/6/0 of 7 | -0.67 | +0.0% |
| TRXUSDT-PERP | +1.3% | 2023-01-01 (-1.9%) | yes | 2/2/0 of 7 | 0.54 | +66.7% |
| ETCUSDT-PERP | -7.6% | 2023-01-01 (-2.6%) | no | 1/1/0 of 7 | -0.42 | +33.3% |
| LINKUSDT-PERP | -1.4% | 2022-01-01 (-0.9%) | no | 4/2/0 of 7 | 0.18 | +53.3% |
| XLMUSDT-PERP | +10.5% | 2024-01-01 (-1.6%) | yes | 5/3/0 of 7 | 0.13 | +53.3% |
| EURUSD | -0.3% | 2023-07-01 (-0.8%) | no | 5/5/0 of 7 | -0.13 | +40.0% |
| GBPUSD | +0.5% | 2024-01-01 (-1.3%) | yes | 5/5/0 of 7 | 0.04 | +53.3% |
| USDJPY | +3.7% | 2024-07-01 (-1.5%) | yes | 1/1/0 of 7 | -0.20 | +40.0% |
| AUDUSD | -7.2% | 2025-01-01 (-3.3%) | no | 2/2/0 of 7 | -0.68 | +13.3% |
| USA500IDXUSD | +2.1% | 2022-01-01 (-1.5%) | yes | 2/4/0 of 7 | -0.49 | +33.3% |
| DEUIDXEUR | -2.8% | 2023-07-01 (-2.6%) | no | 4/4/0 of 7 | 0.07 | +66.7% |

Family combination: OOS return -0.4%, Sharpe -0.10, max drawdown +3.2%; worst fold 2024-01-01; effective independent sources 9.8 of 12.

## Hypothesis predictions (development data, descriptive)

```
{
 "max_drawdown_by_asset_class": {
  "crypto": 0.035658235753577294,
  "fx": 0.046584331818083524,
  "index": 0.043062465999505695
 },
 "max_drawdown_portfolio": 0.031693176863241024,
 "prediction_portfolio_drawdown_below_every_class": true
}
```

## Adversarial audit

Counts: {'PASS': 16, 'FAIL': 2, 'WARNING': 1}.

- FAIL `train_test_holdout`: the reserve start 2025-09-01 is readable for fx: no sealed holdout; the reserve start 2025-09-01 is readable for index: no sealed holdout
- FAIL `survivorship`: instruments ['AUDUSD.DUKASCOPY', 'BCHUSDT-PERP.BINANCE', 'DEUIDXEUR.DUKASCOPY', 'EOSUSDT-PERP.BINANCE', 'ETCUSDT-PERP.BINANCE', 'EURUSD.DUKASCOPY', 'GBPUSD.DUKASCOPY', 'LINKUSDT-PERP.BINANCE', 'TRXUSDT-PERP.BINANCE', 'USA500IDXUSD.DUKASCOPY', 'USDJPY.DUKASCOPY', 'XLMUSDT-PERP.BINANCE'] are not a declared universe
- WARNING `liquidity`: max position / median daily traded value: 10.00% (AUDUSD.DUKASCOPY); entries capped at 10% of median bar volume

## Verdict: FAIL

Lines passing every gate at all cost multipliers: none. Family combinations passing: none. Audit FAIL checks: ['train_test_holdout', 'survivorship'].
