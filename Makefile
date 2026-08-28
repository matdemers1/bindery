# Bindery. The compose file lives in infra/, so every command needs --env-file
# to pick up the repo-root .env. That is the only reason this file exists.

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml

.PHONY: up down build logs ps migrate revision test test-pipeline ocr-report seed-forms enqueue-stage reprocess shell psql create-user tunnel

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
