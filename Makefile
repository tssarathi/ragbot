.DEFAULT_GOAL := help

env-val = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null)

FRAPPE_BRANCH     ?= version-16
FRAPPE_DOCKER     ?= .build/frappe_docker
FRAPPE_DOCKER_REF ?= a0c5213
IMAGE             ?= $(or $(call env-val,CUSTOM_IMAGE),frappe-demo)
TAG               ?= $(or $(call env-val,CUSTOM_TAG),16)
BUILD_FLAGS       ?=

AGENT_REPO   ?= .build/frappe-ai-agent
AGENT_REF    ?= 777ca69
MCP_REPO     ?= .build/frappe-mcp-server
MCP_REF      ?= 06ab2e3

SITE             ?= $(or $(call env-val,SITE_NAME),demo.localhost)
ADMIN_PASSWORD   ?= $(or $(call env-val,ADMIN_PASSWORD),admin)
DB_ROOT_PASSWORD ?= $(or $(call env-val,DB_PASSWORD),123)
AI_MODEL         ?= $(or $(call env-val,AI_MODEL),gemma4:26b)
EMBED_MODEL      ?= $(or $(call env-val,EMBED_MODEL),nomic-embed-text)
AGENT_URL        ?= http://agent:8484

FY               := $(shell date +%Y-%m | awk -F- '{y = $$2<7 ? $$1-1 : $$1; print y"-07-01 "y+1"-06-30"}')
SETUP_DEMO       ?= 1
ERRORS_BEFORE    := /tmp/ragbot-errors-before

COMPOSE = docker compose --project-name frappe-demo --project-directory . --env-file .env \
  -f $(FRAPPE_DOCKER)/compose.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.mariadb.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.redis.yaml \
  -f compose.demo.yaml

# A clone rule only fires when its directory is missing, so the ref is checked out here
# instead: otherwise a stale tree builds silently and the pin above means nothing.
define at-ref
@git -C $(1) cat-file -e $(2)^{commit} 2>/dev/null || git -C $(1) fetch --quiet origin
@git -C $(1) diff --quiet && git -C $(1) diff --cached --quiet \
  || { echo '$(1) has uncommitted changes; commit or stash them first' >&2; exit 1; }
@git -C $(1) checkout --quiet --detach $(2)
endef

setup: ## Build everything and bring the whole demo up from scratch
	$(MAKE) image agent-image mcp-image
	$(MAKE) up site

help: ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

$(FRAPPE_DOCKER):
	git clone --quiet https://github.com/frappe/frappe_docker.git $@
	git -C $@ checkout --quiet $(FRAPPE_DOCKER_REF)

$(AGENT_REPO):
	git clone --quiet https://github.com/vyogotech/frappe-ai-agent.git $@
	git -C $@ checkout --quiet $(AGENT_REF)

$(MCP_REPO):
	git clone --quiet https://github.com/vyogotech/frappe-mcp-server.git $@
	git -C $@ checkout --quiet $(MCP_REF)

.env:
	cp .env.example .env

image: $(FRAPPE_DOCKER) ## Build the image with erpnext, drive and frappe_ai
	docker build $(BUILD_FLAGS) \
	  --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
	  --build-arg=FRAPPE_BRANCH=$(FRAPPE_BRANCH) \
	  --build-arg=CACHE_BUST=$$(cksum apps.json | cut -d' ' -f1) \
	  --secret=id=apps_json,src=apps.json \
	  --tag=$(IMAGE):$(TAG) \
	  --file=$(FRAPPE_DOCKER)/images/layered/Containerfile \
	  $(FRAPPE_DOCKER)

agent-image: $(AGENT_REPO) ## Build the AI agent image
	$(call at-ref,$(AGENT_REPO),$(AGENT_REF))
	docker build $(BUILD_FLAGS) --tag=frappe-ai-agent:local $(AGENT_REPO)

mcp-image: $(MCP_REPO) ## Build the MCP server image
	$(call at-ref,$(MCP_REPO),$(MCP_REF))
	docker build $(BUILD_FLAGS) --tag=frappe-mcp-server:local --file=mcp/Dockerfile $(MCP_REPO)

model: ## Check the configured models are present on the host
	@for m in $(AI_MODEL) $(EMBED_MODEL); do \
	  case $$m in *:*) ;; *) m="$$m:latest";; esac; \
	  find "$(HOME)/.ollama/models/manifests" -type f -path "*/$$(echo $$m | tr : /)" 2>/dev/null \
	    | grep -q . || { \
	    echo "$$m is not on this host." >&2; \
	    echo "The stack mounts ~/.ollama/models read-only and cannot download it." >&2; \
	    echo "Run: ollama pull $$m" >&2; exit 1; }; \
	done

up: $(FRAPPE_DOCKER) .env model ## Start the stack
	$(COMPOSE) up -d

down: ## Stop the stack, keep the data
	$(COMPOSE) down

destroy: ## Stop the stack and delete the data
	$(COMPOSE) down --volumes

site: ## Create the demo site, run setup and wire the agent
	@$(COMPOSE) exec backend bench new-site "$(SITE)" \
	  --mariadb-user-host-login-scope=% \
	  --db-root-password $(DB_ROOT_PASSWORD) \
	  --admin-password $(ADMIN_PASSWORD) \
	  --install-app erpnext --install-app drive --install-app frappe_ai --install-app rag \
	  --set-default
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" set-config frappe_ai_agent_url "$(AGENT_URL)"
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" set-config -p frappe_ai_agent_url_unsafe_ok 1
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" set-config allow_tests true
	@$(COMPOSE) exec -T backend bench pip install --quiet -e apps/rag
	@$(MAKE) wizard

wizard: ## Complete the ERPNext setup wizard (idempotent)
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" execute \
	  frappe.desk.page.setup_wizard.setup_wizard.setup_complete \
	  --kwargs '{"args": {"language":"English","country":"Australia","timezone":"Australia/Adelaide","currency":"AUD","company_name":"L2X Technologies Pty Ltd","company_abbr":"L2X","chart_of_accounts":"Australia - Chart of Accounts with Account Numbers","fy_start_date":"$(word 1,$(FY))","fy_end_date":"$(word 2,$(FY))","setup_demo":$(SETUP_DEMO)}}' >/dev/null
	@test -n "$$($(COMPOSE) exec -T backend bench --site "$(SITE)" execute frappe.db.count --kwargs '{"dt":"Fiscal Year"}')" \
	  || { echo 'wizard: no fiscal year created, setup failed silently' >&2; exit 1; }
	@test -n "$$($(COMPOSE) exec -T backend bench --site "$(SITE)" execute frappe.db.count --kwargs '{"dt":"Company"}')" \
	  || { echo 'wizard: no company created, setup failed silently' >&2; exit 1; }
	@test -n "$$($(COMPOSE) exec -T backend bench --site "$(SITE)" execute frappe.db.count --kwargs '{"dt":"Account","filters":{"company":"L2X Technologies Pty Ltd"}}')" \
	  || { echo 'wizard: company has no accounts, chart of accounts name is wrong' >&2; exit 1; }
	@echo 'setup wizard complete'

check: ## Run the quality gates: lint, tests, index health, no new errors
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" execute frappe.db.sql \
	  --kwargs '{"query":"SELECT COUNT(*) AS n FROM `tabError Log`","as_dict":True}' | grep -o '[0-9]\+' > $(ERRORS_BEFORE)
	@ruff check rag/
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" run-tests --app rag
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" execute rag.ingest.health \
	  | tee /dev/stderr | grep -q '"problems": \[\]' \
	  || { echo 'check: the index has problems, see above' >&2; exit 1; }
	@if $(COMPOSE) exec -T backend bench --site "$(SITE)" execute rag.ingest.health | grep -q '"chunks": 0'; \
	  then echo 'check: nothing is indexed, so the integration tests only skipped' >&2; exit 1; fi
	@after=$$($(COMPOSE) exec -T backend bench --site "$(SITE)" execute frappe.db.sql \
	  --kwargs '{"query":"SELECT COUNT(*) AS n FROM `tabError Log`","as_dict":True}' | grep -o '[0-9]\+'); before=$$(cat $(ERRORS_BEFORE)); \
	  test "$$before" = "$$after" \
	  || { echo "check: this run logged $$((after - before)) error(s), see the Error Log" >&2; exit 1; }
	@$(COMPOSE) exec -T -e PW="$(ADMIN_PASSWORD)" \
	  backend python -c "import json, os, urllib.request as u; \
	  base = 'http://frontend:8080'; \
	  web = u.build_opener(u.HTTPCookieProcessor()); \
	  web.open(u.Request(base + '/api/method/login', \
	    json.dumps({'usr': 'Administrator', 'pwd': os.environ['PW']}).encode(), \
	    {'Content-Type': 'application/json'})); \
	  rows = json.load(web.open(base + '/api/method/rag.search.search?query=leave&limit=1'))['message']; \
	  assert rows, 'the endpoint answered but matched nothing'; \
	  assert set(rows[0]) >= {'file', 'seq', 'content', 'distance'}, rows[0]" \
	  || { echo 'check: the search endpoint the mcp tool calls is broken, see above' >&2; exit 1; }
	@$(COMPOSE) exec -T backend mariadb-dump --print-defaults | grep -q hex-blob \
	  || { echo 'check: mariadb-client.cnf is being ignored, so a backup would zero every vector' >&2; exit 1; }
	@echo 'check: all gates passed'

apps: ## List the apps in the image
	@docker run --rm --entrypoint sh $(IMAGE):$(TAG) -c 'ls -1 apps'

bench: ## Run a bench command, e.g. make bench ARGS="--site demo.localhost list-apps"
	@$(COMPOSE) exec -T backend bench $(ARGS)

logs: ## Follow the logs
	$(COMPOSE) logs -f

shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend bash

.PHONY: setup help model wizard image agent-image mcp-image up down destroy site check apps bench logs shell
