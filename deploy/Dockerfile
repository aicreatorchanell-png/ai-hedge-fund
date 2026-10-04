# Paper/testnet runtime for hedge_fund.paper (NautilusTrader). PAPER / SIMULATION ONLY.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 POETRY_VIRTUALENVS_CREATE=false AIHF_CACHE_DIR=/data/cache
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir poetry==1.8.3
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main
COPY . .
RUN poetry install --only main
# Real money is impossible by construction: the guard refuses live execution clients,
# and only AIHF_PAPER_* credentials (testnet/demo) are ever read.
ENTRYPOINT ["python", "-m", "hedge_fund.paper.supervisor"]
CMD ["--mode", "sandbox_kraken_futures", "--plumbing-check", "--deployment", "runs/active/paper/deployment_experimental.yaml"]
