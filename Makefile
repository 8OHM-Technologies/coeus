# Define phony targets so Make doesn't look for actual files with these names
.PHONY: up down status logs dagster stop-dagster publish

# Default tag for Docker images built and published locally
TAG ?= latest

# -----------------------------------------------------------------------------
# GLOBAL COMMANDS
# -----------------------------------------------------------------------------

# Spin up all containers in the background
up:
	docker compose up -d --build
	docker compose build coeus-scraper coeus-extractor

# Tear down all containers, networks, and volumes
down:
	docker compose down

# Spin up all containers in the background no build
up-no-build:
	docker compose up -d

# Check the status of all running services
status:
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

# Spin up the 3 specific Dagster containers together
coeus:
	docker compose up -d --build traefik control-plane postgres

# Spin down just the Dagster stack
stop-coeus:
	docker compose stop control-plane postgres

# -----------------------------------------------------------------------------
# DYNAMIC INDIVIDUAL SERVICE COMMANDS
# -----------------------------------------------------------------------------

# Catch-all target: Spin up any individual container by its service name
# Example: 'make web' or 'make redis'
%:
	@docker compose up -d $@

# Dynamic target: Stop any individual container by prefixing 'stop-'
# Example: 'make stop-web' or 'make stop-redis'
stop-%:
	@docker compose stop $*