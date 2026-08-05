-include .env
export GITHUB_ACCESS_TOKEN

# Define phony targets so Make doesn't look for actual files with these names
.PHONY: up down status logs dagster stop-dagster publish pull-prod up-prod down-prod pull-portable up-portable down-portable restart-portable coeus stop-coeus scheduler stop-scheduler migrate seed setup sync-extracted import-bookstack sync-shop migrate-saflii scrub

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

pull-prod:
	echo $(GITHUB_ACCESS_TOKEN) | docker login ghcr.io -u 8ohm-tiaanf --password-stdin
	docker compose -f docker-compose.prod.yml pull
	docker pull ghcr.io/8ohm-technologies/coeus-scraper:latest
	docker pull ghcr.io/8ohm-technologies/coeus-extractor:latest

# Full down → pull → up for prod (ensures updated images are used)
up-prod:
	docker compose -f docker-compose.prod.yml down
	docker compose -f docker-compose.prod.yml pull
	docker compose -f docker-compose.prod.yml up -d

# Tear down all containers, networks, and volumes
down-prod:
	docker compose -f docker-compose.prod.yml down

# Pull latest images for the portable stack
pull-portable:
	echo $(GITHUB_ACCESS_TOKEN) | docker login ghcr.io -u 8ohm-tiaanf --password-stdin
	docker compose -f docker-compose.portable.yml pull
	docker pull ghcr.io/8ohm-technologies/coeus-scraper:latest
	docker pull ghcr.io/8ohm-technologies/coeus-extractor:latest

# Full down → pull → up for portable (ensures updated images are used)
up-portable:
	docker compose -f docker-compose.portable.yml down
	docker compose -f docker-compose.portable.yml pull
	docker compose -f docker-compose.portable.yml up -d

# Tear down the portable stack
down-portable:
	docker compose -f docker-compose.portable.yml down

# Restart all portable services without a full down (quick refresh)
restart-portable:
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

# Spin up the specific Coeus control plane containers together
coeus:
	docker compose up -d --build traefik control-plane scheduler postgres

# Spin down just the Coeus stack
stop-coeus:
	docker compose stop control-plane scheduler postgres

# -----------------------------------------------------------------------------
# DATABASE & CONTROL PLANE COMMANDS
# -----------------------------------------------------------------------------

# Run database migrations
migrate:
	docker compose exec control-plane python control_plane/manage.py migrate

# Seed pipeline definitions
seed:
	docker compose exec control-plane python control_plane/manage.py seed_pipelines

# First-time setup: Run migrations and seed pipelines
setup: migrate seed

# Sync extracted JSON records from disk into DB
sync-extracted:
	docker compose exec control-plane python control_plane/manage.py sync_extracted_records

# Import scrubbed court records into BookStack
import-bookstack:
	docker compose exec control-plane python control_plane/manage.py import_to_bookstack $(ARGS)

# Sync shop products
sync-shop:
	docker compose exec control-plane python control_plane/manage.py sync_shop_products

# Migrate SAFLII records
migrate-saflii:
	docker compose exec control-plane python control_plane/manage.py migrate_saflii_records

# Run standalone PII scrubber CLI script
scrub:
	docker compose exec control-plane python extraction_workers/scrub_standalone.py $(ARGS)

# Run CCMA court normalization cleanup
clean-ccma:
	docker compose exec control-plane python extraction_workers/clean_ccma_courts.py

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

# Dynamic target (PROD): Spin up any individual container by prefixing 'up-prod-'
# Example: 'make up-prod-web' or 'make up-prod-redis'
up-prod-%:
	@docker compose -f docker-compose.prod.yml up -d --build $*

# Dynamic target (PROD): Stop any individual container by prefixing 'stop-prod-'
# Example: 'make stop-prod-web' or 'make stop-prod-redis'
stop-prod-%:
	@docker compose -f docker-compose.prod.yml stop $*