# 🌌 Project COEUS: GEMINI context

COEUS is a distributed data platform for automated scraping, extraction, and analysis of multi-source documentation. It handles complex web environments, including anti-bot measures, using a modular architecture.

## 🏗️ System Architecture

- **Extracted Data (Django):** Manages entity, target, and record metadata. Located in `control_plane/extracted_data/`.
- **Orchestrator (Apache Airflow):** Dynamically generates DAGs based on Control Plane configurations. Located in `orchestration/`.
- **Extraction Workers:** Ephemeral containers running Playwright/BeautifulSoup scrapers. Located in `extraction_workers/`.
- **HCaptcha Solver:** Vision-based solver using Hugging Face models (Grounding DINO, Qwen). Located in `solver/`.
- **Storage:** PostgreSQL for state and orchestration history. Local/S3 for documents.

## 🛠️ Key Technical Features

### 1. Dynamic DAG Factory
The orchestrator polls the Django API at `http://coeus-control-plane:8001/api/pipelines/active/` to create DAGs for each active `PipelineConfiguration`.

### 2. Adaptive Scraping Strategy
Based on `PipelineConfiguration`, the system selects the appropriate worker script:
- `sedarplus_scraper.py` for SEDAR+ (supports dynamic document types, Playwright + Solver).
- `sabinet_scraper.py` for Sabinet CCMA Labor Awards (index extraction, Playwright).
- `sabinet_detail_scraper.py` for Sabinet Award Details (follows index JSON, Playwright).
- `ccma_playwright_scraper.py` for CCMA (multi-category traversal + download utility, Playwright).
- `judiciary_scraper.py` for South African Judiciary Judgments (limit auto-expander, Playwright).
- `saflii_scraper.py` for SAFLII Case Law (basic crawling with Xvfb, Playwright).
- `mantech_scraper.py` for Mantech Electronics Store (ASP.NET WebForms paginated table, Playwright).
- `livestainable_scraper.py` for Livestainable Products (e-commerce catalog, Playwright).
- `lotto_scraper.py` for South African lotto results (national lottery parser, Playwright).

### 3. AI-Powered hCaptcha Solver
Uses `VisionManager` for object detection and `PromptTranslator` for prompt engineering. Slices the 3x3 grid for high-precision inference.

## 🚀 Development Guide

### Building and Running
The system is fully containerized.
- **Boot All Services:** `docker-compose up -d --build`
- **Django Admin (Direct):** `http://localhost:8001/admin` (admin/admin)
- **Django Admin (Traefik):** `http://control-plane.localhost:81/admin`
- **Airflow UI (Direct):** `http://localhost:9001` (admin/admin)
- **Airflow UI (Traefik):** `http://airflow.localhost:81`

### Development Conventions
- **Model-Driven Pipelines:** All scrapers must be configurable via the Django `PipelineConfiguration` model.
- **Ephemeral Workers:** Workers should be stateless and use volumes/mounts for persistence (`/app/data/scraped_pdfs`).
- **Logging:** Use the project's logging format (`%(asctime)s [%(levelname)s] %(message)s`).
- **Testing:** New features should include integration tests or be verified via the ephemeral worker tasks.

### Core Files
- `control_plane/pipelines/models.py`: Pipeline configuration definitions.
- `control_plane/extracted_data/models.py`: Entity and Target data models.
- `extraction_workers/sedarplus_scraper.py`: Playwright scraper for Sedarplus documents.
- `extraction_workers/ccma_playwright_scraper.py`: Playwright scraper for CCMA documents.
- `extraction_workers/lotto_scraper.py`: Playwright scraper for South African lotto results.
- `extraction_workers/saflii_scraper.py`: Playwright scraper for SAFLII case law.
- `extraction_workers/mantech_scraper.py`: Playwright scraper for Mantech electronics store.
- `extraction_workers/livestainable_scraper.py`: Playwright scraper for Livestainable products.
- `extraction_workers/sabinet_scraper.py`: Playwright scraper for Sabinet indexes.
- `extraction_workers/sabinet_detail_scraper.py`: Playwright scraper for Sabinet details.
- `extraction_workers/judiciary_scraper.py`: Playwright scraper for South African Judiciary.
- `orchestration/airflow_dags/dynamic_factory.py`: Airflow DAG generator.
- `solver/src/solver/solver.py`: hCaptcha solving orchestration.

## 📂 Project Structure
- `control_plane/`: Django source code.
- `extraction_workers/`: Scraping scripts and LLM extraction logic.
- `orchestration/`: Airflow configuration and dynamic DAGs.
- `solver/`: Vision and LLM models for CAPTCHA bypass.
- `data/`: Local storage for scraped outputs and debug snapshots.
