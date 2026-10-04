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

## Current drafts (2026-10-04) — none approved

All drafted by AI (`status: draft`). None may be researched until a human approves it.

| Id | Edge | Feasibility |
|---|---|---|
| H-FUNDING-CROWDING | crowded perpetual positioning (extreme funding) | ready (funding archive cached to the fence via `trading.data.funding`) |
| H-VOL-SCALED-TREND | time-series trend, risk premium | needs_data (daily cross-asset) |
| H-FUNDING-CARRY | delta-neutral funding carry | needs_infrastructure (two legs) |
| H-XS-MOMENTUM | cross-sectional crypto momentum | needs_infrastructure (portfolio, survivorship-free universe) |
| H-PAIRS-STATARB | cointegrated pairs reversion | needs_infrastructure |
| H-MARKET-MAKING | spread capture vs inventory risk | blocked (L2 data is paid) |
| H-OB-IMBALANCE | order-book imbalance | blocked (L2 data is paid) |
| H-CROSS-EXCHANGE | cross-venue dislocations | blocked (multi-venue L2 + accounts) |
