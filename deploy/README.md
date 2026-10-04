# 24/7 paper runtime (PAPER / SIMULATION ONLY — real money disabled)

What runs: `hedge_fund.paper.supervisor` with a NautilusTrader TradingNode on live public
Kraken Futures data and simulated fills (`sandbox_kraken_futures`), the EXPERIMENTAL /
UNVALIDATED `funding_crowding` deployment (`runs/active/paper/deployment_experimental.yaml`)
and the sandbox plumbing check. Hard limits: risk per trade, max order notional (risk
engine + sizing), total exposure cap, daily loss, drawdown kill switch, stale-data and
stale-funding guards, order-rate limit, health HALT (human reset only), `KILL_SWITCH` file.

Layers of recovery: Nautilus reconnects websockets; the supervisor rebuilds a crashed node
with backoff (5 s .. 300 s); Docker/systemd restart the supervisor; state (kill switch,
HALT latches, governor peaks) persists in the data volume.

Requirements: a Linux VM with 2 vCPU, 4 GB RAM, 20 GB disk, outbound HTTPS/WSS to
futures.kraken.com (no inbound ports), Docker 24+.

    git clone <repo> && cd ai-hedge-fund && git checkout claude/nifty-knuth-7wjfuv
    cp deploy/paper.env.example deploy/paper.env
    docker compose -f deploy/docker-compose.yml up -d --build
    docker compose -f deploy/docker-compose.yml logs -f paper
    docker compose -f deploy/docker-compose.yml run --rm dashboard      # live view

Stop new entries at once: `docker compose exec paper touch /data/cache/paper/futures/KILL_SWITCH`.
Testnet order routing instead of simulated fills needs testnet/demo keys in paper.env and
`--mode binance_futures_testnet` or `--mode kraken_futures_demo` (different deployment file).
