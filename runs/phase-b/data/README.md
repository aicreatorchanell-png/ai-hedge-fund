# Phase B — Top-100 data readiness (2026-10-01)

**Decision: `NOT_READY` (7 PASS, 3 FAIL). No freeze. No strategy trial was run and no strategy result exists.**

- **Scope:** data readiness only, for a quarterly point-in-time top-100 universe over the development period 2011-07-01 to 2026-07-01 (61 reconstitution dates).
- **Tiingo:** 2 requests were made to verify the account (Part 3), and no price data was downloaded.
- **PEAD:** remains `DATA_NOT_READY`. 10-Q/10-K filing dates were not used as a substitute for earnings announcement dates.

## Gates (`readiness_gates_top100.py` → `readiness_gates_top100.json`)

| Gate | Result | Evidence |
|---|---|---|
| G0 storage | **PASS** | The cache survived a container restart (recorded in `storage_history.jsonl`: boot at 19:04:51Z, pre-boot manifests verified with 0 missing, Tiingo tree hash unchanged). Integrity verifies today too. It sits outside git (no Tiingo file is tracked), and the directories are mode 0700. *Advisory:* survival after this session ends is **not provable**, because a new session starts on a fresh machine. |
| G1 SEC frames | **PASS** | All 24 missing public-float frames (CY2009Q1–CY2013Q4, CY2025Q3–CY2026Q2) were acquired from SEC in 24 requests. Every discovery window is now complete. |
| G2 universe (SEC stages) | **FAIL** | PIT, survivorship, lineage, coverage and duplicate checks pass. The sampled symbol verification fails: today's SEC ticker map contradicts the company's own filings in **5 of 60** verifiable (company, date) rows. |
| G3 final market-cap membership | **FAIL** | The ranking needs Tiingo prices for 494 first-try symbols, of which 64 are cached and **430 are missing**. They were not downloaded. |
| G4 Tiingo account verified | **FAIL** | Tiingo has no API for plan or usage and returns no rate-limit headers. The plan has to be read from the account dashboard by a human. |
| G5 Tiingo fits the plan | **PASS (conditional)** | Assuming the most restrictive plan (Starter, 500 symbols and 1 GB per month): 430 symbols against a 450 limit (with 10% headroom), and about 0.73 GB against 0.9 GB. That leaves little room, and if first-try symbols fail, the contingency set (631 missing symbols) would exceed the limit. |
| G6 exits quantified | **PASS** | 86 pool members later stop filing (5.7 per year). Only 2 have a known outcome; 84 would be liquidated at their last close. The scenario bias is about 0.43% per year if 25% of exits are performance-related. No delisting returns were invented. |
| G7 candidate data | **PASS** | H-RESMOM, H-LOWVOL, H-GPROF and H-ISSUE have data-ready definitions. H-PEAD is `DATA_NOT_READY`. |
| G8 holdout fence | **PASS** | Checked live: research reads into the embargo and holdout are refused, SPY data is visible only through 2026-08-31, and the last universe date is 2026-07-01. |
| G9 runtime | **PASS** | Estimated at about 0.75 hours for the full pilot. |

## What was done

1. **Storage:** `storage_check.py` verifies persistence. The cache directories are now owner-only (0700). The cache location can be configured, sources are kept separate (`AIHF_TIINGO_CACHE_DIR` / `AIHF_SEC_CACHE_DIR`), and each has an integrity manifest (`python -m hedge_fund.data.cache_manifest`). Nothing was uploaded anywhere.

2. **Tiingo** (`tiingo_verification.json`):
   - **Published limits:** the free Starter plan allows 500 unique symbols, 50 requests per hour, 1,000 per day and **1 GB per month**. Power ($30/month) allows 110,159 symbols and 40 GB.
   - **Measured:** responses are **not compressed**, at 263 bytes per row on the wire.
   - **Account:** no plan was purchased or changed. The account's actual plan and this month's usage remain **unverified**.

3. **SEC frames:** the 24 missing frames were acquired.

4. **Universe, SEC stages** (`build_top100_sec.py` → `top100_sec_stage.json`, 61 dates, about 7,500 SEC requests, 0 Tiingo requests). Three builder fixes, each tested:
   - **Point-in-time nominations** (`f2a6d43`): a frame row nominates a company only if its float was filed by the date. This excluded 11,287 nominations that relied on later filings, including filings made during the embargo.
   - **Symbols for pre-2019 filers:**
     - first fix (`91f0254`): look for the cover-page symbol on earlier filings too;
     - second fix (`f316f7f`): read the listing sentence in the company's own 10-K text.

     At 2011-07-01, the top 250 candidates without a symbol fell from 57 to 17. These were large caps such as Disney (old CIK), Medco, Genzyme, EMC and Dell, which would otherwise have dropped out (a survivorship bias). Across the whole period, 195 of 12,200 pool rows (1.6%) still have no symbol.
   - **Reused tickers:** when two companies claim the same symbol on a date, the one whose own filings state it keeps it, but only when there is positive evidence. This fixed JCI (Tyco now resolves to TYC). It cannot fix CB, because ACE's 10-K never states its ticker.

5. **Validation** (`validate_top100.py` → `top100_validation.json`):
   - **PIT:** every float and filing used was filed by its date, and no date reaches the fence.
   - **Survivorship:** the 86 companies that later disappear stay in the pools while they are alive. The 2 that re-enter (Party City, Spire Global) did so only after filing again.
   - **Lineage:** 4 predecessor exclusions, no company counted twice.
   - **Identifiers:** after resolution, no symbol is claimed by two companies on the same date. But 5,127 pre-2019 pool rows still rely on today's SEC map.

6. **Exact Tiingo symbol requirement** (in `top100_sec_stage.json` → summary):

   | Set | Symbols | Already cached | Missing |
   |---|---|---|---|
   | first-try (every first symbol prices) | 494 | 64 | **430** |
   | contingency (deeper candidates and alternative symbols) | 698 | 67 | 631 |

## Why G2 and G3 cannot pass from SEC data alone

An exchange ticker at a date is not the same thing as the vendor's key for that security's price history:
- **Renamed companies:** Tiingo files their history under the *current* symbol (BB&T is under TFC, Coach under TPR).
- **Reused tickers:** "CB" was Chubb Corp's ticker until 2016, and is now ACE's (renamed Chubb Ltd).

So whether a symbol identifies the right company can only be confirmed on the vendor side, against Tiingo metadata or prices. The builder's float-to-market-cap check catches gross mismatches but not two large companies of similar size. The sample's five contradictions were:
- BB&T→TFC, Welltower HCN→WELL and Coach COH→TPR (renames, probably fine for price lookup);
- Equity Residential→"VMRK" and Marsh & McLennan→"MRSH" (wrong symbols from today's map).

## Decisions needed (human)

1. **Tiingo plan (G4):** log in to the account's usage page (tiingo.com/account/api/usage) and record the plan, the symbols used this month and the bandwidth used. Nothing will be bought or changed without your approval.
2. **Approve the price download (G3):** about 430 first-try symbols, about 0.73 GB and about 10 hours at 50 requests per hour. On the free plan this fits in October with little room to spare, and the contingency set does not fit. A request start date of 2009-01-01 instead of the full history would cut bandwidth by roughly half.
3. **Identifier verification (G2), after (2):**
   - check each (company, date) symbol against its Tiingo price history (start and end dates, float-to-market-cap ratio, and continuity with the company's filed shares), and add curated lineage rows to `ticker_history.csv` for confirmed renames and reuses (ACE/CB, BB&T/TFC, Welltower, Coach, Williams Partners);
   - rerun `verify_symbols_sample.py`, which must show 0 contradictions.
4. **Durable storage (advisory):** attach a private volume, or accept that a new session must re-download the cache.
