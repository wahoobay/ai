.PHONY: help up down build rebuild ps logs psql worker-logs dashboard-logs poller-logs eval test

PY ?= python

help:
	@echo "Wahoo Bay — common make targets"
	@echo ""
	@echo "  Containerised stack (docker compose):"
	@echo "    make up              start postgres + worker + dashboard + poller"
	@echo "    make down            stop the stack (volumes preserved)"
	@echo "    make build           rebuild service images"
	@echo "    make rebuild         build --no-cache"
	@echo "    make ps              service status"
	@echo "    make logs            tail logs from all services"
	@echo "    make worker-logs     tail just the worker"
	@echo "    make dashboard-logs  tail just the dashboard"
	@echo "    make poller-logs     tail just the poller"
	@echo "    make psql            open a psql shell in the postgres container"
	@echo ""
	@echo "  Bare-metal dev (current DGX flow, unchanged):"
	@echo "    ./scripts/dev/start_postgres.sh  + ./scripts/dev/run_worker.sh etc."
	@echo ""
	@echo "  Eval / tests:"
	@echo "    make eval            run the eval harness over eval/manifest.json"
	@echo "    make test            pytest the dashboard + eval modules"

# ----- containerised stack ----------------------------------------------

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

rebuild:
	docker compose build --no-cache

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100

worker-logs:
	docker compose logs -f --tail=200 worker

dashboard-logs:
	docker compose logs -f --tail=200 dashboard

poller-logs:
	docker compose logs -f --tail=200 sensestream-poller

psql:
	docker compose exec postgres psql -U wahoobay -d wahoobay

# ----- eval / tests -----------------------------------------------------

eval:
	$(PY) -m eval.run --manifest eval/manifest.json --out eval/reports $(if $(CLIPS),--clips $(CLIPS))

# placeholder — once we add unit tests (stats module first)
test:
	$(PY) -m pytest -q services/dashboard eval
