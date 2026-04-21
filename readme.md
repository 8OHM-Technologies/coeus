# 🌌 Project COEUS: Dynamic PDF Intelligence Pipeline

Project Codename: **COEUS** (named after the Greek Titan god of intellect, heavenly axis, and inquisitive mind - and I like codenames) is a showcase of a distributed data platform designed to automate the extraction and analysis of complex regulatory filings (NI 43-101 technical reports). Includes a custom built hCaptcha solver using intelligent hCaptcha prompt-translation and zero-shot object detection.

The system leverages a **Control Plane architecture** where a Django-based UI manages scraping blueprints, which are then dynamically orchestrated by **Apache Airflow** to spin up ephemeral **Playwright** workers.

## 🏗️ System Architecture

The platform is built on a decoupled, microservices-oriented architecture:

-   **Control Plane (Django):** The source of truth. Manages pipeline configurations, target URLs, and scheduling metadata.
    
-   **Orchestrator (Airflow 2.9.1):** Dynamically generates DAGs by polling the Control Plane API. It uses the `DockerOperator` to ensure workers are isolated and ephemeral.
    
-   **Muscles (Playwright Worker):** An optimized Chromium-based container that handles complex SPA interactions (dropdowns, inputs, and search execution) to find and extract document links.
    
-   **Storage (PostgreSQL):** A centralized database for both application metadata and Airflow orchestration history.
    
-   **Gateway (Traefik):** Handles internal routing and provides a professional `coeus.localhost` entry point.

## 🚀 Quick Start

The environment is fully containerized and configured for **Zero-Friction Access**. No manual user creation or database migrations are required.

### 1. Boot the System
Ensure you have Docker and Docker Compose installed, then run:
```
docker-compose up -d --build
```
### 2. Access the Dashboards
|**Service**|**URL**|**Credentials**|
|--|--|--|
|Control Plane|http://localhost:8000/admin|`admin` / `admin`
|Airflow UI|http://localhost:9000|`admin` / `admin`
|DAG API Endpoint|http://coeus.localhost/api/pipelines/active/|N/A

## 🛠️ Key Technical Features

### **Dynamic DAG Generation**

Instead of static `.py` files, the Airflow scheduler utilizes a **DAG Factory**. It polls the Control Plane API and creates a pipeline for every "Active" configuration in the database. This allows the system to scale to thousands of targets without restarting the orchestrator.

### **Ephemeral Worker Pattern**

To prevent memory leaks and "zombie" browser processes, the Scraper does not run inside Airflow. Airflow acts as a remote commander, spinning up a fresh `coeus_worker_image` for each task and destroying it immediately upon completion.

### **SPA Interaction Logic**

The worker uses Playwright's asynchronous engine to navigate the **SEDAR+** platform. It bypasses complex ARIA-hidden elements and dynamic loaders by using locator-based interactions that mimic human behavior, ensuring high accuracy and reliability.

## 📂 Project Structure

```
├── /
│   ├── control_plane/       # Django Project
│   ├── extraction_worker/   # Playwright Scraper
│   └── orchestration/       # Airflow DAG Factory
├── database/
│   └── init.sql             # Multi-tenant DB initialization
├── Dockerfile.app           # Optimized for Web/API
├── Dockerfile.worker        # Heavy-lift image with Chromium binaries
└── docker-compose.yml       # The infrastructure manifest
```

## 🏗️ Going from Demo to Production

While this repository provides a fully functional local environment, a production-grade deployment would migrate several components to cloud-native managed services to ensure high availability, security, and infinite scalability.

### **1. Infrastructure & Orchestration**

-   **From `Standalone` to `Celery/Kubernetes Executor`:** In production, Airflow wouldn't run as a single process. We would use the **KubernetesExecutor**, where every task (the scraper) is spun up as a literal Pod in a K8s cluster. This allows for massive parallel scraping across multiple nodes.
    
-   **Managed Airflow:** Instead of self-hosting, we would migrate to **AWS MWAA** or **Google Cloud Composer** to offload the maintenance of the Airflow metadata database and webserver. We would also preferably move Airflow's metadata DB to the Postgres instance instead of using SQLite

### **2. Data & Storage**

-   **Managed Databases:** The containerized PostgreSQL would be replaced by a managed service like **AWS RDS** or **GCP Cloud SQL**. This provides automated backups, multi-AZ failover, and encrypted-at-rest storage.
    
-   **Object Storage (S3/GCS):** The `scraped_pdfs` Docker volume is a "local-only" solution. In production, the worker would stream the extracted PDFs directly to **Amazon S3** or **Google Cloud Storage** with lifecycle policies to move older reports to cold storage (Glacier).

### **3. The "Muscle" (Playwright) at Scale**

-   **Proxy Rotation:** To avoid being blocked by SEDAR+ or other regulatory bodies, the Playwright worker would be integrated with a **Residential Proxy Manager** (like Bright Data or Oxylabs) to rotate IP addresses for every request.
    
-   **Headless Clusters:** For high-volume scraping, we would use a specialized browser-less grid like **Browserless.io**, allowing the worker containers to be even lighter since they wouldn't need to carry the heavy Chromium binaries themselves.

### **4. Security & Secrets**

-   **Secrets Management:** Hardcoded passwords in `docker-compose.yml` would be moved to **AWS Secrets Manager**.
    
-   **TLS/SSL:** Traefik would be configured with **Let's Encrypt** or AWS Certificate Manager (ACM) to ensure all traffic to the Control Plane and Airflow UI is encrypted via HTTPS.

### **5. Observability**

-   **Error Tracking:** Integration with **Sentry** would be added to the Django and Playwright code to capture stack traces of failed scrapes in real-time.
    
-   **Monitoring:** Possibly export Airflow metrics to **Prometheus & Grafana** to track "Scrape Success Rate" and "Worker Latency" over time.
