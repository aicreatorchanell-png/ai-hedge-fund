# Active trading: data phase (2026-10-03)

The data is loaded and the strategy families are prepared. **No strategy has been backtested on real data**
apart from timing runs, whose results were not inspected. Nothing is assumed to be profitable.

## Data

All of it is stored in the private cache (`~/.hedge-fund/cache`, outside git).

### Crypto: Binance spot, 1-minute bars (`data/load_crypto.py`)

| Pair | Months | 1-minute bars | Missing |
|---|---|---|---|
| BTCUSDT | 104 (2018-01 to 2026-08) | 4,549,535 | 0.20% |
| ETHUSDT | 104 | 4,549,534 | 0.20% |
| LTCUSDT | 104 | 4,549,534 | 0.20% |
| BNBUSDT | 104 | 4,549,534 | 0.20% |
| ADAUSDT | 101 (listed 2018-04) | 4,398,836 | 0.13% |
| XRPUSDT | 100 (listed 2018-05) | 4,374,108 | 0.13% |

- **Checks:** every archive's checksum is verified against Binance's published sha256. There are no duplicate bars and no OHLC errors. Missing bars are exchange outages and stay missing, with no forward fill.
- **Extreme moves:** 12 one-minute moves above 15% across XRP, LTC and ADA; these are real flash moves.
- **Cross-check against a second exchange** (`data/crosscheck_crypto.py`, CCXT, OKX public data): 42 random 5-hour windows were compared, about one per pair and year.
  - 42 windows had an overlap. Of these, 38 pass, with a median close difference of 0.8–2.4 bp and a median 1-minute return correlation of 0.80–0.97.
  - The 4 that fail are all 2018–2019 windows. Price differences stay under 50 bp, but the 1-minute returns correlate poorly, which points to thin markets on OKX at the time rather than a data error.
  - One further 2018 XRP window had no OKX data to compare.
- **Selection rule:** the six pairs were fixed before any test. They are USDT pairs listed on Binance by 2018-05 whose coins were top-10 by market capitalization in January 2018. This rule is not chosen with hindsight of later winners.
- **Fence:** downloads stop at 2026-08-31, before the sealed holdout's fence of 2026-09-01.

### FX and indices: Dukascopy (`data/load_dukascopy.py`)

- Mid-price bars are built from BID and ASK candles, and the spread is measured each month.
- From this environment the datafeed returns HTTP 429/503 most of the time. A slow, resumable pilot is running (EURUSD and USA500IDXUSD, 2024-Q1). Full histories have to be trickled in over days.

### Stocks and ETFs

- Daily Tiingo bars already exist from Phase B.
- Intraday stock data is not loaded: Tiingo IEX intraday would use the monthly symbol budget.

### Futures

- No free source is available, and contract specifications and rolls are not implemented.

## Cost model (`hedge_fund/trading/data/markets.py`)

Bars carry no spread, so costs are charged as fees on every fill. A one-tick adverse slip is added to every fill on top of these fees.

| Market | Maker (targets) | Taker (market and stop orders) |
|---|---|---|
| Crypto (BTC, ETH) | 10 bp | 13 bp: commission 10 + half-spread 1 + slippage 2 |
| Crypto (other pairs) | 10 bp | 15 bp |
| FX | — | 1.2 bp |
| Index CFDs | — | 2 bp |

A cost-stress run at 2× must also pass the gates.

## Strategy families (`hedge_fund/trading/families.py`)

| Family | Style | Grid |
|---|---|---|
| donchian_breakout | trend | 16 |
| ema_trend | trend | 16 |
| tsmom | momentum | 12 |
| volatility_breakout | expansion | 16 |
| bollinger_reversion | mean reversion | 16 |
| opening_range | session | 8 |

- That is 84 configurations per instrument.
- Signals are computed on 5-minute, 15-minute or 1-hour bars aggregated from 1-minute bars. Fills, stop-losses and take-profits are simulated on 1-minute bars, and every entry fills at the next bar's open.
- Every family is tested for look-ahead: changing future bars does not change past decisions.

## Research plan (`research/plan_crypto_v1.json`, frozen, not run)

- **Scope:** 6 families × 6 pairs = 504 configurations.
- **Development window:** 2018-07-01 to 2025-08-31.
- **Walk-forward:** 24 months of training and 6 months of testing, stepped every 6 months, giving 10 folds. Each fold's configuration is chosen on training data only.
- **Statistics:** deflated Sharpe ratio, using the trial count from the registry; PBO; a bootstrap confidence interval; out-of-sample drawdown; and the share of positive folds. All are evaluated with the frozen gates (`configs/validation-gates.yaml`).
- **Cost stress:** 2×.
- **Combinations:** per family across pairs, and all family-pair lines together. Each member is chosen on training data only.
- **Runtime:** about 22 s per instrument-year for each configuration, so about 10 hours on 4 cores.

## Proposal for a human decision: seal a final out-of-sample year

The plan reads data only up to 2025-08-31. The year from 2025-09-01 to 2026-08-31 has not been read by any research run. Sealing it would allow a single final evaluation of whatever survives walk-forward.

Editing `configs/holdouts.yaml` is a protected action that requires a human commit. The proposed entry:

```yaml
  - id: active-crypto-final
    start: "2025-09-01"
    end: "2026-08-31"
    embargo_days: 0
    status: sealed
    evaluation_mode: one_shot
    reason: Final out-of-sample year for the active-trading crypto plan (crypto-v1).
```
