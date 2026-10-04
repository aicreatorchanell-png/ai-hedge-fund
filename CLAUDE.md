# ai-hedge-fund — working rules for Claude

Systematic trading research platform. Python 3.12 + Poetry, package `hedge_fund/`.
Educational research only: no profitability claims, no real-money trading.

## Git
- Never modify, merge into, or push to `main`. Work on the assigned `claude/*` branch.
- Never force-push or rewrite pushed history. Checkpoint-commit each finished phase.

## Data
- Prices: Tiingo. Fundamentals: SEC EDGAR point-in-time. Approved by the owner on
  2026-10-03 for research: Binance public bulk data (data.binance.vision), CCXT public
  market-data endpoints, Dukascopy historical data. Nothing else by default.
- Paid data (Databento, Tardis, ...) is OFF unless the owner approves it.
- Market data is raw in the private cache (`hedge_fund.paths.CACHE_DIR`), never in git.
- Financial Datasets is forbidden. It is blocked in code (`hedge_fund/data/policy.py`)
  unless `AIHF_ALLOW_FINANCIAL_DATASETS=1`; never set that. Also run commands with
  `env -u FINANCIAL_DATASETS_API_KEY`.
- Strict point-in-time: a decision at session D may use only data published by D.
  Market data for strategies goes through `hedge_fund.systematic` `AsOfView` (daily engine)
  or `hedge_fund.trading.data.catalog.Catalog.load_bars` (active engine; close-stamped bars).
- Fills happen at the next eligible execution event, never the observation session.

## Research discipline
- Never tune parameters on a locked holdout. A holdout failure ends that candidate;
  do not iterate on the same holdout.
- Record every experiment in the registry; count trials for multiple-testing adjustments.
- The quantitative engine (signals → portfolio → risk → execution) is deterministic Python.
- Active trading runs on NautilusTrader (pinned in pyproject) through `hedge_fund.trading`:
  strategies subclass `GuardedStrategy` (bracket entries, risk sizing, governor, kill switch)
  and backtest via `run_backtest` (holdout fence, next-bar fills, ambiguity audit).
  The daily `hedge_fund.systematic` engine and the stock/SEC universe are kept as is.
- Backtest only for now: no live trading node, no live broker adapter (`test_safety.py`).
  Paper trading (IBKR paper, crypto testnet) is approved for preparation only; real-money
  and live trading stay disabled.
- Research runs use a frozen `ResearchPlan` (`hedge_fund.trading.research`); every run is a
  registry trial; selection is on training windows only. The trial count is cumulative over
  every registry in `runs/active/research/` and never resets between phases.
- No strategy family is researched without an approved economic hypothesis
  (`research/hypotheses/H-*.yaml`, `hedge_fund/trading/hypothesis.py`): why the edge exists,
  who pays, why they pay, why it persists, when it disappears, testable predictions.
  AI may draft; only a human approves (`approve_hypothesis`). Families already tested
  (plan_crypto_v1) cannot be re-tested on the same instruments and development dates.
- Every plan gets the adversarial audit (`hedge_fund.trading.audit`, PASS/WARNING/FAIL).
  Promotion from VALIDATED on requires a passing audit; any FAIL blocks it.
- Diagnostics (`regimes.py`, `diagnostics.py`: causal regimes, worst fold, parameter
  neighbourhood, concentration, diversification) describe results; they never create
  trading rules or choose parameters.
- Paper/live health monitor (`hedge_fund.trading.health`): thresholds in
  `configs/health-thresholds.yaml` are versioned and fixed before deployment; HALT blocks
  new entries and only a human can reset it (protected actions `reset_strategy_halt`,
  `change_health_thresholds`). Backtests refuse instruments without trading costs.
- AI output (Claude, Kimi, ...) is a proposal or critique only. Promotion needs the
  Python validation gates to PASS, then paper trading, then explicit human approval.
- AI must never change risk limits, validation thresholds, the locked holdout, or the
  kill switch, and must never enable live trading.

## LLM cost
- Cache every paid LLM response (atomic writes); never re-pay a cached prompt.
- Respect the task's cost cap; estimate before any large batch and stop above it.

## Tests
- Offline suite before and after changes:
  `env -u FINANCIAL_DATASETS_API_KEY poetry run pytest -q -p no:cacheprovider hedge_fund`
- Every new module ships with tests; look-ahead guards must stay tested.

## Secrets
- Never print, log, commit, or echo API keys, tokens or credentials. Report env vars as
  PRESENT/ABSENT only.
