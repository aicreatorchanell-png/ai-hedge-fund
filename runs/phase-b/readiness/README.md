# Phase B — final readiness audit (2026-10-01)

**Outcome: `NO_UNIVERSE_QUALIFIES`. The process stopped before freezing the preregistration and before any Phase B backtest.**

No Phase B strategy result exists. No trial was started. No pilot was run, including the €200 simulation. No data was downloaded.

Each part below links to the script and JSON that support it. All of them ran offline: Tiingo and SEC were called zero times.

---

## Part 1 — Holdout semantics (done; commit `71e1cc4`, raw-client fence `037a19d`)

The holdout dates are unchanged: 2026-10-01 to 2027-09-30, fenced from 2026-09-01. The declaration is now `evaluation_mode: forward`.

**Research mode** has zero access, for every purpose. The fence applies in these places:

- `MarketPanel.build` and `as_of`;
- the LLM agent backtesters;
- experiment-ledger trials;
- raw `TiingoClient` reads: bars inside a sealed window are hidden, and requests that touch one raise;
- `EdgarClient` reads with an `end_date`.

**Forward paper evaluation** (`hedge_fund/validation/forward.py`) is the only way to read the holdout:

| Rule | How it is enforced |
|---|---|
| A human freezes a candidate first | The spec hash and the engine code hash are recorded. `start_after` is set to the latest session already completed or already seen by the candidate's lineage. |
| One session at a time, as time passes | `step()` takes no date. It evaluates the next session that real calendar time has completed. There is no rewind and no arbitrary historical read. |
| No change in place | Each step replays the run from the frozen specs. Every recorded session must reproduce exactly, so any change to config, code or data is refused. |
| Records cannot be quietly edited | Records are append-only, hash-chained, and anchored by a head file. |
| Results reach humans only | Only `human:` actors with purpose `human_review` can read results. Each read is itself recorded. Optimization, selection, feature discovery, LLM candidate generation and dashboards are all refused. |
| Lineage is explicit | A modified candidate becomes a new generation with a declared parent. It starts after the last session its lineage observed. |
| Real time only | An injected clock is refused for any reviewed holdout. |

**Adversarial tests** (`hedge_fund/validation/test_forward.py`) cover these attacks:

- every research purpose;
- a forward purpose used outside a step;
- a strategy that, during a step, tries to read future views or panels, asks for research access, or starts a thread;
- tampering with records, or truncating them;
- changing data under an already-observed session;
- changing the code hash;
- adding a sibling candidate without declaring lineage;
- raw Tiingo reads, both outside a step and beyond the step's session.

A static scan also checks that no research code imports the evaluator or the guard's internals.

**Disclosure.** The local Tiingo cache contains embargo bars from 2026-09-01 to 2026-09-29: 59 of 70 files, extended by earlier cache refreshes. No holdout bars (2026-10-01 or later) exist. Before `037a19d`, the first run of `tiingo_budget.py` opened these files directly to average row counts and file sizes; it read no prices or returns. Its output has since been regenerated with those rows dropped.

## Part 2 — Storage: `PERSISTENT_STORAGE_REQUIRED`

What I inspected:

- **Disk:** `/` is the container's own disk (ext4), and the cache lives there at `~/.hedge-fund/cache`. `/mnt/user-data` is a plain directory on the same disk. Neither survives the container being recreated.
- **Harness storage:** `FSSPEC_GCS` (present; value not printed) and `/opt/rclone` belong to the session harness, not to a private store you configured. Pushing licensed data there would be an upload you didn't approve, so I didn't.

What is implemented (`9ef30b7`):

- `AIHF_CACHE_DIR` moves the cache root. `AIHF_TIINGO_CACHE_DIR` and `AIHF_SEC_CACHE_DIR` move one source each, so licensed Tiingo data and public SEC data can live apart.
- The Tiingo cache is refused inside the git checkout.
- Each source gets its own `MANIFEST.json` with a schema version, licence class, size and sha256 for every file, and a tree hash. It is written atomically and can be re-verified. Cache files themselves were already written atomically.
- Current tree hashes: Tiingo `4fb0c15c…` (70 files), SEC `ef8829e4…` (1,178 files).

Safe options, for you to choose from:

1. A private volume attached to the environment, with `AIHF_CACHE_DIR` pointed at it.
2. A private, access-controlled bucket you own, synced manually with the manifests verified on restore. It must never be the repository.
3. Running the data build on your own machine through Remote Control, where the cache persists.

## Part 3 — Tiingo budget (dry run, `tiingo_budget.py` → `tiingo_budget.json`)

**How the estimate was built:**

- **Exact replay:** the builder's discovery step is replayed exactly from the cached SEC frames, for 39 of 61 quarterly dates.
- **Extrapolation:** 22 dates need frames that aren't cached (CY2009–13 and CY2025Q3 onward). They are filled in from the measured rate of new names per quarter.
- **Measured rates:** the SEC screen pass rate (0.80), the pricing failure rate (0.19 per slot) and symbols per company (1.06) all come from the real top-10 build.
- **Cost per symbol:** each new symbol costs exactly one Tiingo GET, a full-history download.

| Universe | Unique symbols | Cached | Missing | Requests | Free tier feasible? |
|---|---|---|---|---|---|
| Quarterly PIT top-100 | ~735 | 50 | ~685 | ~685 | **No.** 685 exceeds the 450 symbols/month limit (500 with 10% headroom), so it needs 2 months. About 15 hours at 50/hour, about 1 day at 1000/day. About 1.0 GB transferred. |
| Quarterly PIT top-200 | ~1,255 | 50 | ~1,205 | ~1,205 | **No.** Needs 3 months. About 27 hours / 1.3 days. About 1.8 GB. |
| Quarterly PIT top-500 | ~2,946 | 50 | ~2,896 | ~2,896 | **No.** Needs 7 months. About 64 hours / 3.2 days. About 4.2 GB. |

The 500 unique-symbols-per-month figure is the limit as you stated it; I didn't query the account. The rates were measured on megacaps, so smaller names will probably fail pricing more often and need more requests. Building the universe also needs about 4,000–7,800 SEC companyfacts reads; SEC is public and allows 10 requests per second.

## Part 4 — Candidate data audit (`candidate_data_audit.py` → `candidate_data_measurements.json`)

Measured on 450 cached companyfacts files (large caps):

- **Reporting lag:** 10-Q median 36 days (p90 41); 10-K median 55 days (p90 74).
- **Restatements:** 8.0% of revenue/asset periods carry more than one reported value. Values are read as filed on or before the observation date (`KnowledgeView`: `filed <= cutoff`), so later restatements never leak backward.

| Candidate | Verdict | Evidence and construction facts |
|---|---|---|
| **Residual momentum** (H-RESMOM) | DATA_READY (definition) | See "Residual momentum" below. |
| **Low volatility** (H-LOWVOL) | DATA_READY (definition) | See "Low volatility" below. |
| **PEAD** (H-PEAD) | **`DATA_NOT_READY`** | See "PEAD" below. |
| **Gross profitability** (H-GPROF) | DATA_READY, with exclusions | See "Gross profitability" below. |
| **Net share issuance** (H-ISSUE) | DATA_READY, with exclusions | See "Net share issuance" below. |

### Residual momentum (H-RESMOM)

- **Residual model:** OLS of each stock's daily log return on SPY's daily log return. The only factor is SPY, from Tiingo, using split-adjusted closes without dividends.
- **Estimation window:** rolling 36 or 24 months of sessions strictly before t, so no future factor data is used.
- **Signal:** the sum of residuals over t−252 to t−21, divided by their standard deviation.
- **Portfolio:** monthly rebalance; top quintile, inverse-volatility weighted, long-only.
- **Difference from the published construction** (Blitz, Huij and Martens 2011):
  - they use Fama-French 3-factor residuals on monthly returns;
  - they hold equal-weight decile long–short portfolios across the broad US market.
- **What ours measures instead:** a long-only, large-cap tilt that keeps market beta. Fama-French factors are not in the allowed data sources.

### Low volatility (H-LOWVOL)

- **Definition:** standard deviation of daily returns over the last 126 or 252 sessions; lowest quintile.
- **Portfolio:** equal weight, capped per name, quarterly rebalance.
- **Sector concentration:** expected in utilities, staples and REITs. It will be reported per rebalance as a diagnostic, using PIT SIC codes from EDGAR; it is not a constraint.
- **Not Betting Against Beta.** BAB is a leveraged, beta-neutral long–short construction (long low-beta, short high-beta). This candidate is long-only and unlevered, with beta below 1, and it is judged on CAPM alpha, not raw return.

### PEAD (H-PEAD) — `DATA_NOT_READY`

- **No usable event source in the allowed data:**
  - in this codebase, earnings 8-K and surprise records come only from Financial Datasets, which is forbidden; `EdgarClient.get_earnings_history` raises `NotImplementedError`;
  - EDGAR's structured EPS arrives with the 10-Q/10-K, a median of 36–55 days after period end, which is typically weeks after the earnings release;
  - 8-K item 2.02 acceptance times exist in EDGAR submissions (not kept in the trimmed cache), but the EX-99.1 press release is unstructured text.
- **Why there is no workaround:**
  - pairing the 8-K timestamp with EPS from the 10-Q would use data that wasn't public yet;
  - using the 10-Q date instead would be the silent substitution you ruled out.
- **Result:** "announcement < observation < execution" cannot be proven for every event.
- **Path to readiness:** parse the 8-K 2.02 acceptance datetimes, extract EPS from EX-99.1, validate it against the later XBRL values, and test the timestamp order for every event.

### Gross profitability (H-GPROF)

- **Inputs:** revenue, cost of revenue (or the GrossProfit tag), and total assets, as filed by the observation date.
- **Coverage at 2020-06:** 279 of 403 companies (197 tagged, 82 derived). The 124 without inputs are mostly financials, which the definition excludes, plus some tagging gaps.
- **Timing:** reporting lag as above. A filing is observed on its filing date and executed at the next session's open.

### Net share issuance (H-ISSUE)

- **Share counts:** `dei:EntityCommonStockSharesOutstanding` as filed on each filing's cover page. History is never inferred from today's adjusted shares.
- **Splits:** adjusted using Tiingo split events dated between the two cover dates (visible rows only).
- **What counts as issuance:** buybacks, new issuance and stock-funded mergers all show up as changes in shares outstanding, which is the definition the literature uses.
- **Exclusions:** 417 of 450 companies have a single unambiguous value. 3 are ambiguous multi-class companies (excluded unless in the curated `SHARE_CLASSES` list) and 30 have no cover-page shares (excluded).

## Part 5 — Delisting and survivorship audit (`delisting_audit.py` → `delisting_audit.json`)

The universe proxy uses the builder's own discovery logic over 2016-01 to 2024-07 (35 quarterly dates). A member counts as disappeared when it is absent from all of the last four cached frames.

| | Top-100 | Top-200 | Top-500 |
|---|---|---|---|
| Distinct members | 272 | 455 | 968 |
| Disappeared (stopped filing) | 73 (8.3/yr) | 112 (12.8/yr) | 230 (26.3/yr) |
| With stored quotes | 0 | 0 | 0 |
| With a known outcome (curated) | 0 | 0 | 0 |
| Would be liquidated at last close | 73 | 112 | 230 |
| Bias if 10% / 25% / 50% are performance-related (−30%) | 0.25 / 0.63 / 1.25 %/yr | 0.19 / 0.48 / 0.96 %/yr | 0.16 / 0.39 / 0.79 %/yr |

- **These are upper bounds.** The proxy skips the SEC screen, so it includes filers with bad float tags. Many disappearances are re-registrations under a new CIK or mergers rather than losses: Google → Alphabet, Walgreen Co, Dow Chemical, Broadcom Ltd.
- **No delisting return was invented.** The −30% is Shumway's (1997) average for performance-related delistings, used only as a scenario bound.
- **Not claimed:** no result is claimed to be survivorship-safe for small or mid caps.
- **Scalable treatment:** EDGAR Forms 25 and 15 and 8-K items 2.01 and 1.03 can classify outcomes, but they need submission downloads. Terminal values for bankruptcies are still not available.

## Part 6 — Performance (commit `f78c5b2`)

- **Equivalence:** `marks_for` and the new per-session memoization in execution are both checked against the original code paths. Four whole backtests produced byte-identical serialized results, covering trades, fills, rejections, liquidations, NAV, cash, exposure, metrics and decisions. Repeated runs were deterministic. The four scenarios:
  - $100k time-series momentum;
  - €200 weekly mean reversion with a minimum commission;
  - a delisted holding;
  - halts longer than the `marks_for` window.
- **Speed:** a 100-name profiled run went from 56 s to 37 s, then to 21 s. Without the profiler, about 4.5 s per 100 names per year.
- **Next hotspot:** `apply_corporate_actions`, not yet optimized.

## Part 7 — Mechanical pilot selection (`pilot_selection.py` → `pilot_selection.json`)

The rules were fixed in the script before it was evaluated. No strategy result exists.

| Gate | Top-100 | Top-200 | Top-500 |
|---|---|---|---|
| G0 persistent private storage | fail | fail | fail |
| G1 reliable PIT membership (schedule buildable from available data) | fail | fail | fail |
| G2 delisting quantified, bias ≤ 1%/yr at a 25% performance-related share | pass | pass | pass |
| G3 a DATA_READY candidate | pass | pass | pass |
| G4 free tier with 10% headroom | fail | fail | fail |
| G5 runtime ≤ 12 h | pass (0.76 h) | pass (1.51 h) | pass (3.77 h) |

**Decision:** `NO_UNIVERSE_QUALIFIES`, so Parts 8–10 were not executed. Freezing needs a finalized universe and a data hash. A preregistration cannot honestly be locked, and trials cannot be appended, before the data exists.

## Final comparative table

| Candidate | Data Ready | Universe | Trials | OOS Sharpe | DSR | PBO | Max DD | Costs | €200 End | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Residual momentum | definition yes; universe data not acquired | none qualifies | 0 | — | — | — | — | — | — | `DATA_NOT_READY` |
| Low volatility | definition yes; universe data not acquired | none qualifies | 0 | — | — | — | — | — | — | `DATA_NOT_READY` |
| PEAD | **no** (event timing) | — | 0 | — | — | — | — | — | — | `DATA_NOT_READY` |
| Gross profitability | definition yes; universe data not acquired | none qualifies | 0 | — | — | — | — | — | — | `DATA_NOT_READY` |
| Net share issuance | definition yes; universe data not acquired | none qualifies | 0 | — | — | — | — | — | — | `DATA_NOT_READY` |

All five candidates are kept and unchanged; nothing was cherry-picked.

## What unblocks the pilot (decisions for a human)

1. **Storage (G0):** choose one of the private storage options in Part 2.
2. **Data plan (G4):** either a paid Tiingo tier, or a top-100 acquisition spread over two calendar months (about 450 symbols in each). The two-month plan only makes sense once G0 is solved; otherwise the first month's downloads are lost.
3. **Membership (G1):** after that, fetch the 24 missing SEC frames (public data) and build the quarterly top-100 schedule.
4. **Rerun:** run the readiness scripts again, then freeze and lock (human), then append the trials, then run the pilot.
