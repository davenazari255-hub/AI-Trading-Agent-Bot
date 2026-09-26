# Mooo

Mooo is an autonomous AI trading agent for Bybit V5 USDT linear perpetual futures.
The project plan (requirements, blueprints, work orders) lives in Software Factory.

## Safety defaults

* Bybit **Demo Trading** is the default environment (`BYBIT_ENV=demo`).
* **Live Trading is disabled** (`ALLOW_LIVE_TRADING=false`). Setting it to `true` only
  allows the Operator to enable Live in the dashboard. It never enables Live by itself.
* No news provider is enabled by default. With no provider, the system reports
  NEWS DATA UNAVAILABLE and never fabricates news.
* Never commit `.env`, API keys, or real trading credentials.

## Repository layout

| Path | Purpose |
|---|---|
| `backend/mooo_core` | Shared settings, defaults, Redis keys, database base |
| `backend/mooo_api` | FastAPI API Server |
| `backend/mooo_worker` | Agent Worker (supervised asyncio tasks) |
| `backend/tests` | Backend tests (pytest) |
| `backend/migrations` | Alembic migrations |
| `web/` | Next.js web client |
| `deploy/Caddyfile` | Reverse proxy and TLS |
| `docker-compose.yml` | caddy, web, api, worker, backtest-worker, postgres, redis, migrate |

## Quick start (local)

```bash
make env   # creates .env from .env.example with generated local secrets
make up    # builds and starts the full stack
```

Then open https://localhost (Caddy uses a local certificate).
Health: `https://localhost/api/v1/health`.

## Development

```bash
cd backend
python -m pip install -e ".[dev]"
cd ..
make check   # ruff, mypy, pytest
```

CI runs lint, type checks, tests, migrations, the web build, and a Docker Compose
smoke test on every push.
