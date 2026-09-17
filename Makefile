.DEFAULT_GOAL := help

FRAPPE_BRANCH     ?= version-16
FRAPPE_DOCKER     ?= .build/frappe_docker
FRAPPE_DOCKER_REF ?= a0c5213
IMAGE             ?= frappe-demo
TAG               ?= 16
BUILD_FLAGS       ?=

SITE             ?= demo.localhost
ADMIN_PASSWORD   ?= admin
DB_ROOT_PASSWORD ?= 123

COMPOSE = docker compose --project-name frappe-demo --env-file .env \
  -f $(FRAPPE_DOCKER)/compose.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.mariadb.yaml \
  -f $(FRAPPE_DOCKER)/overrides/compose.redis.yaml \
  -f compose.demo.yaml

help: ## List targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

$(FRAPPE_DOCKER):
	git clone --quiet https://github.com/frappe/frappe_docker.git $@
	git -C $@ checkout --quiet $(FRAPPE_DOCKER_REF)

.env:
	cp .env.example .env

image: $(FRAPPE_DOCKER) ## Build the image with erpnext and drive
	docker build $(BUILD_FLAGS) \
	  --build-arg=FRAPPE_PATH=https://github.com/frappe/frappe \
	  --build-arg=FRAPPE_BRANCH=$(FRAPPE_BRANCH) \
	  --build-arg=CACHE_BUST=$$(cksum apps.json | cut -d' ' -f1) \
	  --secret=id=apps_json,src=apps.json \
	  --tag=$(IMAGE):$(TAG) \
	  --file=$(FRAPPE_DOCKER)/images/layered/Containerfile \
	  $(FRAPPE_DOCKER)

up: $(FRAPPE_DOCKER) .env ## Start the stack
	$(COMPOSE) up -d

down: ## Stop the stack, keep the data
	$(COMPOSE) down

destroy: ## Stop the stack and delete the data
	$(COMPOSE) down --volumes

site: ## Create the demo site
	$(COMPOSE) exec backend bench new-site $(SITE) \
	  --mariadb-user-host-login-scope=% \
	  --db-root-password $(DB_ROOT_PASSWORD) \
	  --admin-password $(ADMIN_PASSWORD) \
	  --install-app erpnext --install-app drive \
	  --set-default

apps: ## List the apps in the image
	@docker run --rm --entrypoint sh $(IMAGE):$(TAG) -c 'ls -1 apps'

bench: ## Run a bench command, e.g. make bench ARGS="--site demo.localhost list-apps"
	@$(COMPOSE) exec -T backend bench $(ARGS)

logs: ## Follow the logs
	$(COMPOSE) logs -f

shell: ## Open a shell in the backend container
	$(COMPOSE) exec backend bash

.PHONY: help image up down destroy site apps bench logs shell
