# CLAUDE.md

This repo is the Wahoo Bay fish-identification system: the worker (detector + classifier on the camera stream), the FastAPI dashboard, the SenseStream poller and Postgres, run with `docker compose`. The platform it is moving onto (the AWS account, CDK stacks, pipelines, runbooks, the cost dashboard) is the sibling repository `wahoobay/infra`: its design spec (§4.5) describes where this system is going, and its README is the runbook.

## Ground rules

- **Production runs from the DGX checkout** at `/raid/scratch/dzimmerman2021/wahoobay`, 24/7, serving the public demo. Never `docker compose down`, rebuild, or migrate the database there casually. Development that needs containers uses a second checkout with its own `COMPOSE_PROJECT_NAME` and ports. See `docs/operations.md`.
- **No AWS credentials in this repo or on the DGX.** Code reaches AWS only through GitHub Actions and the OIDC roles defined in `infra`. Secrets live in `.env` (ignored) locally and in SSM or GitHub secrets for pipelines; never in code, commits, logs or chat.
- **Location is configuration.** Camera URLs, paths and site names come from configuration; nothing named "wahoobay" is hard-coded where a location parameter belongs, because the system is meant to run for other sites.
- **Before a pull request**: `ruff check .` and `pytest` (what CI runs), and `docker compose config` if compose changed. Model weights, frames, logs and exports never enter git (see `.gitignore`).
- Branch `reports-panel-and-weather` is the pending work to land first.
- Docs: `docs/architecture.md`, `docs/operations.md`, `docs/data_pipeline.md`, `docs/finetune_workflow.md`, `docs/reviewer_guide.md`. `PLAN.md` is the original DGX plan.
