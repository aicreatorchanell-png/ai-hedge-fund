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

There are no hypotheses yet: none has been written or approved.
