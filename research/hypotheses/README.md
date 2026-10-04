# Economic hypotheses

Every new strategy family needs an approved `H-<ID>.yaml` here before any research plan can run it (`hedge_fund/trading/hypothesis.py`).

## What a hypothesis must state

- **Why the edge exists:** the economic mechanism behind it.
- **Who is on the other side:** the counterparty.
- **Why they pay:** why that counterparty systematically loses or pays us.
- **Why it persists:** why arbitrage has not removed it.
- **When it disappears:** the conditions under which the edge should vanish.
- **Predictions:** consequences you can test.

## Rules

- **Approval:** AI may draft a hypothesis, but only a human can approve it (protected action `approve_hypothesis`).
- **No indicator-only reasons:** justifications built from indicator vocabulary alone are rejected.
- **No re-testing:** the six families of `plan_crypto_v1` were tested without such hypotheses and failed. They cannot be re-tested on the same instruments and development dates.

## Status (2026-10-04)

All drafted by AI. The owner approved H-FUNDING-CROWDING and H-VOL-SCALED-TREND for research and
validation; both were pre-registered, run and **failed** their gates. On 2026-10-04 the owner also approved
H-FUNDING-CARRY, H-XS-MOMENTUM and H-PAIRS-STATARB; all three failed too on development data (the sealed
holdout was not read). A failed candidate is not iterated on the same data. The others remain drafts.

| Tested | Plan | Verdict | Result |
|---|---|---|---|
| H-FUNDING-CROWDING | funding-crowding-v1 (48 configs) | FAIL | `runs/active/research/funding-crowding-v1/RESULT.md` |
| H-VOL-SCALED-TREND | vol-trend-v1 (72 configs) | FAIL | `runs/active/research/vol-trend-v1/RESULT.md` |
| H-FUNDING-CARRY | funding-carry-v1 (24 configs) | tested: FAIL | `runs/active/research/funding-carry-v1/RESULT.md` |
| H-XS-MOMENTUM | xs-momentum-v1 (4 configs) | tested: FAIL | `runs/active/research/xs-momentum-v1/RESULT.md` |
| H-PAIRS-STATARB | pairs-statarb-v1 (20 configs) | tested: FAIL | `runs/active/research/pairs-statarb-v1/RESULT.md` |

| Id | Edge | Feasibility |
|---|---|---|
| H-FUNDING-CROWDING | crowded perpetual positioning (extreme funding) | tested: FAIL |
| H-VOL-SCALED-TREND | time-series trend, risk premium | tested: FAIL |
| H-FUNDING-CARRY | delta-neutral funding carry | tested: FAIL |
| H-XS-MOMENTUM | cross-sectional crypto momentum | tested: FAIL |
| H-PAIRS-STATARB | cointegrated pairs reversion | tested: FAIL |
| H-MARKET-MAKING | spread capture vs inventory risk | blocked (L2 data is paid) |
| H-OB-IMBALANCE | order-book imbalance | blocked (L2 data is paid) |
| H-CROSS-EXCHANGE | cross-venue dislocations | blocked (multi-venue L2 + accounts) |
