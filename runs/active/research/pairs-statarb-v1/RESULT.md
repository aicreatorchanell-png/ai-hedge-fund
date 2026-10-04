# pairs-statarb-v1: FAIL

Plan hash `f4b9fa9e0e21417d`. Hypothesis: H-PAIRS-STATARB. Development window 2020-01-01 to 2025-08-31; the sealed holdout from 2025-09-01 was not read. Cumulative trial count used for the deflated Sharpe: 672. Gate config hash `2fc90f4ad972f482`.

Decision rule (pre-registered): PASS if and only if (a) at least one instrument line, or the equal-weight family combination, passes every gate in configs/validation-gates.yaml (hash recorded here) both at cost x1.0 and at cost x2.0, with the deflated Sharpe computed on the cumulative trial count of every registry under runs/active/research, and (b) the adversarial audit of this plan reports no FAIL. Otherwise FAIL. Regime, worst-fold, parameter-stability, concentration and CPCV diagnostics are reported but cannot turn a FAIL into a PASS. The sealed holdout (from reserve_start) is not read.

## Out-of-sample lines (walk-forward, cost x1; x2 stress shown as pass/fail)

| Line | Trades | OOS Sharpe | Deflated Sharpe | PBO | Max DD | Positive folds | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|---|---|
| BTC/ETH | 19 | -0.25 | 0.00 | 0.47 | +22.6% | +42.9% | min_trades, oos_sharpe, deflated_sharpe, walk_forward, bootstrap | fail |
| ETH/ETC | 63 | 0.13 | 0.00 | 0.54 | +24.8% | +42.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| BTC/BCH | 37 | 0.43 | 0.01 | 0.86 | +32.3% | +57.1% | oos_sharpe, deflated_sharpe, pbo, max_drawdown, walk_forward, bootstrap | fail |
| BTC/LTC | 45 | -1.07 | 0.00 | 0.54 | +61.6% | +28.6% | oos_sharpe, deflated_sharpe, pbo, max_drawdown, walk_forward, bootstrap | fail |
| XRP/XLM | 29 | 0.85 | 0.01 | 0.23 | +20.2% | +57.1% | min_trades, deflated_sharpe, walk_forward, bootstrap | fail |

## Combinations

| Combination | Trades | OOS Sharpe | Deflated Sharpe | Max DD | Failed gates (x1) | x2 |
|---|---|---|---|---|---|---|
| family:pairs_statarb | 193 | 0.26 | 0.00 | +13.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |
| all | 193 | 0.26 | 0.00 | +13.9% | oos_sharpe, deflated_sharpe, pbo, walk_forward, bootstrap | fail |

## Diagnostics (descriptive; they cannot change the verdict)

| Line | OOS return | Worst fold | Fragile | Stability flags (sharp/unstable/isolated of folds) | CPCV median Sharpe | CPCV share > 0 |
|---|---|---|---|---|---|---|
| BTC/ETH | -11.9% | 2025-01-01 (-10.4%) | no | 7/7/0 of 7 | 0.29 | +60.0% |
| ETH/ETC | -27.2% | 2022-01-01 (-32.5%) | no | 4/4/0 of 7 | -0.85 | +26.7% |
| BTC/BCH | +19.3% | 2023-01-01 (-19.0%) | no | 3/3/0 of 7 | -0.02 | +46.7% |
| BTC/LTC | -23.6% | 2023-01-01 (-24.4%) | no | 2/2/0 of 7 | -0.98 | +6.7% |
| XRP/XLM | +126.8% | 2025-01-01 (-9.0%) | yes | 4/2/0 of 7 | 0.42 | +66.7% |

Family combination: OOS return +11.7%, Sharpe 0.36, max drawdown +14.2%; worst fold 2025-01-01; effective independent sources 4.8 of 5.

## Adversarial audit

Counts: {'PASS': 12, 'WARNING': 1}.

- WARNING `survivorship`: universe crypto-perp-early-12: every USD-M perpetual with archive data by 2020-02 (all 12 early listings, incl. EOS, delisted 2025); later listings are excluded, so coins that only became large later are absent

## Verdict: FAIL

Lines passing every gate at all cost multipliers: none. Family combinations passing: none. Audit FAIL checks: none.
