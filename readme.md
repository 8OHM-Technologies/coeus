# 🌌 Project COEUS: Dynamic PDF Intelligence Pipeline

**COEUS** (named after the Greek Titan of intellect and the heavenly axis) is a distributed data platform designed to automate the scraping, extraction, and analysis of multi-source documentation. 

The project demonstrates a scalable approach to handling varying degrees of web complexity: from **basic HTML parsing** on sites like **ccma.org.za** using Python and BeautifulSoup, to bypassing **advanced anti-bot measures** on high-security regulatory platforms like **sedarplus.ca** (e.g., NI 43-101 mining reports) using Playwright and Selenium.

---

## 🏗️ System Architecture

The platform utilizes a decoupled, microservices-oriented architecture designed for horizontal scalability:

* **Control Plane (Django):** The system's source of truth. It manages pipeline configurations, target metadata, and scraping "blueprints."
* **Orchestrator (Apache Airflow 2.9.1):** Dynamically generates DAGs by polling the Control Plane API. It utilizes the `DockerOperator` to ensure workers are isolated and ephemeral.
* **The "Muscles" (Playwright & BeautifulSoup Workers):** Task-specific containers that scale based on complexity. It handles everything from simple requests to complex SPA interactions (dropdowns, dynamic inputs) to extract deep-linked documentation.
* **Storage (PostgreSQL):** A centralized relational store for application state and orchestration history.
* **Gateway (Traefik):** Manages internal service discovery and provides a unified `coeus.localhost` entry point.

---

## 🛠️ Key Technical Features

### **1. Adaptive Scraping Strategy**
COEUS doesn't use a "one-size-fits-all" approach. Based on the target's complexity defined in the Control Plane:
* **Lightweight Extraction:** Rapidly parses unprotected sites using **BeautifulSoup**, minimizing resource overhead.
* **Heavy-Lift Automation:** Deploys **Playwright/Selenium** to navigate modern JavaScript-heavy frameworks and bypass ARIA-hidden elements or dynamic loaders.

### **2. Dynamic DAG Factory**
The scheduler polls the Django API to generate pipelines in real-time for every "Active" configuration. This enables the system to scale to thousands of targets without manual intervention or orchestrator restarts.

### **3. AI-Powered hCaptcha Solver**
Integrated bypass logic utilizing **Hugging Face Transformers**. The system performs:
* **Intelligent Prompt Translation:** Converting hCaptcha challenges into machine-readable queries.
* **Zero-Shot Object Detection:** Using vision models to identify and interact with CAPTCHA elements without pre-trained site-specific labels.

### **4. Ecommerce Aggregation Scheduler**
The control plane schedules background combination tasks (via Django-APScheduler) to automatically combine product datasets from Mantech and Livestainable, calculate markups, deduplicate, and trigger downstream e-commerce sync (Saleor sync).

### **5. Incremental Crawling & Rate Throttling**
Scrapers targeting large volumes (such as Sabinet CCMA Awards) utilize local output-aware incremental early stopping. They check scraped metadata `(award_number, title)` against existing database files to terminate execution immediately when cached records are hit. They also run page-based rate throttling (e.g. 60-second sleeps after every 100 pages) to bypass active bot mitigation.

---

## 🕵️ Scraper Registry

COEUS comes pre-equipped with specialized scraper worker scripts inside the `extraction_workers/` directory:

| Scraper Script | Target Platform / Data Type | Technology & Strategy |
| :--- | :--- | :--- |
| [sedarplus_scraper.py](file:///e:/Code/coeus/extraction_workers/sedarplus_scraper.py) | **SEDAR+ Corporate Filings** | Playwright + Hugging Face Vision Solver (HCaptcha bypass, zero-shot DINO+Qwen) |
| [sabinet_scraper.py](file:///e:/Code/coeus/extraction_workers/sabinet_scraper.py) | **Sabinet CCMA Labor Awards** | Playwright, incremental early-stopping, rate-limit sleep throttling, Ant Design select components |
| [ccma_playwright_scraper.py](file:///e:/Code/coeus/extraction_workers/ccma_playwright_scraper.py) | **CCMA Arbitration Documents** | Playwright multi-category traversal + download utility |
| [judiciary_scraper.py](file:///e:/Code/coeus/extraction_workers/judiciary_scraper.py) | **South African Judiciary Judgments** | Playwright limit auto-expander + document bulk downloader |
| [mantech_scraper.py](file:///e:/Code/coeus/extraction_workers/mantech_scraper.py) | **Mantech Electronics Store** | Playwright ASP.NET WebForms paginated table extraction |
| [livestainable_scraper.py](file:///e:/Code/coeus/extraction_workers/livestainable_scraper.py) | **Livestainable Products** | Playwright e-commerce scraper |
| [lotto_scraper.py](file:///e:/Code/coeus/extraction_workers/lotto_scraper.py) | **National Lottery Results** | Playwright historical data parser with CSV exporter |

---

## 🚀 Quick Start

The environment is fully containerized for **Zero-Friction Access**. No manual migrations or user creation required.

### 1. Boot the System
```bash
docker-compose up -d --build
```

### 2. Access the Dashboards
| Service | URL | Credentials |
| :--- | :--- | :--- |
| **Control Plane** | `http://localhost:8001/admin` | `admin` / `admin` |
| **Airflow UI** | `http://localhost:9001` | `admin` / `admin` |
| **DAG API** | `http://coeus.localhost/api/pipelines/active/` | N/A |

---

## 🏗️ Production Roadmap

To transition this proof-of-concept to a production-grade environment, the following migrations are recommended:

### **Cloud-Native Orchestration**
* **Executor Upgrade:** Move from `Standalone` to `KubernetesExecutor`, allowing every scraping task to run as a native K8s Pod for infinite horizontal scaling.
* **Managed Airflow:** Utilize **AWS MWAA** or **GCP Cloud Composer** to offload infrastructure maintenance.

### **Enterprise Data Handling**
* **Object Storage:** Transition from local Docker volumes to **Amazon S3** or **GCS** for PDF storage, implementing lifecycle policies for cold-storage (Glacier) archiving.
* **Managed Databases:** Migrate the containerized PostgreSQL to **AWS RDS** or **GCP Cloud SQL** for automated backups and multi-AZ failover.

### **Resilience & Stealth**
* **Proxy Rotation:** Integration with residential proxy managers (e.g., Bright Data) to rotate egress IPs and avoid rate-limiting on regulatory portals.
* **Headless Clusters:** Offload browser execution to specialized grids like **Browserless.io**, reducing the resource footprint of the worker containers.

### **Observability & Security**
* **Error Tracking:** Integrate **Sentry** within the Playwright workers to capture real-time stack traces of failed extraction attempts.
* **Secrets Management:** Replace hardcoded environment variables with **AWS Secrets Manager** or **HashiCorp Vault**.

---

## 📂 Project Structure
```text
├── control_plane/       # Django Project (The Brain)
├── extraction_workers/   # Playwright Scrapers (The Muscle)
├── orchestration/       # Airflow DAG Factory (The Nervous System)
├── database/            # Multi-tenant DB initialization
├── Dockerfile.app       # Optimized Web/API image
├── Dockerfile.worker    # Heavy-lift image with Chromium binaries
└── docker-compose.yml   # Infrastructure manifest
