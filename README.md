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
* **Stage Control & Driver Timeouts**: Supports `skip_stages` execution flags (either via the `--skip_stages` CLI argument or defined inside Django `extraction_params` configuration) to control pipeline stages (e.g., detailing-only or indexing-only) for increased stability.

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
* **Indexed Performance**: Database fields (`scraped_at`, `detailed_at`, `dataset_number`, `url`) are indexed for fast lookups.
* **Multi-DB Sync**: Includes Django management commands (`sync_oc_to_master`) to bi-directionally synchronize `ExtractedRecord` entries between dual Oracle DB and primary PostgreSQL databases using bulk operations and target caching.

### **6. Scraper-Type Analytics & Background Scheduler**
* **Scraper-Type Aggregation**: Aggregates scraping analytics metrics (`control_plane/pipelines/analytics.py`) grouped by `scraper_type` rather than individual pipeline configurations.
* **Background Scheduler**: A dedicated background scheduler service periodically calculates scrape rates, success metrics, and status breakdowns, caching results in `ScrapingPipelineMetrics`.

### **7. Section Parsing (`ParsedRecord`), 3-Pass Section-Targeted LLM Extraction & PII Redaction (`ScrubbedRecord`)**

The data extraction and record cleaning pipeline transforms raw scraped document records (`extracted_records`) into structured, scrubbed, and compliant database entries (`scrubbed_records`) via a multi-stage dataflow:

* **Step 1: Authoritative System Metadata Resolution (Database SELECT JOIN)**
  * Executes a database SQL `SELECT` joining `extracted_records e`, `targets t` (`e.target_id = t.id`), and `entities ent` (`t.entity_id = ent.id`).
  * Resolves authoritative system metadata directly from database columns: `entity_name` (`ent.name`, e.g. `"Saflii"`), `target_name` (`t.target_name`, e.g. `"ZAGPJHC"`), `document_date` (`e.document_date`), `record_type` (`e.record_type`), and `case_number` (`e.data->'case_number'`).
  * **Strict Constraint**: System metadata fields (`BaseExtractedRecord`) are **never provided in the LLM prompt or JSON schema**, preventing LLM hallucination on administrative data.

* **Step 2: Mandatory Document Section Parsing (`saflii_document_parser.py` ➔ `parsed_records`)**
  * A document section parser is required in Django `extraction_params` (e.g. `"parser": "saflii_document_parser"`).
  * `llm_extractor.py` executes the section parser first in batches (default 250 records/batch) strictly on records in the `"cases"` category (skipping journals, gazettes, and court rolls), splitting raw court documents into structured sections (`header`, `judgment`, `order`, `appearances`, `footnotes`) while avoiding memory spikes and query timeouts.
  * **Text Cleaning (`clean_saflii_text`)**: Automatically strips website navigation breadcrumbs (`"LawCite"` and preceding text), SAFLII website UI noise lines (e.g. `"Download original files"`, `"PDF format"`, `"RTF format"`, `"Links to summary"`, `"Heads of argument"`), and excessive blank line runs from headers, full text, and journal articles.
  * **Regex & HTML Parsing**:
    * **Header**: Isolates court jurisdiction, judges, parties, and dates using cleaned header text.
    * **Footnotes & Link Targets**: Uses BeautifulSoup to extract HTML `<a>` link targets into structured URL objects (`[{"text": "...", "url": "..."}]`) and footnote text into `raw_text`. Includes trailing order rescue logic: if footnotes appear prior to the order in the judgment document, the order and any following appearances are preserved and restored.
    * **Judgment vs. Order Split**: Splits text into Judgment Body and Final Order using comprehensive South African order patterns (explicit order headers, plural orders, appellate proposals, Afrikaans rulings such as `van die hand gewys` / `van die rol geskrap`, sentencing substitutions, inline rulings, or judge signature boundaries). Order extraction is decoupled before Appearances to prevent premature truncation.
  * **Automated Data Quality & Human Review Flagging**: If any core section (`header`, `judgment`, `order`) is missing (`null_values`), the parent `extracted_record` is automatically updated in PostgreSQL with `requires_human_review = TRUE` and `review_reason = 'Document parsing failed'`. Clean sections are saved into `parsed_records` (1:1 with `extracted_records`).

* **Step 3: Programmatic & 3-Pass Section-Targeted LLM Context Routing (`llm_extractor.py`)**
  * **Programmatic Non-Case Extraction**: Non-case categories (e.g. `journals`, `gaz`) bypass LLM extraction; their raw content is cleaned with `clean_saflii_text` (stripping LawCite navigation breadcrumbs and UI noise lines) and structured directly into `SafliiJournalGazetteExtraction`.
  * **3-Pass Court Case Extraction**: To prevent local LLM context overflow on long court judgments (50k+ chars), case extraction is routed in **3 targeted passes** using isolated schema subsets:
  * **Pass 1 (Header Fields 1–8)**: Evaluates `SafliiHeaderData` (`applicant_plaintiff` to `court_location`) using **Header section context only**. If the LLM call times out or fails on identifying fields, a regex heuristic fallback extracts court, parties, and date from record metadata before flagging human review. If all extractions fail, the record is flagged for human review (`requires_human_review = TRUE`) with missing fields recorded in `review_reason` and skipped, avoiding full-document context overflows.
  * **Pass 2 (Footnotes & Precedents)**: Evaluates `SafliiPrecedentsData` (`precedents_cited` list of structured `PrecedentCategory` objects: `case_name`, `case_number`, `neutral_citation`, `commercial_citations`, `decision_date`, `treatment`, `reasoning`, `url`, `raw_citation`) using **Footnotes section context only**.
    * **Target Matcher & Citation Parser Post-Processing**: Runs `parse_legal_citation_string()` on all citations, compares output against HTML `targets` list, injects exact URLs, and appends missing link targets to guarantee **100% citation target coverage**.
  * **Pass 3 (Judgment Body Fields 9–14)**: Evaluates `SafliiBodyData` (`ratio_decidendi`, `obiter_dicta`, `order`, `summary`, `keywords`) using **Judgment + Order section context**.

* **Step 4: Payload Merging, Standardization & System Metadata Wrapping**
  * Combines sub-results, applies fallback defaults for missing optional text fields, and standardizes court names (`normalize_court_name`) to canonical South African court names and formats judge names (`format_judge_name`) to Title Case with uppercase judicial title acronyms.
  * Validates against `SafliiExtractedData` and programmatically attaches system `BaseExtractedRecord` metadata (`entity_name`, `target_name`, `document_date`, `record_type`, `case_number`).

* **Step 5: Compliance PII Scrubbing (`pii_scrub.py`)**
  * Recursively scrubs sensitive South African personal identifiers using regex patterns:
    * **RSA ID Numbers**: `\b\d{13}\b` ──► `"[RSA ID]"`
    * **Passport Numbers**: `\b[A-Za-z]\d{8}\b` ──► `"[PASSPORT]"`
    * **Bank & Tax Accounts**: 10–16 digit sequences ──► `"[BANK/TAX NUMBER]"`

* **Step 6: PostgreSQL Upsert & Scrubbed Status Marking (`scrubbed_records`)**
  * Upserts the cleaned, scrubbed, and validated JSON payload into `scrubbed_records` (JSONB `data` column, with `created_at` and `updated_at`).
  * Updates `extracted_records.scrubbed_at = NOW()` and `extracted_records.updated_at = NOW()`, marking the record processing lifecycle as complete.

### **8. Pipeline Maintenance & Normalization Scripts**
* **Failed SAFLII Records Reprocessor** (`scripts/reprocess_failed_saflii_records.py`): Batch reprocessor that re-runs the enhanced section parser over records flagged for human review under `'Document parsing failed'`, updates `parsed_records.data`, and clears `requires_human_review` back to `FALSE`. Supports `--dry-run`, `--limit`, `--force`, and `--batch-size`.
* **Footnotes & Structured Precedents Normalizer** (`scripts/fix_saflii_footnotes_records.py`): Migrates legacy `citations` in `parsed_records` to `footnotes`, and standardizes `precedents_cited` in `scrubbed_records` into the 5-part structured citation schema (`case_name`, `case_number`, `neutral_citation`, `commercial_citations`, `decision_date`, `treatment`, `reasoning`, `url`, `raw_citation`).
* **Case Records Court & Judge Normalizer** (`scripts/fix_saflii_case_records.py`): Standardizes existing court names and Title Case judge name formatting across `scrubbed_records`.
* **Case Records Document Date Normalizer** (`scripts/fix_saflii_document_dates.py`): Extracts and normalizes exact document dates from case record titles across `scrubbed_records` metadata and `extracted_records`.
* **PDF Case Titles & Dates Normalizer** (`scripts/fix_saflii_pdf_case_titles.py`): Fetches companion SAFLII `.html` pages to resolve true case titles and decision dates for PDF-sourced cases across `extracted_records` and `scrubbed_records`.
* **Journal Records Cleaner** (`scripts/fix_saflii_journal_records.py`): Strips website navigation breadcrumbs and UI noise lines from existing journal entries in `scrubbed_records`.
* **Case Reset Tool** (`scripts/reset_saflii_cases.py`): Resets case extraction states across database tables to re-trigger parsing and multi-pass extraction.
* **DuckDB Data Analysis Tool** (`scripts/analyze_duckdb.py`): In-memory analytical engine and interactive REPL over `coeus` PostgreSQL data.

---

## 🕵️ Scraper Registry

Specialized worker scripts reside in `extraction_workers/`:

| Scraper Script | Target Platform / Data Type | Technology & Evasion Strategy |
| :--- | :--- | :--- |
| [sedarplus_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sedarplus_scraper.py) | **SEDAR+ Corporate Filings** | Playwright + `misstcha.HCaptchaSolver` (Grounding DINO + Qwen) |
| [sabinet_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/sabinet_scraper.py) | **Sabinet CCMA Labor Awards** | SeleniumBase UC Mode, dynamic date range selection, rate-limit throttling, direct DB storage |
| [new_saflii_scraper.py](file:///home/tiaanf/Dev/coeus/extraction_workers/new_saflii_scraper.py) | **SAFLII Case Law** | SeleniumBase UC Mode + BeautifulSoup, Turnstile bypass, automatic case re-queuing, proxy rotation, dynamic year progress tracking |
| [saflii_document_parser.py](file:///home/tiaanf/Dev/coeus/extraction_workers/utils/saflii_document_parser.py) | **SAFLII Document Segmentation** | Multi-pattern regex engine for splitting SAFLII court judgments into Header/Intro, Judgment Body, Order/Ruling, and Appearances |
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
├── scripts/                 # Maintenance, data analysis, and OCR extraction utility scripts
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

## 🛠️ Utility Scripts

The `scripts/` folder provides standalone CLI tools for database analysis, maintenance, and document processing:

* **Image OCR Text Extraction** ([`scripts/extract_image_ocr.py`](file:///home/tiaanf/Dev/coeus/scripts/extract_image_ocr.py)):
  Extracts text from images using RapidOCR (ONNX Runtime, pure-Python) or Tesseract with optional image preprocessing (`binarize`, `grayscale`, `denoise`) and JSON output with bounding boxes:
  ```bash
  # Plain text extraction:
  python scripts/extract_image_ocr.py document.png

  # Structured JSON with bounding boxes and line confidences:
  python scripts/extract_image_ocr.py document.png --format json

  # Save to file with preprocessing:
  python scripts/extract_image_ocr.py document.png --preprocess binarize -o output.txt
  ```
* **DuckDB Data Analysis** ([`scripts/analyze_duckdb.py`](file:///home/tiaanf/Dev/coeus/scripts/analyze_duckdb.py)): High-performance analytical queries and text search on PostgreSQL state database.
* **SAFLII Case Numbers Normalizer** ([`scripts/fix_saflii_case_numbers.py`](file:///home/tiaanf/Dev/coeus/scripts/fix_saflii_case_numbers.py)): Extracts authoritative case numbers from title strings (matching `(CASE_NUM) [YEAR] ZA...`) and judgment headers, replaces corrupt neutral citations and leaked precedent citations, and normalizes `case_number` across `scrubbed_records` and `extracted_records`:
  ```bash
  # Preview case numbers to normalize without modifying data:
  python scripts/fix_saflii_case_numbers.py --dry-run

  # Normalize and update case numbers across all targets:
  python scripts/fix_saflii_case_numbers.py --force

  # Target specific court (e.g. ZACC, ZACT, ZASCA):
  python scripts/fix_saflii_case_numbers.py --target ZACC --force
  ```
* **SAFLII Record Maintenance**: Utilities for resetting extraction states and normalizing case records, dates, and footnotes (`reset_saflii_cases.py`, `fix_saflii_case_records.py`, `fix_saflii_document_dates.py`, etc.).

---

## 🔮 Future Enhancements

* **Proxy Pool Auto-Scaling**: Dynamic proxy pool integration within `BaseScraper` with automated performance scoring and failover.
* **Kubernetes Pipes Executor**: Transition from `PipesDockerClient` to `PipesK8sClient` for container autoscaling across Kubernetes clusters.
* **Fleet Observability**: Prometheus metrics and OpenTelemetry tracing across ephemeral scraper execution workers.

---

## 📄 Related Documentation

* [TECHNICAL_OVERVIEW.md](file:///home/tiaanf/Dev/coeus/TECHNICAL_OVERVIEW.md) — Comprehensive technical overview of the COEUS scraping, extraction, and orchestration architecture.
