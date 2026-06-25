import json
import logging
import os
from datetime import datetime, timedelta

import requests
from airflow import DAG
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount

logger = logging.getLogger(__name__)

API_URL = os.getenv("COEUS_API_URL")
HOST_DATA_PATH = os.getenv("HOST_DATA_PATH")

if not HOST_DATA_PATH:
    HOST_DATA_PATH = "/tmp/coeus_data"
    logger.warning(
        "Environment variable HOST_DATA_PATH is not set. "
        "Falling back to /tmp/coeus_data."
    )

# The host-side path to the extraction_workers source directory (bind-mounted
# into every container so hot-fixing scripts doesn't require a full image rebuild).
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

# ---------------------------------------------------------------------------
# Dynamic Scraper Discovery
# The scheduler discovers the actual scraper files here; the DockerOperator
# always references /app/extraction_workers/ inside the container image.
# ---------------------------------------------------------------------------
_discovery_candidates = [
    "/app/extraction_workers",
    "/opt/airflow/extraction_workers",
    os.path.join(os.path.dirname(__file__), "../extraction_workers"),
]
discovery_path = next(
    (path for path in _discovery_candidates if os.path.exists(path)),
    None,
)

scraper_map: dict[str, str] = {}
if discovery_path:
    for f in os.listdir(discovery_path):
        if f.endswith("_scraper.py"):
            key = f.replace("_scraper.py", "")
            # Always use the container-internal path for the command string
            scraper_map[key] = f"/app/extraction_workers/{f}"
else:
    logger.warning(
        "Scrapers directory not found in any candidate path. Scraper map will be empty."
    )

# ---------------------------------------------------------------------------
# DAG Generation Loop
# ---------------------------------------------------------------------------
for blueprint in blueprints:
    pipeline_id: str = blueprint["pipeline_id"]
    dag_id: str = f"extract_{pipeline_id}"

    scraper_type: str = blueprint.get("scraper_type", "sedarplus")
    worker_script: str | None = scraper_map.get(scraper_type)

    if not worker_script:
        logger.error(
            "Unknown scraper type '%s' for pipeline '%s'. "
            "Available scrapers: %s. Skipping DAG creation.",
            scraper_type,
            pipeline_id,
            list(scraper_map.keys()),
        )
        continue

    # -- Phase 1: Ingestion config --
    phase1 = blueprint["phase_1_ingestion"]
    target_url: str = phase1["start_url"]

    # -- Phase 2: Extraction config --
    phase2 = blueprint["phase_2_extraction"]
    extraction_params: dict = phase2.get("extraction_params", {})
    expected_schema: str = phase2.get("expected_schema", "")
    llm_engine: str = phase2.get("engine", "ollama/phi4-mini")
    extraction_instructions: str = phase2.get("extraction_instructions", "")
    document_type: str = blueprint["metadata"].get("document_type", "pdf")

    # -- Build scraper CLI arguments --
    worker_args = f"--pipeline_name '{pipeline_id}'"

    if scraper_type == "mantech":
        search_keyword = extraction_params.get("search_keyword")
        category = extraction_params.get("category") or extraction_params.get("categories")
        if search_keyword:
            worker_args += f" --search_keyword '{search_keyword}'"
        if category:
            category_str = (
                ",".join(str(c) for c in category)
                if isinstance(category, list)
                else str(category)
            )
            worker_args += f" --category '{category_str}'"

    elif scraper_type in ("saflii", "new_saflii"):
        # Playwright runs non-headless inside an Xvfb virtual display
        worker_args += " --headless false"

    # -- Scraper command (SAFLII needs an Xvfb virtual display) --
    if scraper_type in ("saflii", "new_saflii"):
        scraper_command = (
            f"bash -c 'Xvfb :99 -screen 0 1280x720x24 & "
            f"export DISPLAY=:99 && sleep 1 && "
            f"python {worker_script} {worker_args}'"
        )
    else:
        scraper_command = f"python {worker_script} {worker_args}"

    # -- Shared environment passed to every scraper container --
    scraper_environment = {
        "PYTHONPATH": "/app:/app/extraction_workers",
        "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
        "PIPELINE_CONFIG": API_URL or "",
        # Pipeline-specific settings resolved by the factory so workers can
        # load them from env rather than making an API call at startup.
        "START_URL": target_url,
        "DOCUMENT_TYPE": document_type,
        "CAT_SELECTOR": phase1.get("target_css_selector_categories", ""),
        "DOC_SELECTOR": phase1.get("target_css_selector_documents", ""),
        "ALLOW_INSECURE_HTTPS": str(phase1.get("allow_insecure_https", False)),
        "ALLOW_INSECURE_REQUESTS": str(phase1.get("allow_insecure_requests", False)),
        "USE_PROXY": str(
            phase1.get("use_proxy", False)
            or extraction_params.get("use_proxy", False)
            or os.environ.get("USE_PROXY", "False").lower() == "true"
        ),
        # Always JSON-encoded so utils.fetch_pipeline_config can do a straight
        # json.loads() without any ast.literal_eval fallback.
        "EXTRACTION_PARAMS": json.dumps(extraction_params),
    }

    schedule_val = blueprint.get("schedule")
    if not schedule_val:
        schedule_val = None

    dag = DAG(
        dag_id=dag_id,
        default_args=default_args,
        schedule=schedule_val,
        start_date=datetime(2024, 1, 1),
        catchup=False,
        tags=["dynamic_extraction"],
    )

    # -----------------------------------------------------------------------
    # Task 1 — Scraper
    # -----------------------------------------------------------------------
    scrape_task = DockerOperator(
        task_id="run_scraper",
        image="ghcr.io/8ohm-technologies/coeus-worker:latest",
        docker_conn_id="github_container_registry",
        container_name=f"ephemeral_scraper_{pipeline_id}",
        docker_url="unix://var/run/docker.sock",
        network_mode="8ohm-network",
        environment=scraper_environment,
        command=scraper_command,
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

    # -----------------------------------------------------------------------
    # Task 2 — LLM Extractor (conditional)
    # -----------------------------------------------------------------------
    if phase2.get("requires_extraction"):
        extract_task = DockerOperator(
            task_id="run_llm_extraction",
            image="ghcr.io/8ohm-technologies/coeus-worker:latest",
            docker_conn_id="github_container_registry",
            container_name=f"ephemeral_extractor_{pipeline_id}",
            docker_url="unix://var/run/docker.sock",
            network_mode="8ohm-network",
            environment={
                "PYTHONPATH": "/app:/app/extraction_workers",
                "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
                "PIPELINE_NAME": pipeline_id,
                "DOCUMENT_TYPE": document_type,
                "EXTRACTION_INSTRUCTIONS": extraction_instructions,
                "AI_MODEL": llm_engine,
                # DB credentials forwarded from the Airflow worker environment
                "POSTGRES_HOST": os.environ.get("POSTGRES_HOST", ""),
                "POSTGRES_USER": os.environ.get("POSTGRES_USER", ""),
                "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
                "POSTGRES_DB": os.environ.get("POSTGRES_DB", ""),
            },
            command=(
                f"python /app/extraction_workers/llm_extractor.py "
                f"--pipeline_name '{pipeline_id}' "
                f"--schema '{expected_schema}'"
            ),
            auto_remove="force",
            mount_tmp_dir=False,
            mounts=[
                # Mount the full data root so the extractor can navigate
                # /app/data/{pipeline_id}/{document_type}/
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
            ],
            dag=dag,
        )

        scrape_task >> extract_task

    globals()[dag_id] = dag
