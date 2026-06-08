import logging
import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount

logger = logging.getLogger(__name__)

API_URL = os.getenv(
    "COEUS_API_URL"
)
HOST_DATA_PATH = os.getenv("HOST_DATA_PATH")

if not HOST_DATA_PATH:
    HOST_DATA_PATH = "/tmp/coeus_data"
    logger.warning(
        "Environment variable HOST_DATA_PATH is not set. "
        "Falling back to /tmp/coeus_data."
    )

host_workers_path = os.path.join(os.path.dirname(HOST_DATA_PATH), "extraction_workers")

if not API_URL:
    logger.warning("Environment variable COEUS_API_URL is not set.")

try:
    if not API_URL:
        blueprints = []
    else:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        blueprints = response.json().get("pipelines", [])
        logger.info("Loaded %s active pipeline blueprints from API.", len(blueprints))
except Exception as exc:
    logger.exception("Failed to fetch dynamic pipeline blueprints: %s", exc)
    blueprints = []

default_args = {
    "owner": "data_engineering",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}

# --- Dynamic Scraper Discovery ---
# Discovery path (where the Airflow Scheduler finds the files)
_discovery_candidates = [
    "/app/extraction_workers",
    "/opt/airflow/extraction_workers",
    os.path.join(os.path.dirname(__file__), "../extraction_workers"),
]
discovery_path = next(
    (path for path in _discovery_candidates if os.path.exists(path)),
    None,
)

scraper_map = {}
if discovery_path:
    for f in os.listdir(discovery_path):
        if f.endswith("_scraper.py"):
            key = f.replace("_scraper.py", "")
            scraper_map[key] = f"/app/extraction_workers/{f}"
else:
    logger.warning(
        f"Scrapers directory not found at {discovery_path}. Scraper map will be empty."
    )

for blueprint in blueprints:
    dag_id = f"extract_{blueprint['pipeline_id']}"
    target_url = blueprint["phase_1_ingestion"]["start_url"]

    # Routing Logic & Worker Arguments
    pipeline_id = blueprint["pipeline_id"]
    scraper_type = blueprint.get("scraper_type", "sedarplus")

    extraction_params = blueprint["phase_2_extraction"].get("extraction_params", {})
    search_keyword = extraction_params.get("search_keyword")
    category = extraction_params.get("category") or extraction_params.get("categories")

    worker_args = f"--pipeline_name '{pipeline_id}'"
    if scraper_type == "mantech":
        if search_keyword:
            worker_args += f" --search_keyword '{search_keyword}'"
        if category:
            if isinstance(category, list):
                category_str = ",".join(str(c) for c in category)
            else:
                category_str = str(category)
            worker_args += f" --category '{category_str}'"

    worker_script = scraper_map.get(scraper_type)
    if not worker_script:
        logger.warning(
            f"Unknown scraper type: {scraper_type}. Available: {list(scraper_map.keys())}"
        )

    dag = DAG(
        dag_id=dag_id,
        default_args=default_args,
        schedule=blueprint["schedule"],
        start_date=datetime(2024, 1, 1),
        catchup=False,
        tags=["dynamic_extraction"],
    )

    # 1. The Ingestion Task
    scrape_task = DockerOperator(
        task_id="run_scraper",
        image="ghcr.io/8ohm-technologies/coeus-worker:latest",
        docker_conn_id='github_container_registry',
        container_name=f"ephemeral_scraper_{blueprint['pipeline_id']}",
        docker_url="unix://var/run/docker.sock",
        network_mode="8ohm-network",
        environment={
            "PYTHONPATH": "/app",
            "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
            "PIPELINE_CONFIG": os.getenv(
                "COEUS_API_URL"
            ),
            # Storing pipeline specific settings in the dynamic factory's task environment
            "START_URL": target_url,
            "DOCUMENT_TYPE": blueprint["metadata"]["document_type"],
            "CAT_SELECTOR": blueprint["phase_1_ingestion"].get(
                "target_css_selector_categories", ""
            ),
            "DOC_SELECTOR": blueprint["phase_1_ingestion"].get(
                "target_css_selector_documents", ""
            ),
            "ALLOW_INSECURE_HTTPS": str(
                blueprint["phase_1_ingestion"].get("allow_insecure_https", False)
            ),
            "ALLOW_INSECURE_REQUESTS": str(
                blueprint["phase_1_ingestion"].get("allow_insecure_requests", False)
            ),
            "EXTRACTION_PARAMS": str(
                blueprint["phase_2_extraction"].get("extraction_params", {})
            ),
        },
        command=f"bash -c 'Xvfb :99 -screen 0 1280x720x24 & export DISPLAY=:99 && sleep 1 && python {worker_script} {worker_args}'" if scraper_type == "saflii" else f"python {worker_script} {worker_args}",
        auto_remove="force",
        mount_tmp_dir=False,
        mounts=[
            Mount(
                source=HOST_DATA_PATH,
                target="/app/data",
                type="bind",
            ),
            Mount(
                source=host_workers_path,
                target="/app/extraction_workers",
                type="bind",
            ),
            Mount(
                source="huggingface_cache",
                target="/root/.cache/huggingface",
                type="volume",
            ),
        ],
        dag=dag,
    )

    # 2. The Conditional Extraction Task
    if blueprint["phase_2_extraction"]["requires_extraction"]:
        extract_task = DockerOperator(
            task_id="run_llm_extraction",
            image="ghcr.io/8ohm-technologies/coeus-worker:latest",
            docker_conn_id='github_container_registry',
            container_name=f"ephemeral_extractor_{blueprint['pipeline_id']}",
            docker_url="unix://var/run/docker.sock",
            network_mode="8ohm-network",
            environment={
                "PYTHONPATH": "/app",
                "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
                "PIPELINE_NAME": blueprint["pipeline_id"],
                "EXTRACTION_INSTRUCTIONS": blueprint["phase_2_extraction"].get(
                    "extraction_instructions", ""
                ),
            },
            command=f"python /app/extraction_workers/llm_extractor.py --pipeline_name '{blueprint['pipeline_id']}' --schema '{blueprint['phase_2_extraction']['expected_schema']}'",
            auto_remove="force",
            mount_tmp_dir=False,
            mounts=[
                Mount(
                    source=HOST_DATA_PATH, target="/app/data/scraped_pdfs", type="bind"
                ),
                Mount(
                    source=host_workers_path, target="/app/extraction_workers", type="bind"
                ),
            ],
            dag=dag,
        )

        scrape_task.set_downstream(extract_task)
    else:
        pass

    globals()[dag_id] = dag
