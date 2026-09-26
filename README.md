# Mooo

Autonomous AI trading agent for Bybit V5 linear perpetual futures.

Mooo runs as a Docker Compose stack:

| Service | Purpose |
| --- | --- |
| `caddy` | TLS reverse proxy. Routes `/api/*` and `/ws/*` to `api`, everything else to `web`. |
| `web` | Next.js dashboard (placeholder page in this stage). |
| `api` | FastAPI API Server (`backend/mooo_api`). |
| `worker` | Agent Worker in the `live` role. Holds the Redis lock `mooo:worker:lock`. |
| `backtest-worker` | Agent Worker in the `backtest` role. Never takes the live lock. |
| `postgres` | PostgreSQL 16, system of record. |
| `redis` | Redis 7, event bus, cache, and coordination locks. |
| `migrate` | One-shot `alembic upgrade head` that runs before the backend services start. |

## Repository layout

```
backend/
  mooo_core/     shared config (typed Settings, Deployment Ceilings, defaults)
  mooo_api/      FastAPI API Server
  mooo_worker/   Agent Worker (TaskSupervisor, worker lock, health endpoint)
  migrations/    Alembic migrations
  tests/         pytest suite
web/             Next.js client
deploy/Caddyfile
.env.example     every configuration key with safe defaults
Makefile
docker-compose.yml
```

## Quick start (local)

```bash
make env   # creates .env from .env.example and generates MOOO_MASTER_KEY and the database password
make up    # builds and starts the full stack on https://localhost
make ps    # shows service health
make logs  # follows logs
make down  # stops the stack
```

`.env` is never committed. The services fail fast at startup if a setting is invalid. Error output names the setting but never prints its value.

### Safety defaults

* `BYBIT_ENV=demo`. It sets only the initial Active Environment.
* `ALLOW_LIVE_TRADING=false`. `BYBIT_ENV=live` is refused at startup unless this is `true`. Live still requires Live Trading Enablement in the dashboard.
* `NEWS_PROVIDERS` is empty. No news provider is enabled by default.
* `MOOO_MASTER_KEY` must encode exactly 32 bytes (base64 or hex). Generate one with `openssl rand -base64 32`.
* Deployment Ceilings (`CEILINGS__*`) bound what the dashboard can set. A ceiling cannot be looser-than-default in the wrong direction: startup fails if a ceiling is below the initial conservative default it bounds.

## Backend development

```bash
make install    # pip install -e "backend[dev]" into the active Python 3.12 environment
make lint       # ruff
make typecheck  # mypy
make test       # pytest
make check      # all three
```

## VPS deployment

Ubuntu 24.04 with Docker Engine. Allow only ports 22, 80, and 443 in UFW. Set `MOOO_DOMAIN` in `.env` to the public domain so Caddy obtains a certificate automatically. Schedule a nightly `pg_dump` of the `postgres` service.
