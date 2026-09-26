COMPOSE ?= docker compose

.PHONY: env up down logs ps build migrate lint typecheck test check

env:
	python3 scripts/init_env.py

up: env
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

build:
	$(COMPOSE) build

migrate:
	$(COMPOSE) run --rm migrate

lint:
	cd backend && ruff check .

typecheck:
	cd backend && mypy

test:
	cd backend && pytest -q

check: lint typecheck test
