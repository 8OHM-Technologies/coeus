import logging
import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount

logger = logging.getLogger(__name__)

API_URL = os.getenv(
    "COEUS_API_URL", "http://coeus-control-plane:8000/api/pipelines/active/"
)
HOST_PDF_PATH = os.getenv("HOST_SCRAPED_PDFS_PATH")

if not HOST_PDF_PATH:
    raise ValueError("Environment variable HOST_SCRAPED_PDFS_PATH is not set.")

HOST_LOTTO_PATH = os.getenv("HOST_LOTTO_PATH")

if not HOST_LOTTO_PATH:
    raise ValueError("Environment variable HOST_LOTTO_PATH is not set.")

try:
    response = requests.get(API_URL, timeout=10)
    response.raise_for_status()
    blueprints = response.json().get("pipelines", [])
except Exception:
    blueprints = []

default_args = {
    "owner": "data_engineering",
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}

for blueprint in blueprints:
    dag_id = f"extract_{blueprint['pipeline_id']}"
    target_url = blueprint["phase_1_ingestion"]["start_url"]

    # Routing Logic & Worker Arguments
    pipeline_id = blueprint["pipeline_id"]
    scraper_type = blueprint.get("scraper_type", "sedarplus")
    worker_args = f"--pipeline_name '{pipeline_id}'"

    scraper_map = {
        "ccma": "/app/extraction_workers/ccma_playwright_scraper.py",
        "lotto": "/app/extraction_workers/lotto_scraper.py",
        "judiciary": "/app/extraction_workers/judiciary_scraper.py",
        "sedarplus": "/app/extraction_workers/sedarplus_scraper.py",
    }
    worker_script = scraper_map.get(scraper_type)
    if not worker_script:
        logger.warning(f"Unknown scraper type: {scraper_type}.")

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
        image="coeus_worker_image:latest",
        container_name=f"ephemeral_scraper_{blueprint['pipeline_id']}",
        docker_url="unix://var/run/docker.sock",
        network_mode="coeus_network",
        environment={
            "PYTHONPATH": "/app:/app/solver/src",
            "HF_TOKEN": os.environ.get("HUGGINGFACE_TOKEN", ""),
            "PIPELINE_CONFIG": os.getenv(
                "COEUS_API_URL", "http://coeus-control-plane:8000/api/pipelines/active/"
            ),  # Fallback or marker
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
        command=f"python {worker_script} {worker_args}",
        auto_remove="force",
        mount_tmp_dir=False,
        mounts=[
            Mount(source=HOST_PDF_PATH, target="/app/data/scraped_pdfs", type="bind"),
            Mount(
                source=HOST_LOTTO_PATH, target="/app/data/lotto_results", type="bind"
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
            image="coeus_worker_image:latest",
            container_name=f"ephemeral_extractor_{blueprint['pipeline_id']}",
            docker_url="unix://var/run/docker.sock",
            network_mode="coeus_network",
            environment={
                "PYTHONPATH": "/app:/app/solver/src",
                "HF_TOKEN": os.environ.get("HUGGINGFACE_TOKEN", ""),
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
                    source=HOST_PDF_PATH, target="/app/data/scraped_pdfs", type="bind"
                )
            ],
            dag=dag,
        )

        scrape_task.set_downstream(extract_task)
    else:
        pass

    globals()[dag_id] = dag
