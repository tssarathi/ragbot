.DEFAULT_GOAL := help

env-val = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null)

FRAPPE_BRANCH     ?= version-16
FRAPPE_DOCKER     ?= .build/frappe_docker
FRAPPE_DOCKER_REF ?= a0c5213
IMAGE             ?= $(or $(call env-val,CUSTOM_IMAGE),frappe-demo)
TAG               ?= $(or $(call env-val,CUSTOM_TAG),16)
BUILD_FLAGS       ?=

AGENT_REPO   ?= .build/frappe-ai-agent
MCP_REPO     ?= .build/frappe-mcp-server

SITE             ?= $(or $(call env-val,SITE_NAME),demo.localhost)
ADMIN_PASSWORD   ?= $(or $(call env-val,ADMIN_PASSWORD),admin)
DB_ROOT_PASSWORD ?= $(or $(call env-val,DB_PASSWORD),123)
AI_MODEL         ?= $(or $(call env-val,AI_MODEL),phi4:14b)
AGENT_URL        ?= http://agent:8484

FY               := $(shell date +%Y-%m | awk -F- '{y = $$2<7 ? $$1-1 : $$1; print y"-07-01 "y+1"-06-30"}')
SETUP_DEMO       ?= 1
MODEL_MANIFEST    = $(HOME)/.ollama/models/manifests/registry.ollama.ai/library/$(subst :,/,$(AI_MODEL))

COMPOSE = docker compose --project-name frappe-demo --project-directory . --env-file .env \
  -f $(FRAPPE_DOCKER)/compose.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.mariadb.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.redis.yaml \
  -f compose.demo.yaml

setup: ## Build everything and bring the whole demo up from scratch
	$(MAKE) image agent-image mcp-image
	$(MAKE) up site keys

help: ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

$(FRAPPE_DOCKER):
	git clone --quiet https://github.com/frappe/frappe_docker.git $@
	git -C $@ checkout --quiet $(FRAPPE_DOCKER_REF)

$(AGENT_REPO):
	git clone --quiet --depth 1 https://github.com/vyogotech/frappe-ai-agent.git $@

$(MCP_REPO):
	git clone --quiet --depth 1 https://github.com/vyogotech/frappe-mcp-server.git $@

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
	docker build $(BUILD_FLAGS) --tag=frappe-ai-agent:local $(AGENT_REPO)

mcp-image: $(MCP_REPO) ## Build the MCP server image
	docker build $(BUILD_FLAGS) --tag=frappe-mcp-server:local --file=mcp/Dockerfile $(MCP_REPO)

model: ## Check the configured model is present on the host
	@test -e "$(MODEL_MANIFEST)" || { \
	  echo "AI_MODEL=$(AI_MODEL) is not on this host." >&2; \
	  echo "The stack mounts ~/.ollama/models read-only and cannot download it." >&2; \
	  echo "Run: ollama pull $(AI_MODEL)" >&2; exit 1; }

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

keys: .env ## Write Frappe API credentials for the MCP server into .env
	@$(COMPOSE) exec -T backend bench --site "$(SITE)" execute \
	  frappe.core.doctype.user.user.generate_keys --args '["Administrator"]' \
	  | tr ',' '\n' \
	  | sed -n -e 's/.*"api_key"[^"]*"\([^"]*\)".*/FRAPPE_API_KEY=\1/p' \
	           -e 's/.*"api_secret"[^"]*"\([^"]*\)".*/FRAPPE_API_SECRET=\1/p' > .env.keys
	@test "$$(wc -l < .env.keys)" -eq 2 || { rm -f .env.keys; echo 'keys: bench returned no credentials' >&2; exit 1; }
	@grep -v '^FRAPPE_API_' .env > .env.new && cat .env.keys >> .env.new && mv .env.new .env && rm -f .env.keys
	@$(COMPOSE) up -d mcp
	@echo 'credentials written to .env'

apps: ## List the apps in the image
	@docker run --rm --entrypoint sh $(IMAGE):$(TAG) -c 'ls -1 apps'

bench: ## Run a bench command, e.g. make bench ARGS="--site demo.localhost list-apps"
	@$(COMPOSE) exec -T backend bench $(ARGS)

logs: ## Follow the logs
	$(COMPOSE) logs -f

shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend bash

.PHONY: setup help model wizard image agent-image mcp-image up down destroy site keys apps bench logs shell
