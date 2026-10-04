# funding-carry-v1: FAIL

Plan hash `ea0a0651480d3465`. Hypothesis: H-FUNDING-CARRY. Development window 2020-01-01 to 2025-08-31; the sealed holdout from 2025-09-01 was not read. Cumulative trial count used for the deflated Sharpe: 670. Gate config hash `2fc90f4ad972f482`.

Decision rule (pre-registered): PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, with the deflated Sharpe computed on the cumulative trial count of every registry under runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.

## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)

| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 22 | 3.74 | 1.00 | 0.00 | +1.8% | +57.1% | min_trades, walk_forward | fail |
| ETHUSDT | 36 | 2.20 | 0.83 | 0.00 | +3.8% | +57.1% | deflated_sharpe, walk_forward, bootstrap | fail |
| XRPUSDT | 34 | 1.61 | 0.45 | 0.86 | +2.8% | +57.1% | deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| LTCUSDT | 22 | 3.27 | 1.00 | 0.00 | +2.1% | +71.4% | min_trades | fail |
| ADAUSDT | 13 | 4.66 | 1.00 | 0.00 | +0.5% | +100.0% | min_trades | fail |
| BNBUSDT | 17 | -0.78 | 0.00 | 0.01 | +1.7% | +14.3% | min_trades, oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |

## Combinations

| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|
| family:funding_carry | 144 | 3.89 | 1.00 | +1.2% | walk_forward | fail |
| all | 144 | 3.89 | 1.00 | +1.2% | walk_forward | fail |

## Diagnostics (descriptive; they cannot change the verdict)

| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |
|---|---|---|---|---|---|---|
| BTCUSDT | +7.1% | 2023-01-01 (-1.0%) | yes | 1/1/0 of 7 | 6.78 | +100.0% |
| ETHUSDT | +4.8% | 2022-01-01 (-2.6%) | yes | 2/2/0 of 7 | 7.36 | +100.0% |
| XRPUSDT | +5.4% | 2022-07-01 (-1.5%) | yes | 2/1/0 of 7 | 4.10 | +80.0% |
| LTCUSDT | +6.9% | 2025-01-01 (-2.1%) | yes | 3/2/0 of 7 | 6.97 | +100.0% |
| ADAUSDT | +14.0% | 2022-01-01 (+0.0%) | yes | 3/2/0 of 7 | 6.02 | +100.0% |
| BNBUSDT | -1.2% | 2024-01-01 (-0.8%) | no | 0/0/0 of 7 | 1.55 | +86.7% |

Family combination: OOS return +6.1%, Sharpe 3.89, max drawdown +1.2%; worst fold 2022-01-01; effective independent sources 3.7 of 6.

## Adversarial audit

Counts: {'PASS': 12, 'WARNING': 1}.

- WARNING `survivorship`: universe crypto-carry-6: spot + perpetual of the crypto-binance-6 set (same selection rule and the same caveat)

## Verdict: FAIL

Lines passing every gate at all cost multipliers: none. Family combinations passing: none. Audit FAIL checks: none.
