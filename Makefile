# Bindery. The compose file lives in infra/, so every command needs --env-file
# to pick up the repo-root .env. That is the only reason this file exists.

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml

.PHONY: up down build logs ps migrate revision test test-pipeline integrity backup export mirror drill drill-offsite e2e lint-web lifecycle-check ocr-report seed-forms enqueue-stage reprocess shell psql create-user tunnel

build:            ## build all images
	$(COMPOSE) build

up:               ## start the stack (no tunnel; add `make tunnel` for ingress)
	$(COMPOSE) up -d

tunnel:           ## start the stack including Cloudflare Tunnel ingress
	$(COMPOSE) --profile tunnel up -d

down:
	$(COMPOSE) down

ps:               ## the PORTS column must be empty for every service (REQ-104)
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f

migrate:          ## apply migrations explicitly — never on container boot (REQ-114)
	$(COMPOSE) exec api alembic upgrade head

revision:         ## make a new migration: make revision m="add foo"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

test:             ## full suite against a throwaway database
	$(COMPOSE) --profile test run --rm test

test-pipeline:    ## OCR / pipeline / golden-corpus suites (needs the OCR toolchain)
	$(COMPOSE) --profile test run --rm test-worker

e2e:              ## end-to-end tests against the running stack (needs `make up`)
	$(COMPOSE) --profile e2e run --rm e2e

lint-web:         ## eslint + tsc for the web app
	cd web && npm run lint && npm run typecheck

lifecycle-check:  ## audit the offsite bucket's expiry rules (R-21: a bucket-wide rule deletes the archive)
	$(COMPOSE) exec api python -m api.cli lifecycle-check

integrity:        ## re-hash every original; run this BEFORE a backup, never after
	$(COMPOSE) exec api python -m api.export.cli integrity

backup:           ## integrity check, then pg_dump, then blobs — in that order (REQ-096)
	$(COMPOSE) exec api python -m api.export.cli backup

export:           ## full export: originals + static index, works with the stack stopped
	$(COMPOSE) exec api python -m api.export.cli export

mirror:           ## rebuild the browsable folder tree (safe: it is derived, not source)
	$(COMPOSE) exec api python -m api.export.cli mirror

drill:            ## THE deliverable: restore to a clean database and find the DD-214
	scripts/restore-drill.sh $(b)

drill-offsite:    ## the same, from S3 alone — no local backup, no blob pool, no live stack
	scripts/restore-drill.sh --from-s3 $(term)

ocr-report:       ## score OCR word accuracy on the golden corpus (REQ-018, the R-01 gate)
	$(COMPOSE) --profile test run --rm test-worker \
	  python -m pytest tests/test_ocr_accuracy.py -m slow -s

shell:
	$(COMPOSE) exec api bash

psql:
	$(COMPOSE) exec postgres psql -U bindery -d bindery

seed-forms:       ## load the known-form registry from api/forms/seed/*.yaml
	$(COMPOSE) exec api python -m api.cli seed-forms

enqueue-stage:    ## re-run a stage over every file: make enqueue-stage stage=segment
	$(COMPOSE) exec api python -m api.cli enqueue-stage "$(stage)"

reprocess:        ## re-classify documents left on an older prompt: make reprocess prompt=v1
	$(COMPOSE) exec api python -m api.cli reprocess --prompt-version "$(prompt)"

create-user:      ## make create-user email=you@example.com library=Household
	$(COMPOSE) exec api python -m api.cli create-user --email "$(email)" --library "$(library)"

screenshots:      ## capture documentation screenshots from a running stack
	@BINDERY_URL=$${BINDERY_URL:-http://localhost:8080} python3 scripts/capture-screens.py
