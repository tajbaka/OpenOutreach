.DEFAULT_GOAL := help
.PHONY: help attach test docker-test stop build up up-view install setup run supervise run-awake admin view selfhost-db-prepare selfhost-db-up selfhost-db-stop selfhost-db-logs selfhost-db-restore-copy

help:
	@perl -nle'print $& if m{^[a-zA-Z_-]+:.*?## .*$$}' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

install: ## install all Python dependencies (local dev)
	pip install uv 2>/dev/null || true
	uv pip install -r requirements/local.txt

setup: install ## install deps + Playwright browsers + migrate + bootstrap CRM
	playwright install --with-deps chromium
	python manage.py migrate --no-input
	python manage.py setup_crm

run: ## run the daemon
	python manage.py

supervise: ## run daemon under terminal supervisor with git auto-update
	python daemon_supervisor.py

run-awake: ## run the daemon on macOS without system sleep
	@if command -v caffeinate >/dev/null 2>&1; then \
		caffeinate -ims python manage.py; \
	else \
		echo "run-awake requires macOS caffeinate; use OS power settings or run: python manage.py"; \
		exit 1; \
	fi

test: ## run the test suite
	.venv/bin/pytest

admin: ## start the Django Admin web server
	@echo ""
	@echo "  Django Admin: http://localhost:8000/admin/"
	@echo "  No superuser yet? Run: python manage.py createsuperuser"
	@echo ""
	python manage.py runserver

# Docker targets
attach: ## follow the logs of the service
	docker compose -f local.yml logs -f

docker-test: ## run tests in Docker
	docker compose -f local.yml run --remove-orphans app py.test -vv -p no:cacheprovider

stop: ## stop all services defined in Docker Compose
	docker compose -f local.yml stop

build: ## build all services defined in Docker Compose
	docker compose -f local.yml build

up: ## run the defined service in Docker Compose
	docker compose -f local.yml up --build

up-view: ## run the defined service in Docker Compose and open vinagre
	docker compose -f local.yml up --build -d
	sleep 3
	$(MAKE) view
	docker compose -f local.yml logs -f app

view: ## open vinagre to view the app
	@sh -c 'vinagre vnc://127.0.0.1:5900 > /dev/null 2>&1 &'

selfhost-db-prepare: ## generate local-only self-hosted Postgres secrets/certs
	scripts/selfhost_postgres_prepare.sh

selfhost-db-up: ## start local self-hosted Postgres test DB on 127.0.0.1:55432
	docker compose -f compose/selfhost-postgres.yml up -d

selfhost-db-stop: ## stop local self-hosted Postgres test DB
	docker compose -f compose/selfhost-postgres.yml stop

selfhost-db-logs: ## follow local self-hosted Postgres logs
	docker compose -f compose/selfhost-postgres.yml logs -f postgres

selfhost-db-restore-copy: ## restore current Neon DATABASE_URL into local self-host test DB
	scripts/restore_neon_to_selfhost_test.sh --confirm-reset-local
