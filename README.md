# 🌌 Project COEUS: Dynamic PDF Intelligence Pipeline

**COEUS** (named after the Greek Titan of intellect and the heavenly axis) is a distributed data platform designed to automate the scraping, extraction, and analysis of multi-source documentation.

The project demonstrates a scalable approach to handling varying degrees of web complexity: from **basic HTML parsing** on sites like **ccma.org.za** using Python and BeautifulSoup, to bypassing **advanced anti-bot measures** on high-security regulatory platforms like **sedarplus.ca** (e.g., NI 43-101 mining reports) using Playwright, Selenium, a custom LLM-based tool to bypass hCaptchas/Cloudflare Turnstiles and more.

---

## 🏗️ System Architecture

The platform uses a decoupled, microservices-oriented architecture designed for horizontal scalability:

| Component | Role |
| :--- | :--- |
| **Control Plane (Django 6)** | Source of truth for pipeline blueprints, scraper routing, scheduling metadata, and the Selector Lab API. |
| **Extracted Data (Django)** | Read-only ORM layer over PostgreSQL tables (`entities`, `targets`, `extracted_records`) for structured LLM output. |
| **Orchestrator (Apache Airflow 2.9.1)** | Polls the Control Plane API and materializes one DAG per active `PipelineConfiguration`. Workers run as ephemeral `DockerOperator` containers. |
| **Extraction Workers** | Playwright/BeautifulSoup scripts plus `llm_extractor.py` for schema-enforced post-processing. |
| **Miistcha (`miistcha/`)** | Custom package using Grounding DINO + Qwen for vision-based CAPTCHA bypass (used by SEDAR+, Saflii and others). |
| **Storage (PostgreSQL)** | PostgreSQL DB used by most of the services (currently only OhmBase (Bookstack) uses something else nl. MariaDB). |
| **Gateway (Traefik)** | Reverse proxy with host-based routing; TLS termination and HTTP→HTTPS redirect in production. |
| **Landing Page (Nginx)** | Static marketing site served at `8ohm.co.za` in production. |

```text
┌─────────────┐     poll      ┌──────────────────┐     spawn     ┌─────────────────┐
│   Airflow   │ ────────────► │  Control Plane   │ ────────────► │ DockerOperator  │
│  Scheduler  │               │  (Django API)    │               │  (coeus-worker) │
└─────────────┘               └──────────────────┘               └─────────────────┘
       │                                │                                  │
       └────────────────────────────────┴──────────────────────────────────┘
                                        │
                               ┌────────▼────────┐
                               │  Cloud SQL      │
                               │  (PostgreSQL)   │
                               └─────────────────┘
```

---

## 🛠️ Key Technical Features

### **1. Model-Driven Pipelines**
Every scraper is configured through the Django `PipelineConfiguration` model: `scraper_type`, cron schedule, CSS selectors, `extraction_params` JSON, SSL flags, and optional LLM extraction settings. Changes propagate to Airflow on the next DAG parse without orchestrator restarts.

### **2. Adaptive Scraping Strategy**
COEUS does not use a one-size-fits-all approach. Based on the target's complexity defined in the Control Plane:

- **Lightweight extraction:** Rapidly parses unprotected sites (e.g. **SAFLII**) with minimal browser overhead.
- **Heavy-lift automation:** Deploys **Playwright** to navigate JavaScript-heavy frameworks, Ant Design components, and dynamic loaders.
- **Two-phase Sabinet:** Index scraping (`sabinet_scraper.py`) followed by per-record detail extraction (`sabinet_detail_scraper.py`).

### **3. Dynamic DAG Factory**
`orchestration/airflow_dags/dynamic_factory.py` discovers `*_scraper.py` files at parse time, maps them to `scraper_type` keys, and builds a two-task DAG when `requires_extraction` is enabled: scrape → LLM extract.

### **4. AI-Powered hCaptcha Solver**
The `solver/` package integrates Hugging Face Transformers for:

- **Intelligent prompt translation:** Converting hCaptcha challenges into machine-readable queries.
- **Zero-shot object detection:** Using Grounding DINO to identify and interact with CAPTCHA grid elements.

### **5. LLM Extraction Layer**
When enabled, `llm_extractor.py` runs after ingestion using the configured engine (`gemini-cli` by default), a Pydantic schema from `extraction_workers/schemas.py`, and custom `extraction_instructions`. Results land in `extracted_records` with optional human-review flags.

### **6. Ecommerce Aggregation Scheduler**
The control plane runs background combination tasks (via Django-APScheduler, `run_scheduler` management command) to merge Mantech and Livestainable product datasets, apply markups, deduplicate, and trigger Saleor sync (`sync_saleor`).

### **7. Incremental Crawling & Rate Throttling**
Scrapers targeting large volumes (such as Sabinet CCMA Awards) use output-aware incremental early stopping. They compare scraped metadata `(award_number, title)` against existing files and terminate when cached records are hit. Page-based rate throttling (e.g. 60-second sleeps after every 100 pages) reduces bot-mitigation triggers.

---

## 🕵️ Scraper Registry

Specialized worker scripts live in `extraction_workers/`:

| Scraper Script | Target Platform / Data Type | Technology & Strategy |
| :--- | :--- | :--- |
| [sedarplus_scraper.py](extraction_workers/sedarplus_scraper.py) | **SEDAR+ Corporate Filings** | Playwright + `solver/` hCaptcha bypass (Grounding DINO + Qwen) |
| [sabinet_scraper.py](extraction_workers/sabinet_scraper.py) | **Sabinet CCMA Labor Awards (index)** | Playwright, incremental early-stopping, rate-limit throttling |
| [sabinet_detail_scraper.py](extraction_workers/sabinet_detail_scraper.py) | **Sabinet Award Details** | Playwright; follows index JSON output from parent pipeline |
| [ccma_playwright_scraper.py](extraction_workers/ccma_playwright_scraper.py) | **CCMA Arbitration Documents** | Playwright multi-category traversal + download utility |
| [judiciary_scraper.py](extraction_workers/judiciary_scraper.py) | **South African Judiciary Judgments** | Playwright limit auto-expander + bulk downloader |
| [saflii_scraper.py](extraction_workers/saflii_scraper.py) | **SAFLII Case Law** | Playwright with Xvfb; year-range filtering via `extraction_params` |
| [mantech_scraper.py](extraction_workers/mantech_scraper.py) | **Mantech Electronics Store** | Playwright ASP.NET WebForms paginated table extraction |
| [livestainable_scraper.py](extraction_workers/livestainable_scraper.py) | **Livestainable Products** | Playwright e-commerce scraper |
| [lotto_scraper.py](extraction_workers/lotto_scraper.py) | **National Lottery Results** | Playwright historical data parser with CSV exporter |

Supported `scraper_type` values are defined in `control_plane/pipelines/models.py` (`ScraperType` enum).

---

## 🚀 Quick Start (Local Development)

The stack is fully containerized. You need a `.env` file at the repo root with Cloud SQL proxy settings (`INSTANCE_CONNECTION_NAME`, `AIRFLOW_SQL_ALCHEMY_CONN`, `COEUS_API_URL`, database credentials, etc.) and GCP credentials mounted for the proxy (see `docker-compose.yml`).

### 1. Boot the System

```bash
docker compose up -d --build
```

### 2. Access the Dashboards

| Service | Direct URL | Traefik URL (port 81) | Credentials |
| :--- | :--- | :--- | :--- |
| **Control Plane** | `http://localhost:8001/admin` | `http://control-plane.localhost:81/admin` | `admin` / `admin` |
| **Airflow UI** | `http://localhost:9001` | `http://airflow.localhost:81` | `admin` / `admin` |
| **Active Pipelines API** | — | `http://control-plane.localhost:81/api/pipelines/active/` | N/A |
| **Traefik Dashboard** | `http://localhost:8080` | — | — |

On first boot, the control plane runs migrations and creates the default superuser automatically.

### 3. Run a Scraper Manually (optional)

```bash
docker exec -it coeus-worker python extraction_workers/sedarplus_scraper.py --pipeline_name <pipeline_id>
```

Scraped output is written under `./data/` (bind-mounted into workers).

---

## 🌐 Production Deployment

Production runs on a VPS (AbsoluteHosting) with automated deploys from GitHub Actions on every push to `main`.

| Concern | Implementation |
| :--- | :--- |
| **Compose file** | `docker-compose.prod.yml` |
| **Images** | `ghcr.io/8ohm-technologies/coeus-app:latest`, `ghcr.io/8ohm-technologies/coeus-worker:latest` |
| **CI/CD** | `.github/workflows/deploy.yml` — path-filtered image builds + SCP/SSH deploy |
| **TLS** | Traefik with Let's Encrypt certs (`traefik-config/certs.yml`) |
| **Airflow** | Split `airflow_scheduler` and `airflow_webserver` services |
| **Public URLs** | `https://8ohm.co.za`, `https://control-plane.8ohm.co.za`, `https://airflow.8ohm.co.za` |

Required GitHub secrets: `SSH_HOST`, `SSH_USER`, `SSH_KEY`, plus registry access for GHCR pulls on the VPS.

---

## 📂 Project Structure

```text
├── control_plane/           # Django project (pipelines + extracted_data apps)
├── extraction_workers/      # Playwright scrapers, LLM extractor, Pydantic schemas
├── orchestration/           # Airflow dynamic DAG factory
├── solver/                  # hCaptcha vision solver package (Grounding DINO + Qwen)
├── landing_page/            # Static marketing site (production Nginx volume)
├── traefik-config/          # Production TLS certificate paths for Traefik file provider
├── architecture/            # Architecture decision records (ADRs)
├── database/                # SQL initialization scripts
├── .github/workflows/       # CI/CD (build, push, deploy)
├── Dockerfile.app           # Control plane image
├── Dockerfile.worker        # Playwright + Chromium worker image
├── docker-compose.yml       # Local development stack
└── docker-compose.prod.yml  # Production VPS stack
```

---

## 📋 Architecture Decision Records

Design rationale for major choices is documented under `architecture/decision-records/`:

- **001** — Scraping framework selection (Playwright vs alternatives)
- **002** — LLM extraction and Pydantic schema enforcement
- **003** — Orchestration engine (Airflow)

---

## 🔮 Future Enhancements

The production stack already covers managed database access, CI/CD, and TLS. Remaining improvements worth considering:

- **Executor upgrade:** Move from `DockerOperator` to `KubernetesExecutor` for native pod-per-task scaling.
- **Object storage:** Offload PDF archives from local `./data` volumes to GCS/S3 with lifecycle policies.
- **Proxy rotation:** Residential proxy pools for high-friction regulatory portals.
- **Observability:** Sentry integration in workers and structured pipeline run metrics.
- **Secrets management:** Migrate `.env` values to a dedicated secrets store.

---

## 📄 Related Documentation

- [TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md) — Comprehensive technical analysis of the Coeus scraping, extraction, and orchestration architecture.

