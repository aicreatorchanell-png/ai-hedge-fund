# Phase B — pre-registration stage (no backtests yet)

| File | What it is |
|---|---|
| `preregistration.template.yaml` | Every pre-registration field, filled with proposed defaults. It validates against the repository (`validate_against_environment`). |
| `candidate_hypotheses.yaml` | Five candidate hypotheses (H-RESMOM, H-LOWVOL, H-PEAD, H-GPROF, H-ISSUE) with 10 parameter sets in total. **Proposals only.** None is registered in a ledger, backtested or tuned. |

Rules in force (code, not convention):

- **Holdouts.** `configs/holdouts.yaml` marks the Phase A holdout (2023-02-01..2026-06-30) as contaminated, so it can never be a holdout again. It seals the prospective Phase B holdout (2026-10-01..2027-09-30), fenced from 2026-09-01. Only a human-opened evaluation of one ledger trial may read the sealed window (`hedge_fund/validation/holdout_guard.py`).
- **Ledger.** Every trial is logged to a hash-chained `ExperimentLedger` before it runs, including failures (`hedge_fund/research/governance.py`).
- **Locking.** Locking a pre-registration is human-only (`approve_preregistration`). After that, `BudgetGuard` refuses any trial outside the pre-registered grid or budget.

Next step (human): choose hypotheses and approve the data plan in
`docs/phase-b-data-architecture-audit.md`. Then fill in and lock a pre-registration.
The first Phase B backtest comes after that, and is not run here.
