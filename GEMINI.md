# 🌌 Project COEUS: GEMINI context

COEUS is a distributed data platform for automated scraping, extraction, and analysis of multi-source documentation. It handles complex web environments, including anti-bot measures, using a modular architecture.

## 🏗️ System Architecture

- **Control Plane (Django):** Manages pipeline configurations and metadata. Located in `control_plane/`.
- **Orchestrator (Apache Airflow):** Dynamically generates DAGs based on Control Plane configurations. Located in `orchestration/`.
- **Extraction Workers:** Ephemeral containers running Playwright/BeautifulSoup scrapers. Located in `extraction_worker/`.
- **HCaptcha Solver:** Vision-based solver using Hugging Face models (Grounding DINO, Qwen). Located in `solver/`.
- **Storage:** PostgreSQL for state and orchestration history. Local/S3 for documents.

## 🛠️ Key Technical Features

### 1. Dynamic DAG Factory
The orchestrator polls the Django API at `http://coeus-control-plane:8000/api/pipelines/active/` to create DAGs for each active `PipelineConfiguration`.

### 2. Adaptive Scraping Strategy
Based on `PipelineConfiguration`, the system selects the appropriate worker script:
- `ccma_playwright_scraper.py` for CCMA.
- `lotto_scraper.py` for national lottery.
- `mining_scraper.py` for SEDAR+ (with hCaptcha solving).

### 3. AI-Powered hCaptcha Solver
Uses `VisionManager` for object detection and `PromptTranslator` for prompt engineering. Slices the 3x3 grid for high-precision inference.

## 🚀 Development Guide

### Building and Running
The system is fully containerized.
- **Boot All Services:** `docker-compose up -d --build`
- **Django Admin:** `http://localhost:8000/admin` (admin/admin)
- **Airflow UI:** `http://localhost:9000` (admin/admin)

### Development Conventions
- **Model-Driven Pipelines:** All scrapers must be configurable via the Django `PipelineConfiguration` model.
- **Ephemeral Workers:** Workers should be stateless and use volumes/mounts for persistence (`/app/data/scraped_pdfs`).
- **Logging:** Use the project's logging format (`%(asctime)s [%(levelname)s] %(message)s`).
- **Testing:** New features should include integration tests or be verified via the ephemeral worker tasks.

### Core Files
- `control_plane/pipelines/models.py`: Pipeline configuration definitions.
- `extraction_worker/mining_scraper.py`: Playwright scraper for Sedarplus mining documents, with captcha integration.
- `extraction_worker/ccma_scraper.py`: Playwright scraper for CCMA documents.
- `extraction_worker/lotto_scraper.py`: Playwright scraper for South African lotto results.
- `orchestration/airflow_dags/dynamic_factory.py`: Airflow DAG generator.
- `solver/src/solver/solver.py`: hCaptcha solving orchestration.

## 📂 Project Structure
- `control_plane/`: Django source code.
- `extraction_worker/`: Scraping scripts and LLM extraction logic.
- `orchestration/`: Airflow configuration and dynamic DAGs.
- `solver/`: Vision and LLM models for CAPTCHA bypass.
- `data/`: Local storage for scraped outputs and debug snapshots.
