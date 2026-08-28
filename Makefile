# Bindery. The compose file lives in infra/, so every command needs --env-file
# to pick up the repo-root .env. That is the only reason this file exists.

COMPOSE := docker compose --env-file .env -f infra/docker-compose.yml

.PHONY: up down build logs ps migrate revision test shell psql create-user tunnel

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

shell:
	$(COMPOSE) exec api bash

psql:
	$(COMPOSE) exec postgres psql -U bindery -d bindery

create-user:      ## make create-user email=you@example.com library=Household
	$(COMPOSE) exec api python -m api.cli create-user --email "$(email)" --library "$(library)"
