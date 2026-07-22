# 🌌 Project COEUS: Dynamic PDF Intelligence Pipeline

**COEUS** (named after the Greek Titan of intellect and the heavenly axis) is a distributed data platform designed to automate the scraping, extraction, and analysis of multi-source documentation.

The project demonstrates a scalable approach to handling varying degrees of web complexity: from **basic HTML parsing** on sites like **ccma.org.za** using Python and BeautifulSoup, to bypassing **advanced anti-bot measures** on high-security regulatory platforms like **sedarplus.ca** (e.g., NI 43-101 mining reports) using Playwright, Selenium, and the local **`misstcha`** package (a vision-based solver for hCaptchas and Cloudflare Turnstiles).

---

## 🏗️ System Architecture

The platform uses a decoupled, microservices-oriented architecture designed for horizontal scalability and robust data extraction. It has transitioned from Apache Airflow to **Dagster** for workflow orchestration, leveraging containerized execution via Dagster Pipes.

| Component | Role |
| :--- | :--- |
| **Control Plane (Django 6)** | Source of truth for pipeline blueprints, scraper routing, scheduling metadata, and the Selector Lab API. |
| **Extracted Data (Django)** | Read-only ORM layer over PostgreSQL tables (`entities`, `targets`, `extracted_records`) for structured LLM output. |
| **Orchestrator (Dagster v1.x+)** | Evaluates pipeline blueprints via sensors, registers Dynamic Partitions, and materializes Assets. Runs `dagster-webserver`, `dagster-daemon`, and `dagster-worker`. |
| **Extraction Workers** | Ephemeral containers (`coeus-scraper` and `coeus-extractor`) run via **Dagster Pipes Docker**. Scraping scripts subclass `BaseScraper` and store records directly to PostgreSQL. |
| **`misstcha/`** | Local Python package providing `HCaptchaSolver` (Grounding DINO + Qwen), `TurnstileSolver` (Cloudflare checkbox automation), and a `CaptchaSolverFactory`. Copied into the worker image at build time. |
| **Storage (PostgreSQL 17)** | Primary database used by the control plane and extraction workers for storing pipeline definitions and scraped/extracted structured data. |
| **Gateway (Traefik)** | Reverse proxy with host-based routing; routes local domains to containers via port `8085` and handles Let's Encrypt TLS termination in production. |

```text
┌────────────────────────┐      polls      ┌──────────────────┐
│     Dagster Daemon     │ ──────────────► │  Control Plane   │
│   (Blueprint Sensor)   │                 │   (Django API)   │
└────────────────────────┘                 └──────────────────┘
            │                                        │
            │ spawns via Pipes                       │
 ┌──────────▼──────────┐                             │
 │    Docker Engine    │                             │
 │ ┌─────────────────┐ │                             │
 │ │  coeus-scraper  │ │                             │
 │ └────────┬────────┘ │                             │
 │          │          │                             │
 └──────────┼──────────┘                             │
            │ writes records                         │
            ▼                                        ▼
┌──────────────────────────────────────────────────────┐
│                  PostgreSQL Database                 │
└──────────────────────────────────────────────────────┘
```

---

## 🛠️ Key Technical Features

### **1. Model-Driven Pipelines**
Every scraper is configured through the Django `PipelineConfiguration` model: `scraper_type`, cron schedule, CSS selectors, `extraction_params` JSON, SSL flags, and optional LLM extraction settings. Changes propagate dynamically:
* The Dagster sensor `coeus_blueprint_sensor` fetches active pipeline blueprints from the Django API every 60 seconds.
* It dynamically updates the `pipelines` Dynamic Partitions in Dagster, creating or removing partitions without requiring orchestrator restarts.
* Scheduled runs are triggered based on the cron expressions stored directly in the database configurations.

### **2. Scraper Concurrency & Worker Model**
High-throughput scrapers (like `sabinet` and `new_saflii`) utilize an asynchronous, concurrent worker model to expedite detailing:
* **Concurrency Configuration**: The number of parallel workers is defined via `concurrency` in the pipeline's `extraction_params` (defaulting to `8`).
* **Async Workers**: An `asyncio.Queue` distributes URLs. Multiple worker coroutines process tasks concurrently.
* **Resilient Isolation**: To avoid browser context conflicts and IP blocks, each worker instantiates its own Playwright browser context and page. This ensures independent proxy routing (if `use_proxy` is enabled).
* **Safe Database Commits**: Shared database writes are coordinated with a `self.db_lock` (asyncio Lock) to protect concurrent PostgreSQL updates.

### **3. BaseScraper Class Framework**
All standardized scrapers subclass `BaseScraper` (`extraction_workers/base_scraper.py`), which abstracts lifecycle and state management:
* **`initialize()`**: Fetches configuration, establishes database connections, resolves target IDs, and loads deduplication data.
* **Deduplication State**: Hydrates internal sets (`existing_urls` and `existing_case_numbers`) from the database, preventing redundant scraping or duplicate network calls.
* **Pagination State Persistence**: Implements `save_progress()` to cache rolling-window pagination execution markers (e.g. `last_year`/`last_month`) directly in the database.
* **`cleanup()`**: Automatically recycles page, context, browser, and database connections.

### **4. Dagster Pipes Docker Infrastructure**
Instead of static agent environments, Dagster executes scrapers and extractors using **Dagster Pipes Docker** (`PipesDockerClient`):
* Spawns ephemeral `coeus-scraper` and `coeus-extractor` container runs in the shared Docker network.
* Bind-mounts host data volumes (`/var/shared_scraping_data`) to pass raw output and assets.
* Forwards environment variables (e.g., PostgreSQL credentials, OpenAI API keys, Hugging Face tokens) securely.

### **5. Database-Centric Storage**
Scrapers write records directly to PostgreSQL using `db_storage.py` and `asyncpg`, completely replacing the legacy workflow that relied on local JSON files and Google Drive backups. Duplicate entries are caught during indexing/detailing via database signatures, making operations incremental and self-deduplicating.

### **6. `misstcha` — Local CAPTCHA Solver Package**
The `misstcha/` directory is structured as a local Python package copied into the worker images:
* **`HCaptchaSolver`**: Uses Grounding DINO zero-shot object detection combined with Qwen LLM translation for image-grid hCaptchas.
* **`TurnstileSolver`**: Playwright-based solver that locates Cloudflare Turnstile iframes and performs checkbox interactions.
* **`CaptchaSolverFactory`**: Implements a registry pattern to load captcha solvers dynamically by name.

### **7. LLM Extraction & PII Scrubbing**
* **LLM Extraction**: The `extracted_structured_data` asset runs as a downstream task in Dagster. It processes documents (PDF/JSON) via pdfplumber, queries local **Ollama** instances (`ollama/phi4-mini`) or external APIs, validates the output using Pydantic schemas (`schemas.py`), and saves structured records.
* **PII Scrubbing**: The `scrubbed_extracted_records` asset executes downstream from the LLM extractor. It runs `/app/scrub_entrypoint.py` inside the extractor container to anonymize and remove PII from the structured outputs.

---

## 🕵️ Scraper Registry

Specialized worker scripts reside in `extraction_workers/`:

| Scraper Script | Target Platform / Data Type | Technology & Strategy |
| :--- | :--- | :--- |
| [sedarplus_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sedarplus_scraper.py) | **SEDAR+ Corporate Filings** | Playwright + `misstcha.HCaptchaSolver` (Grounding DINO + Qwen) |
| [sabinet_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sabinet_scraper.py) | **Sabinet CCMA Labor Awards** | Playwright, async concurrency, early stopping, rate-limit throttling, direct DB storage |
| [new_saflii_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/new_saflii_scraper.py) | **SAFLII Case Law** | BeautifulSoup + Playwright + `misstcha.TurnstileSolver`, async concurrency, direct DB storage |
| [mantech_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/mantech_scraper.py) | **Mantech Electronics Store** | Playwright, ASP.NET WebForms paginated table extraction |
| [livestainable_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/livestainable_scraper.py) | **Livestainable Products** | Playwright e-commerce scraping |
| [lotto_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/lotto_scraper.py) | **National Lottery Results** | Playwright historical data parser with CSV exporter |

Supported `scraper_type` values are defined in `control_plane/pipelines/models.py` (`ScraperType` choices).

---

## 🔌 Extracted Data REST API

The Control Plane exposes a REST API powered by **Django REST Framework** with **OAuth2 token authentication** (`django-oauth-toolkit`). This API exposes the extracted data models (`Entity`, `Target`, and `ExtractedRecord`) to external consumers.

### **Authentication Flow**

The API uses the standard OAuth2 **Client Credentials** grant type for secure machine-to-machine communication:

1. **Register an OAuth2 Application**:
   Create an application via the Django Admin panel at `http://localhost:8001/admin/oauth2_provider/application/` (or via the Traefik route `http://control-plane.localhost:8085/admin/...`).
   * **Client Type**: Confidential
   * **Authorization Grant Type**: Client credentials
   * **User**: Select your admin or API user

2. **Obtain an Access Token**:
   ```bash
   curl -X POST http://localhost:8001/o/token/ \
     -u "client_id:client_secret" \
     -d "grant_type=client_credentials"
   ```
   This returns the access token:
   ```json
   {
     "access_token": "your_access_token",
     "expires_in": 36000,
     "token_type": "Bearer",
     "scope": "read write"
   }
   ```

3. **Access Protected API Endpoints**:
   ```bash
   curl -X GET http://localhost:8001/api/v1/entities/ \
     -H "Authorization: Bearer your_access_token"
   ```

### **API Endpoints**

| Endpoint | HTTP Methods | Description |
| :--- | :--- | :--- |
| `/api/v1/entities/` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Manage monitored organizations and companies. |
| `/api/v1/targets/` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Manage projects, locations, or assets belonging to an Entity. |
| `/api/v1/extracted-records/` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Manage structured data records extracted from documents. |

---

## 🚀 Quick Start (Local Development)

The local stack is fully containerized. You must supply a `.env` file at the root with environment variables (credentials, endpoints, etc.) matching `docker-compose.yml`.

### 1. Build and Boot the Services

```bash
# Build the application, orchestrator, and pipes worker images
docker compose build

# Boot the local stack
docker compose up -d
```

### 2. Access the Dashboards

Local URLs are reverse-proxied by Traefik and exposed on port **`8085`** (HTTP) and **`8080`** (Traefik dashboard).

| Service | Direct Port URL | Traefik URL (Port 8085) | Credentials |
| :--- | :--- | :--- | :--- |
| **Control Plane (Django)** | `http://localhost:8001/admin` | `http://control-plane.localhost:8085/admin` | `admin` / `admin` |
| **Dagster Web UI** | `http://localhost:3000` | `http://dagster.localhost:8085` | N/A |
| **Active Pipelines API** | — | `http://control-plane.localhost:8085/api/pipelines/active/` | N/A |
| **Traefik Dashboard** | `http://localhost:8080` | — | — |

On first boot, the control plane will run migrations and automatically create the default superuser.

### 3. Run a Scraper Manually

To bypass Dagster orchestration and execute a scraper script directly in the worker environment:

```bash
docker exec -it coeus-control-plane python extraction_workers/new_saflii_scraper.py --pipeline_name <pipeline_name>
```

---

## 🌐 Production Deployment

Production is deployed on a VPS (AbsoluteHosting) with automated builds and deployment via GitHub Actions on every push to the `main` branch.

| Concern | Implementation |
| :--- | :--- |
| **Compose file** | `docker-compose.prod.yml` |
| **Services** | `control-plane`, `dagster-webserver`, `dagster-daemon`, `dagster-worker`, `postgres`, `redis`, `traefik` |
| **Images** | `ghcr.io/8ohm-technologies/coeus-app`, `ghcr.io/8ohm-technologies/coeus-dagster-webserver`, `ghcr.io/8ohm-technologies/coeus-dagster-daemon`, `ghcr.io/8ohm-technologies/coeus-dagster-worker` |
| **CI/CD** | `.github/workflows/deploy.yml` — Automated docker builds pushed to GHCR, deployed via SSH with Telegram status updates. |
| **TLS** | Traefik with automatic Let's Encrypt certificates. |
| **Public URLs** | `https://control-plane.8ohm.co.za`, `https://dagster.8ohm.co.za` |

---

## 📂 Project Structure

```text
├── control_plane/           # Django project (pipelines, extracted_data apps, scheduler)
├── dagster/                 # Dagster configuration, pipelines workspace, and definitions
├── extraction_workers/      # Python scrapers, LLM extractors, schemas, and db_storage.py
├── misstcha/                # Local CAPTCHA solver package (hCaptcha, Turnstile, Captcha Factory)
├── traefik-config/          # Reverse proxy routing rules
├── ohmbase/                 # BookStack Wiki configuration (Dockerfiles & logs)
├── ohmbot-telegram/         # Telegram Bot interface
├── Dockerfile.app           # Django control plane container image
├── Dockerfile.dagster       # Dagster webserver/daemon container image
├── Dockerfile.worker        # Dagster worker container image
├── Dockerfile.scraper       # Pipes Docker Scraper image
├── Dockerfile.extractor     # Pipes Docker Extractor image
├── docker-compose.yml       # Local development multi-container orchestration
└── docker-compose.prod.yml  # Production deployment multi-container orchestration
```

---

## 🔮 Future Enhancements

* **Proxy Rotation Pools**: Integrate rotating residential proxy networks natively within `BaseScraper` to bypass aggressive geo-blocking.
* **Kubernetes Executor**: Transition from `PipesDockerClient` to `PipesK8sClient` for autoscaling containers dynamically on Kubernetes clusters.
* **Object Cloud Storage**: Offload local `/var/shared_scraping_data` PDFs and JSON assets directly to Google Cloud Storage (GCS) or AWS S3 buckets.
* **Observability**: Set up Prometheus/Grafana or Sentry logging across the scraping/extraction container fleet.

---

## 📄 Related Documentation

* [TECHNICAL_OVERVIEW.md](file:///home/tiaanf/Dev/coeus/TECHNICAL_OVERVIEW.md) — Comprehensive technical details of the COEUS scraping, extraction, and orchestration architecture.
