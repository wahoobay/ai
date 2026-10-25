.PHONY: help up up-gpu up-cpu migrate seed down build ps logs worker-logs dashboard-logs poller-logs psql eval test lint

PY ?= python
COMPOSE = docker compose
ALL_PROFILES = --profile web --profile gpu --profile cpu

help:
	@echo "Wahoo Bay — common make targets"
	@echo ""
	@echo "  Containerised stack (docker compose; profiles web / gpu / cpu):"
	@echo "    make up              postgres + migrations + dashboard + poller (no GPU, no AWS)"
	@echo "    make seed            load the committed sample dataset (data/sample) into an empty database"
	@echo "    make up-gpu          ... plus the worker on the host's NVIDIA GPU"
	@echo "    make up-cpu          ... plus the worker's CPU build over data/test_videos (slow)"
	@echo "    make migrate         bring the database schema to the current revision (alembic upgrade head)"
	@echo "    make down            stop the stack (volumes preserved)"
	@echo "    make build           build the web images (the worker builds when its profile starts)"
	@echo "    make ps              service status"
	@echo "    make logs            tail logs from all running services"
	@echo "    make worker-logs     tail just the worker (either flavour)"
	@echo "    make dashboard-logs  tail just the dashboard"
	@echo "    make poller-logs     tail just the poller"
	@echo "    make psql            open a psql shell in the postgres container"
	@echo ""
	@echo "  Bare-metal dev (current DGX flow, unchanged):"
	@echo "    ./scripts/dev/start_postgres.sh  + ./scripts/dev/run_worker.sh etc."
	@echo ""
	@echo "  Checks (what CI runs):"
	@echo "    make test            pytest (TEST_DATABASE_URL=... for the database tests)"
	@echo "    make lint            ruff check + ruff format --check"
	@echo "    make eval            run the eval harness over eval/manifest.json"

# ----- containerised stack ----------------------------------------------

up: migrate
	$(COMPOSE) --profile web up -d

up-gpu: migrate
	$(COMPOSE) --profile web --profile gpu up -d

up-cpu: migrate
	$(COMPOSE) --profile web --profile cpu up -d

migrate:
	$(COMPOSE) --profile web run --rm migrate

# The dashboard image runs the seed against the compose database; the sample and
# the frames directory are mounted in, so the frames land where the dashboard reads them.
seed:
	$(COMPOSE) --profile web run --rm -v ./data/sample:/data/sample:ro -v ./frames:/data/frames dashboard python -m app.seed --source file:///data/sample

down:
	$(COMPOSE) $(ALL_PROFILES) down

build:
	$(COMPOSE) --profile web build

ps:
	$(COMPOSE) $(ALL_PROFILES) ps

logs:
	$(COMPOSE) $(ALL_PROFILES) logs -f --tail=100

worker-logs:
	$(COMPOSE) --profile gpu --profile cpu logs -f --tail=200 worker worker-cpu

dashboard-logs:
	$(COMPOSE) --profile web logs -f --tail=200 dashboard

poller-logs:
	$(COMPOSE) --profile web logs -f --tail=200 sensestream-poller

psql:
	$(COMPOSE) --profile web exec postgres sh -c 'psql -U "$${POSTGRES_USER:-wahoobay}" -d "$${POSTGRES_DB:-wahoobay}"'

# ----- checks -------------------------------------------------------------

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

eval:
	$(PY) -m eval.run --manifest eval/manifest.json --out eval/reports $(if $(CLIPS),--clips $(CLIPS))
