SHELL := /bin/bash
COMPOSE ?= docker compose
BACKEND := backend

.DEFAULT_GOAL := help

.PHONY: help env up down restart build logs ps migrate install lint fmt typecheck test check

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

env: ## Create .env from .env.example with generated secrets (keeps an existing .env)
	@if [ -f .env ]; then \
		echo ".env exists; leaving it unchanged"; \
	else \
		cp .env.example .env; \
		key="$$(openssl rand -base64 32)"; \
		pw="$$(openssl rand -hex 24)"; \
		sed -i.bak -e "s|^MOOO_MASTER_KEY=.*|MOOO_MASTER_KEY=$$key|" -e "s|change-me-postgres-password|$$pw|g" .env; \
		rm -f .env.bak; \
		chmod 600 .env; \
		echo "Created .env with a generated MOOO_MASTER_KEY and database password"; \
	fi

up: env ## Build and start the full stack
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

build: ## Build all images
	$(COMPOSE) build

logs: ## Follow service logs
	$(COMPOSE) logs -f --tail=200

ps: ## Show service status and health
	$(COMPOSE) ps

migrate: ## Run database migrations once
	$(COMPOSE) run --rm migrate

install: ## Install backend with dev tools into the active Python 3.12 environment
	cd $(BACKEND) && python -m pip install -e ".[dev]"

lint: ## Run ruff
	cd $(BACKEND) && ruff check .

fmt: ## Format backend code with ruff
	cd $(BACKEND) && ruff format . && ruff check --fix .

typecheck: ## Run mypy
	cd $(BACKEND) && mypy mooo_core mooo_api mooo_worker

test: ## Run pytest
	cd $(BACKEND) && pytest

check: lint typecheck test ## Run lint, type-check, and tests
