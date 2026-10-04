# xs-momentum-v1: FAIL

Plan hash `e8c4f0d55272fa0f`. Hypothesis: H-XS-MOMENTUM. Development window 2020-01-01 to 2025-08-31; the sealed holdout from 2025-09-01 was not read. Cumulative trial count used for the deflated Sharpe: 670. Gate config hash `2fc90f4ad972f482`.

Decision rule (pre-registered): PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, with the deflated Sharpe computed on the cumulative trial count of every registry under runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.

## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)

| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|---|---|
| PERP12 | 343 | 0.01 | 0.00 | 0.47 | +39.6% | +57.1% | oos_sharpe, deflated_sharpe, max_drawdown, walk_forward, bootstrap | fail |

## Combinations

| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|
| family:xs_momentum_crypto | 343 | 0.01 | 0.00 | +39.6% | oos_sharpe, deflated_sharpe, max_drawdown, walk_forward, bootstrap | fail |
| all | 343 | 0.01 | 0.00 | +39.6% | oos_sharpe, deflated_sharpe, max_drawdown, walk_forward, bootstrap | fail |

## Diagnostics (descriptive; they cannot change the verdict)

| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |
|---|---|---|---|---|---|---|
| PERP12 | -4.7% | 2023-07-01 (-20.9%) | no | 2/2/0 of 7 | -0.12 | +33.3% |

Family combination: OOS return -4.7%, Sharpe 0.13, max drawdown +40.7%; worst fold 2023-07-01; effective independent sources 1.0 of 1.

## Adversarial audit

Counts: {'PASS': 12, 'WARNING': 1}.

- WARNING `survivorship`: universe crypto-perp-early-12: every USD-M perpetual with archive data by 2020-02 (all 12 early listings, incl. EOS, delisted 2025); later listings are excluded, so coins that only became large later are absent

## Verdict: FAIL

Lines passing every gate at all cost multipliers: none. Family combinations passing: none. Audit FAIL checks: none.
