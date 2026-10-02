# Phase B — Top-100 data readiness after the Tiingo acquisition (2026-10-02)

**Decision: `NOT_READY`.** 10 of 11 gates pass. G10, completeness, fails.

- No strategy backtest, trial, pilot or €200 run took place, and no strategy result exists.
- PEAD remains `DATA_NOT_READY`.
- The holdout fence is intact: every new download stops at 2026-08-31.

| Gate | Result | Evidence |
|---|---|---|
| G0 storage | PASS | The cache survived two container restarts (`storage_history.jsonl`). It is private (0700), outside git and integrity-hashed: Tiingo 513 files / 39 MB; SEC 13,561 files. Survival after this session ends is not provable (advisory). |
| G1 SEC frames | PASS | All discovery frames needed for 2009–2026 are cached. |
| G2 identifiers | PASS | Every member's float is checked against its market cap rolled back to the float's own date. All 6,100 member-dates fall in [0.2, 5]: median 0.998, 1st–99th percentile 0.49–1.21. No symbol is shared by two companies on any date. |
| G3 final membership | PASS | 61 quarterly dates from 2011-07-01 to 2026-07-01, each with exactly 100 members; 219 distinct companies. Nothing is deferred. |
| G4 Tiingo plan verified | PASS | Starter plan, verified manually by the account owner (`tiingo_verification.json`). |
| G5 within limits | PASS | **October usage: 444 unique symbols** (cap 450, plan 500), **410 MB** (cap 0.9 GB, 2 GB available), at most **45 requests in any hour** and **444 in a day**, no failed request. 443 files are bounded to 2008-01-01..2026-08-31, and none holds a bar past the fence. |
| G6 exits quantified | PASS | 86 pool exits (5.7 per year). 2 have a known outcome; the rest are liquidated at their last close. Scenario bias is about 0.43% per year. No delisting return is invented. |
| G7 candidate data | PASS | Four candidates are data-ready; H-PEAD is `DATA_NOT_READY`. |
| G8 holdout fence | PASS | Checked live: research reads stop at 2026-08-31. |
| G9 runtime | PASS | Estimated at about 0.75 hours. |
| **G10 completeness** | **FAIL** | Among the top 100 companies by point-in-time float, the number that cannot be priced is **12 in 2011**, is at most 3 on every date from 2018-01-02, and has a median of 2. The rule allows at most 3 on every date. |

## Acquisition

- **Ledger and caps:** `acquire_top100.py` uses a persistent request ledger (`RequestBudget`). It enforces 90% caps, seeded with this month's earlier usage (SPY on 10-01).
- **History window:** 2008-01-01 to 2026-08-31. The 36-month regression in H-RESMOM needs data from 2008-07 for the first rebalance in 2011-07, and the end is the day before the fence.
- **Resumable:** progress persists through per-symbol files, per-date snapshots and the ledger.
- **Licensing:** licensed price data stays in `~/.hedge-fund/cache`. The repository holds only membership (ticker, CIK, name and source; no prices) and the list of deferred symbols.

## Fixes found by spending real symbols

1. **SEC current map:** the builder tried preferred and ETN tickers (HBANL/M/P/Z; AMJB under JPM). It now tries only the primary listing and skips class or preferred variants.
2. **Mis-scaled floats:** some floats are tagged 1,000× too large (MannKind $197 B on $0.25 B of assets; AZEK $2.8 T). Floats above 50× the company's filed total assets are now rejected by an SEC-only check. A review of 7 dates found no real large cap affected.
3. **Vendor listings and reused tickers** (Tiingo `supported_tickers.zip`, vendor metadata):
   - The API serves only a ticker's active listing. The old histories of reused tickers (MON, DOW, EMC, DELL, STI, APC) are unreachable, and the builder now never prices across them.
   - Renamed companies keep their history under the new ticker (TFC, TPR, VMRK, MRSH).
   - **CB is one listing from 1984**, which is Chubb Corp until 2016. It now goes to Chubb Corp, whose own 10-K states it, instead of ACE, whose filings state no ticker. Before this fix ACE had been priced with Chubb's series.
4. **Deferred mode:** snapshots that still need downloads are no longer cached as final.

These cost about **85 of the 444 symbols** for tickers that ended up pricing nothing (preferred shares, mis-scaled small caps, reused tickers) before the fixes caught them.

## Why G10 fails

The causes are counted in member-dates within the top 100 by float (`top100_completeness.json`):

| Cause | Member-dates | Examples | Fixable with Tiingo? |
|---|---|---|---|
| Vendor history unavailable (ticker reused) | 75 | Dow Chemical, Monsanto, EMC, Anadarko (part) | **No.** Tiingo's API cannot serve the old listing. |
| Not listed at the vendor | 61 | Berkshire 2011–15 (share-class count missing on older cover pages, then a "BRKA" fallback), DIRECTV, 21st Century Fox, Shire | Partly. Berkshire can be fixed in code; FOXA and DTV are reused or later listings. |
| No symbol from SEC data | 77 | DuPont (DD), Time Warner (TWX), Anadarko, Baker Hughes (BHI), TWC, Discover (DFS), Praxair | Mostly. Tiingo lists TWX, BHI, TWC, DFS and PX, but their 10-Ks don't match the listing-sentence patterns. Needs better symbol extraction plus about 5–10 symbols from **November's** budget. |
| Other | 28 | Float/market-cap mismatches (mostly mis-tagged small caps), Simon Property (3 dates) | Review case by case. |

The gap is concentrated in 2011–2016 and consists mostly of **acquired or merged large caps**. A top-100 universe built from Tiingo alone therefore under-represents companies that later disappeared: a survivorship bias, and the gate rule says not to freeze on it.

## Options (human decision)

1. **Stay with Tiingo + SEC.** Fix the code-side causes (Berkshire class shares, symbol extraction), fetch the roughly 5–10 resolvable symbols in November, and re-measure. The reused-ticker companies stay missing. You would then either accept a quantified residual gap, likely 3–6 names per date in 2011–2015, by changing the rule (a human decision, recorded), or:
2. **Start development later,** at 2018-01-02, the first start from which the measured gap is ≤3 on every date. That leaves about 8.5 years of development history; the earlier prices remain available for H-RESMOM's 36-month warm-up.
3. **Add a source with delisted histories** (a licensed survivorship-free dataset). That would need your approval, because the rule is Tiingo + SEC only.
