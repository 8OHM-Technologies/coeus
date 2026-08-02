# 🌌 Project COEUS: Dynamic PDF Intelligence Pipeline

**COEUS** (named after the Greek Titan of intellect and the heavenly axis) is a distributed data platform designed to automate the scraping, extraction, and analysis of multi-source documentation.

The project demonstrates a scalable, resilient approach to handling varying degrees of web complexity: from **basic HTML parsing** on sites like **ccma.org.za** using Python and BeautifulSoup, to bypassing **advanced anti-bot and Cloudflare Turnstile measures** on high-security regulatory platforms like **saflii.org** and **sabinet.co.za** using **SeleniumBase UC Mode (Undetected ChromeDriver)**, Playwright, and the local **`misstcha`** package (a vision-based solver for hCaptchas and Cloudflare Turnstiles).

---

## 🏗️ System Architecture

The platform uses a decoupled, microservices-oriented architecture designed for horizontal scalability, high stealth, and robust data extraction. Workflow orchestration is powered by **Dagster**, leveraging containerized execution via Dagster Pipes Docker.

| Component | Role |
| :--- | :--- |
| **Control Plane (Django 6)** | Source of truth for dynamic pipeline blueprints, scraper routing, relational model configuration (`ScraperType`, `DocumentType`, `LLMEngine`), scheduling metadata, and the REST API. |
| **Extracted Data (Django)** | Read-only & ORM layer over PostgreSQL tables (`entities`, `targets`, `extracted_records`, `scrubbed_records`) for structured LLM output and anonymized data. |
| **Orchestrator (Dagster v1.x+)** | Evaluates pipeline blueprints via sensors, registers Dynamic Partitions, and materializes Assets. Runs `dagster-webserver`, `dagster-daemon`, and `dagster-worker` with resource throttling (`max_concurrent_runs=1`). |
| **Extraction Workers** | Ephemeral containers (`coeus-scraper` and `coeus-extractor`) executed via **Dagster Pipes Docker**. Scraping scripts subclass `BaseScraper`, utilizing **SeleniumBase UC Mode** or Playwright, staggered worker startup, proxy IP verification, and direct PostgreSQL storage. |
| **`misstcha/`** | Local Python package providing `HCaptchaSolver` (Grounding DINO + Qwen), `TurnstileSolver` (Cloudflare checkbox automation), and a `CaptchaSolverFactory`. Copied into worker images at build time. |
| **Storage (PostgreSQL 17 & Oracle DB)** | Primary PostgreSQL database for pipelines and extracted records, paired with dual Oracle DB support and bi-directional synchronization management commands (`sync_oc_to_master`). |
| **Background Scheduler Service** | Executes periodic background tasks, system analytics aggregation, and metrics caching (`ScrapingPipelineMetrics`). |
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
 │ │  coeus-scraper  │ │ (SeleniumBase UC /          │
 │ └────────┬────────┘ │  Playwright)                │
 │          │          │                             │
 └──────────┼──────────┘                             │
            │ writes records                         │
            ▼                                        ▼
┌──────────────────────────────────────────────────────┐
│                  PostgreSQL Database                 │
│              (& Optional Oracle DB Sync)             │
└──────────────────────────────────────────────────────┘
```

---

## 🛠️ Key Technical Features

### **1. Relational Model-Driven Pipelines**
Every scraper is dynamically configured through relational Django models (`PipelineConfiguration`, `ScraperType`, `DocumentType`, and `LLMEngine`):
* **Dynamic Partitions**: The Dagster sensor `coeus_blueprint_sensor` queries active pipeline blueprints from the Django API every 60 seconds and updates `pipelines` Dynamic Partitions without requiring orchestrator restarts.
* **Cron Scheduling**: Scheduled runs trigger automatically based on standard cron expressions stored per configuration.
* **Flexible Extensibility**: New scraper types, document formats, or AI engines can be registered as database records without code changes in the control plane.

### **2. Stealth Scraper & Worker Architecture (SeleniumBase UC Mode)**
High-security targets (such as SAFLII and Sabinet) utilize a high-stealth scraping engine powered by **SeleniumBase UC (Undetected ChromeDriver) Mode**:
* **Turnstile Evasion & Auto-Requeuing**: Detects Cloudflare Turnstile challenges automatically, executes human-like interactions, and re-queues blocked cases for retry.
* **Automatic Proxy Rotation**: On Turnstile block detection or rate limits, the scraper rotates proxy IPs and verifies connectivity via `browser_helper.py` and `debug_helper.py`.
* **Staggered Worker Startup**: Concurrently spawned browser sessions stagger their startup to prevent CPU/memory spikes and race conditions during initial navigation.
* **Thread-Safe DB & GUI Locking**: Multi-threaded detailing coordinates database commits via `self.db_lock` and thread locks for UC GUI interactions.
* **Stage Control & Driver Timeouts**: Supports `skip_stages` execution flags (e.g. indexing-only or detailing-only) and explicit driver timeouts for increased stability.

### **3. BaseScraper Framework & Dynamic Progress Tracking**
Scrapers subclass `BaseScraper` (`extraction_workers/base_scraper.py`), managing state and execution lifecycles:
* **Dynamic Progress Tracking**: Dynamically queries the `extracted_records` database table for pending records per pipeline and year, eliminating reliance on static state markers in pipeline configurations.
* **Deduplication State**: Hydrates internal URL and case number sets from PostgreSQL before scraping to prevent redundant network calls.
* **UTC Timezone Standardization**: All timestamps across scrapers, pipelines, and database records use timezone-aware UTC (`scraped_at`, `detailed_at`).

### **4. Dagster Pipes Docker Infrastructure**
Dagster executes scrapers and extractors inside ephemeral containers via `PipesDockerClient`:
* Spawns clean `coeus-scraper` and `coeus-extractor` container runs inside the shared Docker network.
* Bind-mounts host data volumes (`/var/shared_scraping_data`) for temporary asset passing.
* Image build policies (`pull_policy: build`) and strict concurrency bounds (`max_concurrent_runs: 1` in `dagster.yaml`) maintain system stability under load.

### **5. Dual-Database Storage & Multi-DB Synchronization**
* **Direct Async Writes**: Scrapers write records directly to PostgreSQL using `db_storage.py` and `asyncpg`.
* **Indexed Performance**: Database fields (`scraped_at`, `detailed_at`, `case_number`, `url`) are indexed for fast lookups.
* **Multi-DB Sync**: Includes Django management commands (`sync_oc_to_master`) to bi-directionally synchronize `ExtractedRecord` entries between dual Oracle DB and primary PostgreSQL databases using bulk operations and target caching.

### **6. Scraper-Type Analytics & Background Scheduler**
* **Scraper-Type Aggregation**: Aggregates scraping analytics metrics (`control_plane/pipelines/analytics.py`) grouped by `scraper_type` rather than individual pipeline configurations.
* **Background Scheduler**: A dedicated background scheduler service periodically calculates scrape rates, success metrics, and status breakdowns, caching results in `ScrapingPipelineMetrics`.

### **7. LLM Extraction & PII Scrubbing (`ScrubbedRecord`)**
* **LLM Extraction**: The `extracted_structured_data` Dagster asset processes raw documents via pdfplumber, queries local **Ollama** instances (`ollama/phi4-mini`) or external LLM APIs, and validates JSON against Pydantic schemas (`schemas.py`).
* **PII Redaction (`pii_scrub.py`)**: The `scrubbed_extracted_records` asset executes downstream, running `/app/scrub_entrypoint.py` and `pii_scrub.py` inside the extractor container to redact PII (names, ID numbers, addresses). Cleaned data is saved into the `ScrubbedRecord` model (1:1 relation with `ExtractedRecord`).

---

## 🕵️ Scraper Registry

Specialized worker scripts reside in `extraction_workers/`:

| Scraper Script | Target Platform / Data Type | Technology & Evasion Strategy |
| :--- | :--- | :--- |
| [sedarplus_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sedarplus_scraper.py) | **SEDAR+ Corporate Filings** | Playwright + `misstcha.HCaptchaSolver` (Grounding DINO + Qwen) |
| [sabinet_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sabinet_scraper.py) | **Sabinet CCMA Labor Awards** | SeleniumBase UC Mode, dynamic date range selection, rate-limit throttling, direct DB storage |
| [new_saflii_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/new_saflii_scraper.py) | **SAFLII Case Law** | SeleniumBase UC Mode + BeautifulSoup, Turnstile bypass, automatic case re-queuing, proxy rotation, dynamic year progress tracking |
| [mantech_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/mantech_scraper.py) | **Mantech Electronics Store** | Playwright, ASP.NET WebForms paginated table extraction |
| [livestainable_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/livestainable_scraper.py) | **Livestainable Products** | Playwright e-commerce scraping |
| [lotto_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/lotto_scraper.py) | **National Lottery Results** | Playwright historical data parser with CSV exporter |

Supported `scraper_type` values are defined dynamically in the `ScraperType` database table.

---

## 🔌 Extracted Data REST API

The Control Plane exposes a REST API powered by **Django REST Framework** with **OAuth2 token authentication** (`django-oauth-toolkit`). This API exposes the extracted data models (`Entity`, `Target`, and `ExtractedRecord`) to external consumers.

### **Authentication Flow**

The API uses the standard OAuth2 **Client Credentials** grant type:

1. **Register an OAuth2 Application**:
   Create an application via the Django Admin panel at `http://localhost:8001/admin/oauth2_provider/application/` (or via Traefik route `http://control-plane.localhost:8085/admin/...`).
   * **Client Type**: Confidential
   * **Authorization Grant Type**: Client credentials
   * **User**: Select your admin or API user

2. **Obtain an Access Token**:
   ```bash
   curl -X POST http://localhost:8001/o/token/ \
     -u "client_id:client_secret" \
     -d "grant_type=client_credentials"
   ```
   Returns access token payload:
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
| `/api/v1/targets/` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Manage projects, locations, or targets belonging to an Entity. |
| `/api/v1/extracted-records/` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE` | Manage structured data records extracted from documents. |

---

## 🚀 Quick Start & Deployment Options

### **Option A: Standard Development Stack**

```bash
# Build the application, orchestrator, and worker container images
docker compose build

# Boot the local stack
docker compose up -d
```

### **Option B: Portable Stack (`docker-compose.portable.yml`)**

For self-contained setups with host database binding or portable deployments:

```bash
# Using Makefile shortcuts:
make portable-up       # Boot portable stack
make portable-down     # Stop portable stack
make portable-restart  # Full clean restart cycle
```

---

## 🖥️ Dashboards & Local Access

Local URLs are reverse-proxied by Traefik and exposed on port **`8085`** (HTTP) and **`8080`** (Traefik dashboard).

| Service | Direct Port URL | Traefik URL (Port 8085) | Credentials |
| :--- | :--- | :--- | :--- |
| **Control Plane (Django)** | `http://localhost:8001/admin` | `http://control-plane.localhost:8085/admin` | `admin` / `admin` |
| **Dagster Web UI** | `http://localhost:3000` | `http://dagster.localhost:8085` | N/A |
| **Active Pipelines API** | — | `http://control-plane.localhost:8085/api/pipelines/active/` | N/A |
| **Traefik Dashboard** | `http://localhost:8080` | — | — |

On first boot, the control plane automatically executes database migrations and populates default pipeline seeds (`seed_pipelines`).

---

## 🌐 Production Deployment

Production is deployed on a VPS with automated builds and deployment via GitHub Actions on every push to the `main` branch.

| Concern | Implementation |
| :--- | :--- |
| **Compose file** | `docker-compose.prod.yml` |
| **Services** | `control-plane`, `dagster-webserver`, `dagster-daemon`, `dagster-worker`, `postgres`, `redis`, `traefik`, `scheduler` |
| **Images** | `ghcr.io/8ohm-technologies/coeus-app`, `ghcr.io/8ohm-technologies/coeus-dagster-webserver`, `ghcr.io/8ohm-technologies/coeus-dagster-daemon`, `ghcr.io/8ohm-technologies/coeus-dagster-worker` |
| **CI/CD** | `.github/workflows/deploy.yml` — Automated docker builds pushed to GHCR, deployed via SSH with Telegram status updates. |
| **TLS** | Traefik with automatic Let's Encrypt certificates. |
| **Public URLs** | `https://control-plane.8ohm.co.za`, `https://dagster.8ohm.co.za` |

---

## 📂 Project Structure

```text
├── control_plane/           # Django project (pipelines, extracted_data apps, scheduler, API)
├── dagster/                 # Dagster pipelines, definitions, sensors, and pipes configuration
├── extraction_workers/      # Python scrapers, LLM extractors, schemas, and DB storage
│   └── utils/               # Browser helper, debug helper, PII scrub, proxy verification
├── misstcha/                # Local CAPTCHA solver package (hCaptcha, Turnstile, Captcha Factory)
├── traefik-config/          # Reverse proxy routing configuration
├── Makefile                 # Development and portable stack management commands
├── Dockerfile.app           # Django control plane container image
├── Dockerfile.dagster       # Dagster webserver/daemon container image
├── Dockerfile.worker        # Dagster worker container image
├── Dockerfile.scraper       # Pipes Docker Scraper image
├── Dockerfile.extractor     # Pipes Docker Extractor image
├── docker-compose.yml       # Standard local multi-container composition
└── docker-compose.portable.yml # Portable multi-container stack composition
```

---

## 🔮 Future Enhancements

* **Proxy Pool Auto-Scaling**: Dynamic proxy pool integration within `BaseScraper` with automated performance scoring and failover.
* **Kubernetes Pipes Executor**: Transition from `PipesDockerClient` to `PipesK8sClient` for container autoscaling across Kubernetes clusters.
* **Object Cloud Storage (GCS/S3)**: Offload raw PDF/JSON storage from local disk mounts to Google Cloud Storage or S3 buckets.
* **Fleet Observability**: Prometheus metrics and OpenTelemetry tracing across ephemeral scraper execution workers.

---

## 📄 Related Documentation

* [TECHNICAL_OVERVIEW.md](file:///home/tiaanf/Dev/coeus/TECHNICAL_OVERVIEW.md) — Comprehensive technical overview of the COEUS scraping, extraction, and orchestration architecture.
