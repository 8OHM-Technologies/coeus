import os
import json
import time
import logging
import requests
from typing import Optional

import dagster as dg
from dagster_docker import PipesDockerClient
try:
    from dagster_apprise import apprise_notifications, AppriseNotificationsConfig
    HAS_APPRISE = True
except ImportError:
    HAS_APPRISE = False
from github import Github

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = os.getenv("COEUS_API_URL", "http://coeus-control-plane:8001/api/pipelines/active/")
CACHE_FILE = os.getenv("COEUS_BLUEPRINTS_CACHE_FILE", "/app/data/coeus_blueprints_cache.json")
CACHE_TTL = int(os.getenv("COEUS_BLUEPRINTS_CACHE_TTL", 60))

SCRAPER_IMAGE = os.getenv(
    "DAGSTER_SCRAPER_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-scraper:latest",
)
EXTRACTOR_IMAGE = os.getenv(
    "DAGSTER_EXTRACTOR_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-extractor:latest",
)

HOST_DATA_DIR = os.getenv("COEUS_HOST_DATA_DIR", "/var/shared_scraping_data")
CONTAINER_DATA_DIR = "/app/data"
DOCKER_NETWORK = os.getenv("COEUS_DOCKER_NETWORK", "8ohm-network")
APPRISE_CONN_STRING = os.getenv("APPRISE_CONN_STRING")

apprise_dict = {}
if HAS_APPRISE and APPRISE_CONN_STRING:
    try:
        notification_config = AppriseNotificationsConfig(
            urls=[APPRISE_CONN_STRING],
            events=["STEP_FAILURE","RUN_FAILURE"],
            include_jobs=["*"]
        )
        apprise_dict = apprise_notifications(notification_config).to_dict()
    except Exception as e:
        logger.warning(f"Failed to configure Apprise notifications: {e}")

# ---------------------------------------------------------------------------
# Blueprint fetching (used only at sensor/schedule evaluation time)
# ---------------------------------------------------------------------------

def fetch_blueprints() -> list[dict]:
    """Fetch active pipeline blueprints from the Coeus API with caching + retry."""
    if not API_URL:
        logger.warning("Environment variable COEUS_API_URL is not set.")
        return []

    if os.path.exists(CACHE_FILE):
        try:
            mtime = os.path.getmtime(CACHE_FILE)
            if time.time() - mtime < CACHE_TTL:
                with open(CACHE_FILE, "r") as f:
                    cached_data = json.load(f)
                    logger.info(f"Loaded {len(cached_data)} blueprints from fresh cache.")
                    return cached_data
        except Exception as e:
            logger.warning(f"Failed to read cache file: {e}")

    max_retries = 6
    backoff_factor = 2
    initial_delay = 1
    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Attempt {attempt}/{max_retries} fetching blueprints from {API_URL}...")
            response = requests.get(API_URL, timeout=10)
            response.raise_for_status()
            blueprints = response.json().get("pipelines", [])

            try:
                os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
                with open(CACHE_FILE, "w") as f:
                    json.dump(blueprints, f)
            except Exception as e:
                logger.warning(f"Failed to write cache: {e}")

            logger.info(f"Loaded {len(blueprints)} blueprints from API.")
            return blueprints
        except Exception as exc:
            last_exception = exc
            logger.warning(f"Attempt {attempt} failed: {exc}")
            if attempt < max_retries:
                sleep_time = initial_delay * (backoff_factor ** (attempt - 1))
                logger.info(f"Retrying in {sleep_time}s...")
                time.sleep(sleep_time)

    logger.error(f"All {max_retries} attempts failed: {last_exception}")

    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cached_data = json.load(f)
                logger.warning(f"Falling back to EXPIRED cache ({len(cached_data)} blueprints).")
                return cached_data
        except Exception as e:
            logger.error(f"Failed to read fallback cache: {e}")

    return []


def to_bool(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def _blueprint_to_run_config(blueprint: dict) -> dict:
    phase1 = blueprint.get("phase_1_ingestion", {})
    phase2 = blueprint.get("phase_2_extraction", {})
    metadata = blueprint.get("metadata", {})
    extraction_params = phase2.get("extraction_params", {})

    use_proxy: bool = (
        to_bool(phase1.get("use_proxy"))
        or to_bool(extraction_params.get("use_proxy"))
        or to_bool(os.environ.get("USE_PROXY"))
    )

    return {
        "ops": {
            "raw_scraped_pages": {
                "config": {
                    "scraper_type": blueprint.get("scraper_type", "sedarplus"),
                    "document_type": metadata.get("document_type", "pdf"),
                    "start_url": phase1.get("start_url", ""),
                    "allow_insecure_https": to_bool(phase1.get("allow_insecure_https")),
                    "allow_insecure_requests": to_bool(phase1.get("allow_insecure_requests")),
                    "use_proxy": use_proxy,
                    "output_dir": CONTAINER_DATA_DIR,
                    "extraction_params": json.dumps(extraction_params),
                }
            },
            "extracted_structured_data": {
                "config": {
                    "requires_extraction": to_bool(phase2.get("requires_extraction")),
                    "document_type": metadata.get("document_type", "pdf"),
                    "start_url": phase1.get("start_url", ""),
                    "allow_insecure_https": to_bool(phase1.get("allow_insecure_https")),
                    "allow_insecure_requests": to_bool(phase1.get("allow_insecure_requests")),
                    "use_proxy": use_proxy,
                    "extraction_params": json.dumps(extraction_params),
                    "expected_schema": phase2.get("expected_schema", ""),
                    "llm_engine": phase2.get("engine", "ollama/phi4-mini"),
                    "extraction_instructions": phase2.get("extraction_instructions", ""),
                }
            },
        }
    }


def _build_container_env() -> dict[str, str]:
    forwarded = [
        "DAGSTER_POSTGRES_USER",
        "DAGSTER_POSTGRES_PASSWORD",
        "DAGSTER_POSTGRES_DB",
        "POSTGRES_HOST",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "COEUS_API_URL",
        "USE_PROXY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DAGSTER_PIPES_DEBUG",
        "OLLAMA_BASE_URL",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "HF_HOME",
    ]
    return {k: os.environ[k] for k in forwarded if k in os.environ}


# ---------------------------------------------------------------------------
# PartitionConfigs
# ---------------------------------------------------------------------------

pipeline_partitions = dg.DynamicPartitionsDefinition(name="pipelines")


def get_blueprint_for_partition(partition_key: str) -> Optional[dict]:
    """Helper to pull the specific configuration dictionary for this partition."""
    blueprints = fetch_blueprints()
    for bp in blueprints:
        if str(bp.get("pipeline_id")) == partition_key:
            return bp
    return None

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@dg.resource
def github_pat_resource(_):
    token = dg.EnvVar("GITHUB_ACCESS_TOKEN").get_value()
    return Github(token)

# ---------------------------------------------------------------------------
# Assets (Pipes Docker Execution)
# ---------------------------------------------------------------------------

@dg.asset(required_resource_keys={"github_api"})
def fetch_github_repo_info(context):
    github_client = context.resources.github_api
    
    repo = github_client.get_repo(os.environ["GITHUB_REPO"])
    context.log.info(f"Successfully connected! Repository name: {repo.name}")
    return repo.stargazers_count

@dg.asset(
    name="raw_scraped_pages",
    partitions_def=pipeline_partitions,
)
def raw_scraped_pages(
    context: dg.AssetExecutionContext,
    pipes_docker: PipesDockerClient,
) -> dg.MaterializeResult:
    """Spawns an external container to handle data ingestion via Dagster Pipes."""
    blueprint = get_blueprint_for_partition(context.partition_key)
    if not blueprint:
        raise ValueError(f"No active pipeline blueprint found for partition key: {context.partition_key}")

    phase1 = blueprint.get("phase_1_ingestion", {})
    phase2 = blueprint.get("phase_2_extraction", {})
    metadata = blueprint.get("metadata", {})
    extraction_params = phase2.get("extraction_params", {})

    use_proxy: bool = (
        to_bool(phase1.get("use_proxy"))
        or to_bool(extraction_params.get("use_proxy"))
        or to_bool(os.environ.get("USE_PROXY"))
    )

    # Consolidate your configuration directly into the container extras payload
    extras = {
        "scraper_type": blueprint.get("scraper_type", "sedarplus"),
        "document_type": metadata.get("document_type", "pdf"),
        "start_url": phase1.get("start_url", ""),
        "allow_insecure_https": to_bool(phase1.get("allow_insecure_https")),
        "allow_insecure_requests": to_bool(phase1.get("allow_insecure_requests")),
        "use_proxy": use_proxy,
        "output_dir": CONTAINER_DATA_DIR,
        "extraction_params": json.dumps(extraction_params),
        "partition_key": context.partition_key,
        "subset": blueprint.get("subset", ""),
        "pipeline_name": blueprint.get("name", ""),
    }

    result = pipes_docker.run(
        context=context,
        image=SCRAPER_IMAGE,
        env=_build_container_env(),
        extras=extras,
        container_kwargs={
            "network": DOCKER_NETWORK,
            "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
            "user": "root", 
        }
    )
    return result.get_materialize_result()


@dg.asset(
    name="extracted_structured_data",
    partitions_def=pipeline_partitions,
    deps=[raw_scraped_pages],
)
def extracted_structured_data(
    context: dg.AssetExecutionContext,
    pipes_docker: PipesDockerClient,
) -> dg.MaterializeResult:
    """Spawns an external container to extract structured data via Dagster Pipes."""
    blueprint = get_blueprint_for_partition(context.partition_key)
    if not blueprint:
        raise ValueError(f"No active pipeline blueprint found for partition key: {context.partition_key}")

    phase1 = blueprint.get("phase_1_ingestion", {})
    phase2 = blueprint.get("phase_2_extraction", {})
    metadata = blueprint.get("metadata", {})
    extraction_params = phase2.get("extraction_params", {})

    if not to_bool(phase2.get("requires_extraction")):
        context.log.info("Extraction disabled by config blueprint. Skipping processing.")
        return dg.MaterializeResult(metadata={"skipped": True})

    use_proxy: bool = (
        to_bool(phase1.get("use_proxy"))
        or to_bool(extraction_params.get("use_proxy"))
        or to_bool(os.environ.get("USE_PROXY"))
    )

    extras = {
        "requires_extraction": True,
        "document_type": metadata.get("document_type", "pdf"),
        "start_url": phase1.get("start_url", ""),
        "allow_insecure_https": to_bool(phase1.get("allow_insecure_https")),
        "allow_insecure_requests": to_bool(phase1.get("allow_insecure_requests")),
        "use_proxy": use_proxy,
        "extraction_params": json.dumps(extraction_params),  # Serialize to JSON string for the entrypoint
        "expected_schema": phase2.get("expected_schema", ""),
        "llm_engine": phase2.get("engine", "ollama/phi4-mini"),
        "extraction_instructions": phase2.get("extraction_instructions", ""),
        "input_dir": os.path.join(CONTAINER_DATA_DIR, context.partition_key),
        "partition_key": context.partition_key,
    }

    result = pipes_docker.run(
        context=context,
        image=EXTRACTOR_IMAGE,
        env=_build_container_env(),
        extras=extras,
        container_kwargs={
            "network": DOCKER_NETWORK,
            "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
        }
    )
    return result.get_materialize_result()


@dg.asset(
    name="scrubbed_extracted_records",
    partitions_def=pipeline_partitions,
    deps=[extracted_structured_data],
)
def scrubbed_extracted_records(
    context: dg.AssetExecutionContext,
    pipes_docker: PipesDockerClient,
) -> dg.MaterializeResult:
    """Spawns an external container to scrub PII from extracted structured data."""
    blueprint = get_blueprint_for_partition(context.partition_key)
    if not blueprint:
        raise ValueError(f"No active pipeline blueprint found for partition key: {context.partition_key}")

    result = pipes_docker.run(
        context=context,
        image=EXTRACTOR_IMAGE,
        env=_build_container_env(),
        extras={
            "partition_key": context.partition_key,
        },
        container_kwargs={
            "network": DOCKER_NETWORK,
            "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
            "command": ["python", "/app/scrub_entrypoint.py"],
        }
    )
    return result.get_materialize_result()


def cron_field_matches(field_val: str, current_val: int) -> bool:
    if field_val == "*":
        return True
    for part in field_val.split(","):
        if "/" in part:
            val, step = part.split("/")
            step = int(step)
            if val == "*":
                if current_val % step == 0:
                    return True
            else:
                start, end = val.split("-")
                if int(start) <= current_val <= int(end) and (current_val - int(start)) % step == 0:
                    return True
        elif "-" in part:
            start, end = part.split("-")
            if int(start) <= current_val <= int(end):
                return True
        else:
            if int(part) == current_val:
                return True
    return False


def is_cron_active(cron_str: str, timestamp: float) -> bool:
    try:
        import pytz
        from datetime import datetime
        tz = pytz.timezone("Africa/Johannesburg")
    except ImportError:
        tz = None

    dt = datetime.fromtimestamp(timestamp, tz)
    parts = cron_str.split()
    if len(parts) != 5:
        return False
        
    minute, hour, day_m, month, day_w = parts
    current_day_w = (dt.weekday() + 1) % 7
    
    return (
        cron_field_matches(minute, dt.minute)
        and cron_field_matches(hour, dt.hour)
        and cron_field_matches(day_m, dt.day)
        and cron_field_matches(month, dt.month)
        and cron_field_matches(day_w, current_day_w)
    )


@dg.sensor(
    name="coeus_blueprint_sensor",
    minimum_interval_seconds=60,
    target=dg.AssetSelection.all(),
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def coeus_blueprint_sensor(context: dg.SensorEvaluationContext):
    """Fetches blueprints from API, registers partitions, and triggers runs at scheduled cron times."""
    blueprints = fetch_blueprints()
    if not blueprints:
        return

    active_partition_keys = [str(bp["pipeline_id"]) for bp in blueprints if "pipeline_id" in bp]
    dynamic_partitions_requests = [pipeline_partitions.build_add_request(active_partition_keys)]
    
    current_time = time.time()
    current_minute_ts = int(current_time // 60) * 60

    run_requests = []
    for bp in blueprints:
        partition_key = str(bp.get("pipeline_id"))
        if not partition_key:
            continue
            
        cron_str = bp.get("schedule")
        if cron_str and is_cron_active(cron_str, current_minute_ts):
            context.log.info(f"Cron match found for pipeline '{partition_key}': '{cron_str}'. Triggering run request.")
            run_requests.append(dg.RunRequest(
                run_key=f"{partition_key}_{current_minute_ts}",
                partition_key=partition_key,
            ))
        
    return dg.SensorResult(
        run_requests=run_requests, 
        dynamic_partitions_requests=dynamic_partitions_requests
    )

defs = dg.Definitions(
    assets=[
        fetch_github_repo_info,
        raw_scraped_pages,
        extracted_structured_data,
        scrubbed_extracted_records
    ],
    sensors=[
        coeus_blueprint_sensor
    ],
    resources={
        "pipes_docker": PipesDockerClient(),
        "github_api": github_pat_resource,
    },
    **apprise_dict
)
