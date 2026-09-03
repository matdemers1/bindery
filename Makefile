# Bindery. The compose file lives in infra/, so every command needs --env-file
# to pick up the repo-root .env. That is the only reason this file exists.

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml

.PHONY: help up down build lock logs ps migrate revision test test-pipeline integrity backup export mirror drill drill-offsite e2e lint-web typecheck contract lifecycle-check ocr-report seed-forms enqueue-stage reprocess shell psql create-user tunnel screenshots

build:            ## build all images
	$(COMPOSE) build

# Every target below carrying a `## …` comment is listed by `make help`. The
# comments were there for eleven phases with nothing rendering them, while the
# README told the reader this file was twenty lines long and to go and read it
# (CR-083). Four lines of awk is cheaper than either of those being wrong again.
help:             ## list every command in this file
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z][a-zA-Z0-9_-]*:.*## / \
	  {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# `make lock` freezes, it does not resolve. The point of requirements.lock is
# that what is pinned is what was tested, so this reads versions out of a built
# image rather than asking PyPI what it would install today. The worker's `dev`
# target is the one image carrying every group — base, the OCR extra and the
# test toolchain — so its site-packages is the union of all three.
#
# Run it whenever pyproject.toml changes, in the same commit, and read the diff:
# it is the only place a dependency upgrade is visible.
lock:             ## regenerate requirements.lock from a freshly built image
	$(COMPOSE) --profile test build test-worker
	@sed -n '/^[^#]/q;p' requirements.lock > requirements.lock.tmp
	@$(COMPOSE) --profile test run --rm --no-deps -T test-worker \
	  python -m pip freeze --exclude-editable | tr -d '\r' | sort >> requirements.lock.tmp
	@mv requirements.lock.tmp requirements.lock
	@echo 'requirements.lock regenerated. Review the diff, then run: make test'

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

# The api and worker are annotated — 95% of functions carry a return type and
# there is not one `# type: ignore` — and until CR-089 nothing read a line of it.
# The gate is a ratchet rather than a clean run: see scripts/typecheck.py for
# why, and CLAUDE.md's Conventions section for the posture this project has
# actually chosen, which was the part that was missing.
typecheck:        ## mypy over api/ and worker/, against the checked-in baseline
	$(COMPOSE) --profile test run --rm --no-deps test python scripts/typecheck.py

# The wire contract is written twice by hand. `tsc --noEmit` proves the client
# agrees with itself; this is what proves it agrees with api/schemas.py (CR-059).
contract:         ## check web/src/api.ts against the server's response models
	$(COMPOSE) --profile test run --rm --no-deps test python scripts/check_api_contract.py

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

# Capture runs *inside* the compose network, sharing the web container's network
# namespace, and there is no host-side alternative: no service publishes a port
# (REQ-104), so `http://localhost:8080` on the host reaches nothing, and the
# session cookie is `Secure`, so a browser reaching the app over plain
# `http://web` discards it silently — the login succeeds, everything after it
# 401s, and the screen sits on the login form looking like a wrong password.
#
# This target used to run the script on the host against that unreachable port
# (CR-081), which meant the one command CLAUDE.md gives for a workflow that
# gates CI could not work, and the invocation that does work existed only as a
# comment in infra/docker-compose.yml.
#
# BINDERY_PASSWORD must be in .env or the environment; the compose service
# passes it through.
screenshots:      ## capture documentation screenshots (needs `make up`)
	$(COMPOSE) --profile screenshots run --rm screenshots
