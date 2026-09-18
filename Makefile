-include .env

PYTHON ?= .venv/bin/python
DB_URL ?= $(or $(PPBASE_DATABASE_URL),postgresql+asyncpg://ppbase:ppbase@localhost:55434/buma_master_ppbase)
DATA_DIR ?= $(or $(PPBASE_DATA_DIR),pb_data)
MIGRATIONS_DIR ?= $(or $(PPBASE_MIGRATIONS_DIR),pb_migrations)
HOST ?= $(or $(PPBASE_HOST),127.0.0.1)
PORT ?= $(or $(PPBASE_PORT),8090)

.PHONY: help install postgres-up postgres-down test lint format-check check start migrate-status migrate-up schema-verify complete-schema-verify view-verify verify-all

help:
	@echo "BUMA Master PPBase foundation"
	@echo "  make install       Install runtime and development dependencies"
	@echo "  make postgres-up   Start isolated PostgreSQL on port 55434"
	@echo "  make test          Run tests"
	@echo "  make check         Run tests, Ruff and format check"
	@echo "  make start         Start PPBase (applies pending migrations by default)"
	@echo "  make schema-verify Verify applied base/auth schema against the export"
	@echo "  make complete-schema-verify Verify base/auth collections and views against the export"
	@echo "  make view-verify   Validate translated view queries on PostgreSQL"
	@echo "  make verify-all    Run complete schema and view verification"

install:
	$(PYTHON) -m pip install -e ".[dev]"

postgres-up:
	docker compose up -d postgres

postgres-down:
	docker compose down

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check app tests scripts

format-check:
	$(PYTHON) -m ruff format --check app tests scripts

check: test lint format-check

start:
	BUMA_MASTER_ENABLE_INTERNAL_JOBS=$(BUMA_MASTER_ENABLE_INTERNAL_JOBS) \
	$(PYTHON) -m ppbase serve --db "$(DB_URL)" --dir "$(DATA_DIR)" \
	--hooks app.hooks.register:register --host "$(HOST)" --port "$(PORT)" \
	--migrations-dir "$(MIGRATIONS_DIR)"

migrate-status:
	$(PYTHON) -m ppbase migrate status --db "$(DB_URL)" --dir "$(MIGRATIONS_DIR)"

migrate-up:
	$(PYTHON) -m ppbase migrate up --db "$(DB_URL)" --dir "$(MIGRATIONS_DIR)"

schema-verify:
	$(PYTHON) scripts/verify_schema_baseline.py --db "$(DB_URL)"

complete-schema-verify:
	PPBASE_DATABASE_URL="$(DB_URL)" $(PYTHON) scripts/verify_complete_schema.py

view-verify:
	PPBASE_DATABASE_URL="$(DB_URL)" $(PYTHON) scripts/verify_view_queries.py

verify-all: complete-schema-verify view-verify
