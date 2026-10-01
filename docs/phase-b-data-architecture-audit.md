# Phase B — research and data architecture audit

Date: 2026-10-01. Scope: can the platform run Phase B research on a broader
point-in-time (PIT) universe than Phase A's top-10 US megacaps? Method:
reading the code, checking the local caches, and timing the engine on
synthetic data (no network, no paid calls, no backtest of any Phase B
hypothesis).

## Verdict

The **engine is not hard-coded to the top-10 universe.** Panel, strategies,
portfolio, risk, execution and the ledger all take a ticker list or a PIT
`UniverseSchedule`, and membership is applied per session. The top-10 lives
only in the frozen Phase A and Buffett run scripts (`runs/…/universe_top10.json`).
Those scripts are evidence and stay unchanged.

What blocks a broad universe is **data supply and some missing realism**,
not the architecture: Tiingo free-tier limits, an ephemeral data cache,
hand-curated delisting events, no delisting-return haircut, no borrow fees,
and engine speed. Details follow, ordered by severity.

## What already supports a broad PIT universe

| Component | Status |
|---|---|
| `hedge_fund/universe/builder.py` | Survivorship-aware top-N by PIT market cap. Candidates come from SEC XBRL public-float frames, then a PIT screen on filings filed by D (recent 10-K/10-Q, operating company, plausible float). Price checks use Tiingo, and market cap uses filed shares × raw close. Delisted companies are discovered like any other. Every exclusion is recorded with its reason. `top_n` is a parameter. |
| `MarketPanel` / `AsOfView` | Any ticker set. Membership mask per session; split-basis restatement per view; tradable mask; `FutureDataError`. Phase B adds the holdout fence on `build`/`as_of`. |
| Delisting in the backtester | Liquidation at the last tradable close after `delist_grace` untradable sessions. Vendor placeholder prints are rejected by `tradability.py`. |
| Fundamentals | SEC EDGAR companyfacts, filtered by `filed` date (PIT). |
| Governance | The `ExperimentLedger` records data hashes per trial. `hash_tree(cache_dir, suffixes=(".gz", ".json"), exclude_tests=False)` hashes a cache snapshot, and `hash_panel(panel)` hashes the exact data a run used. |

## Gaps and blockers (most severe first)

1. **Tiingo free-tier limits (blocker for top-500).**
   - The free tier allows 50 requests/hour and 1,000/day. Tiingo also publishes a cap on unique symbols per month (500 on the free plan); verify it on the account.
   - Estimate for top-500 with quarterly reconstitution from 2011-07 to 2026-07: about 60 snapshots, a price-check pool of 2× top_n, and constituent turnover. That is roughly 1,500–2,500 distinct tickers and 3,000–4,000 first-time price downloads.
   - At free-tier rates that is several days of downloads, and the unique-symbol cap would stretch it over months.
   - Options for the human:
     - a paid Tiingo tier;
     - a smaller universe first (top-100/200);
     - fewer price checks (`pool_multiple` 1.5).
   - Financial Datasets stays forbidden.
2. **The data cache is ephemeral (blocker for reproducibility).**
   - `~/.hedge-fund/cache` currently holds Tiingo 7.5 MB (70 tickers), EDGAR 17 MB and universe 0.3 MB. It lives inside this container, which is reclaimed after inactivity, and it is not in git.
   - A broad universe needs persistent storage, plus a content hash recorded in the manifest so later runs can prove they used the same data.
   - Large raw caches should not go into this git repository.
3. **Delisting events are hand-curated.**
   - `hedge_fund/data/security_events.csv` has 4 rows. For hundreds of names, placeholder-print detection after a delisting depends on these events. Without them, the backtester sees zero-volume or unchanged prints only through the volume rule.
   - Needed: automatic events from the Tiingo end date per ticker, plus SEC Form 25/15 filings (EDGAR, PIT), recorded with their filing dates.
4. **No delisting-return haircut.**
   - A delisted holding is liquidated at its last tradable close. For small and mid caps, performance-related delistings are typically followed by large negative returns that this misses, so results are biased upward.
   - Needed: a pre-registered haircut by delisting type (for example −30% for performance-related delistings, 0% for cash acquisitions), applied in `_liquidate`.
5. **No borrow fees.**
   - Short positions pay nothing. `validate_against_environment` blocks any pre-registration with `allow_short`, so this is guarded, but long/short hypotheses cannot run until a borrow-fee accrual exists in the ledger.
6. **Engine speed.** Timed on synthetic data, monthly rebalance, 4 years:

   | Names | Panel build | Backtest |
   |---|---|---|
   | 50 | 3.8 s | 84 s |
   | 200 | 16 s | 339 s |

   - Both timings ran under tracemalloc, which inflates them. The cost is roughly linear in names × sessions, which projects to about an hour per 15-year top-500 backtest. Each trial needs walk-forward, CPCV, cost stress and bootstrap runs on top of that.
   - Fixed in this step: `marks_for` re-read every holding's full history each session. It now reads a 21-session window, vectorized, and falls back to full history only for halted names. Results are identical (equivalence test with halts longer than the window). A 100-name profile went from 56 s to 37 s.
   - Remaining hotspots: `SimulatedExecution.liquidity`/`reference_price`, which build an `as_of` view and a pandas slice per order, and `AsOfView.bars` copies.
   - Next fix: precompute ADV, volatility and open/close arrays once per panel in numpy, with the same "strictly before the session" rule and a look-ahead test.
7. **Universe definition is tied to the Buffett snapshot.**
   - `UniverseConfig.min_filings` defaults to `MIN_PERIODS` (4), which the LLM snapshot needs. It excludes companies in their first year after listing.
   - For price-based hypotheses this should be an explicit, pre-registered choice. It is configurable, but it is not exposed on the `python -m hedge_fund.universe` command line (only `--top-n`, `--cadence`, `--max-price-downloads`).
8. **Cadence and start date.**
   - `reconstitution_dates` supports annual and quarterly only, so monthly is refused at pre-registration.
   - PIT discovery needs SEC XBRL public-float frames, complete for all filers from mid-2011, and discovery reads two years of frames. Pre-registrations starting before 2011-07-01 are refused. The template was corrected from 2006 to 2011-07-01.
9. **Benchmarks.** Only a single-ticker benchmark (SPY, total return) exists. The template's secondary benchmarks (equal-weight universe, beta-matched SPY) need a small implementation from the panel before they can be reported.
10. **Sector data.** SIC codes come from EDGAR submissions; the builder already uses them for exclusions. Sector-neutral hypotheses need a PIT SIC → sector map exposed to the panel (SIC changes are rare but dated).
11. **Holdout fence coverage.**
    - The fence covers `MarketPanel`, the systematic backtester, the LLM agent backtesters (`backtest_fund`, `BacktestEngine.run_alpha`) and the ledger.
    - It does not cover the interactive `hedge_fund/run.py` (agents on today's data), the TUI or raw `TiingoClient` reads. Those are not research-selection paths, but a dashboard built on them could show sealed-window data.
    - Decision for a human: fence the data client itself, or accept this as an operational surface outside research.
    - Consequence of the prospective holdout: the systematic paper-trading loop cannot read data from 2026-09-01 until the holdout is evaluated or a human changes `configs/holdouts.yaml`.

## Recommended order before the first Phase B backtest

1. A human approves:
   - the data plan (Tiingo tier, universe size, persistent cache location);
   - the prospective holdout in `configs/holdouts.yaml`;
   - the fence scope (point 11).
2. Automatic delisting events and a pre-registered delisting-return haircut.
3. Vectorized liquidity and reference prices (point 6), with look-ahead tests.
4. A pilot universe build (for example top-100, quarterly, 2011-07 to 2026-07) within the request budget. Hash the cache and record the manifest.
5. Fill and lock the pre-registration (human). Then the first trial, through `BudgetGuard` and the ledger.
