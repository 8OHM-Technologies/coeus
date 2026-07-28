-include .env
export GITHUB_ACCESS_TOKEN

# Define phony targets so Make doesn't look for actual files with these names
.PHONY: up down status logs dagster stop-dagster publish prod-up prod-down prod-pull portable-pull portable-up portable-down portable-restart

# Default tag for Docker images built and published locally
TAG ?= latest

# -----------------------------------------------------------------------------
# GLOBAL COMMANDS
# -----------------------------------------------------------------------------

pull:
	echo $(GITHUB_ACCESS_TOKEN) | docker login ghcr.io -u 8ohm-tiaanf --password-stdin
	docker compose pull
	docker pull ghcr.io/8ohm-technologies/coeus-scraper:latest
	docker pull ghcr.io/8ohm-technologies/coeus-extractor:latest

# Spin up all containers (full down → pull → up to ensure image changes take effect)
up:
	docker compose down
	docker compose pull
	docker compose up -d

# Tear down all containers, networks, and volumes
down:
	docker compose down

prod-pull:
	echo $(GITHUB_ACCESS_TOKEN) | docker login ghcr.io -u 8ohm-tiaanf --password-stdin
	docker compose -f docker-compose.prod.yml pull
	docker pull ghcr.io/8ohm-technologies/coeus-scraper:latest
	docker pull ghcr.io/8ohm-technologies/coeus-extractor:latest

# Full down → pull → up for prod (ensures updated images are used)
prod-up:
	docker compose -f docker-compose.prod.yml down
	docker compose -f docker-compose.prod.yml pull
	docker compose -f docker-compose.prod.yml up -d

# Tear down all containers, networks, and volumes
prod-down:
	docker compose -f docker-compose.prod.yml down

# Pull latest images for the portable stack
portable-pull:
	echo $(GITHUB_ACCESS_TOKEN) | docker login ghcr.io -u 8ohm-tiaanf --password-stdin
	docker compose -f docker-compose.portable.yml pull
	docker pull ghcr.io/8ohm-technologies/coeus-scraper:latest
	docker pull ghcr.io/8ohm-technologies/coeus-extractor:latest

# Full down → pull → up for portable (ensures updated images are used)
portable-up:
	docker compose -f docker-compose.portable.yml down
	docker compose -f docker-compose.portable.yml pull
	docker compose -f docker-compose.portable.yml up -d

# Tear down the portable stack
portable-down:
	docker compose -f docker-compose.portable.yml down

# Restart all portable services without a full down (quick refresh)
portable-restart:
	docker compose -f docker-compose.portable.yml restart

# Spin up all containers in the background no build
up-no-build:
	docker compose up -d

# Check the status of all running services
ps:
	docker compose ps

# Tail logs for all services
logs:
	docker compose logs -f

# Build, tag, and push all custom Docker images to GHCR from local machine
publish:
	./publish_images.sh $(TAG)

# -----------------------------------------------------------------------------
# GROUP COMMANDS (Dagster Stack)
# -----------------------------------------------------------------------------

# Spin up the 3 specific Dagster containers together
dagster:
	docker compose up -d --build dagster-webserver dagster-daemon postgres
	docker compose build coeus-scraper coeus-extractor

# Spin down just the Dagster stack
stop-dagster:
	docker compose stop dagster-webserver dagster-daemon postgres

# -----------------------------------------------------------------------------
# GROUP COMMANDS (Control Plane Stack)
# -----------------------------------------------------------------------------

# Spin up the 3 specific Coeus containers together
coeus:
	docker compose up -d --build traefik control-plane postgres

# Spin down just the Coeus stack
stop-coeus:
	docker compose stop control-plane postgres

# -----------------------------------------------------------------------------
# DYNAMIC INDIVIDUAL SERVICE COMMANDS
# -----------------------------------------------------------------------------

# Catch-all target: Spin up any individual container by its service name
# Example: 'make web' or 'make redis'
%:
	@docker compose up -d --build $@

# Dynamic target: Stop any individual container by prefixing 'stop-'
# Example: 'make stop-web' or 'make stop-redis'
stop-%:
	@docker compose stop $*

# Catch-all target (PROD): Spin up any individual container by its service name
# Example: 'make prod-web' or 'make prod-redis'
prod-%:
	@docker compose up -d --build $@

# Dynamic target (PROD): Stop any individual container by prefixing 'prod-stop-'
# Example: 'make prod-stop-web' or 'make prod-stop-redis'
prod-stop-%:
	@docker compose stop $*