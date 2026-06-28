import os
import json
import time
import logging
import requests
from typing import Optional

import dagster as dg
from dagster_docker import PipesDockerClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_URL = os.getenv("COEUS_API_URL")
CACHE_FILE = "/app/data/coeus_blueprints_cache.json"
CACHE_TTL = 300  # 5 minutes

SCRAPER_IMAGE = os.getenv(
    "COEUS_SCRAPER_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-scraper:latest",
)
EXTRACTOR_IMAGE = os.getenv(
    "COEUS_EXTRACTOR_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-extractor:latest",
)

HOST_DATA_DIR = os.getenv("COEUS_HOST_DATA_DIR", "/var/shared_scraping_data")
CONTAINER_DATA_DIR = "/app/scraping_data"
DOCKER_NETWORK = os.getenv("COEUS_DOCKER_NETWORK", "8ohm-network")


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
                    "cat_selector": phase1.get("target_css_selector_categories", ""),
                    "doc_selector": phase1.get("target_css_selector_documents", ""),
                    "allow_insecure_https": to_bool(phase1.get("allow_insecure_https")),
                    "allow_insecure_requests": to_bool(phase1.get("allow_insecure_requests")),
                    "use_proxy": use_proxy,
                    "extraction_params": json.dumps(extraction_params),
                }
            },
            "extracted_structured_data": {
                "config": {
                    "requires_extraction": to_bool(phase2.get("requires_extraction")),
                    "document_type": metadata.get("document_type", "pdf"),
                    "start_url": phase1.get("start_url", ""),
                    "cat_selector": phase1.get("target_css_selector_categories", ""),
                    "doc_selector": phase1.get("target_css_selector_documents", ""),
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
        "COEUS_API_URL",
        "USE_PROXY",
        "OPENAI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]
    return {k: os.environ[k] for k in forwarded if k in os.environ}


# ---------------------------------------------------------------------------
# Hybrid approach: DynamicPartitions (who) + Config (how)
# ---------------------------------------------------------------------------

pipeline_partitions = dg.DynamicPartitionsDefinition(name="pipelines")


class ScrapeConfig(dg.Config):
    """Runtime config for the scrape asset."""
    scraper_type: str
    document_type: str
    start_url: str
    cat_selector: str
    doc_selector: str
    allow_insecure_https: bool
    allow_insecure_requests: bool
    use_proxy: bool
    extraction_params: str


class ExtractConfig(dg.Config):
    """Runtime config for the extraction asset."""
    requires_extraction: bool
    document_type: str
    start_url: str
    cat_selector: str
    doc_selector: str
    allow_insecure_https: bool
    allow_insecure_requests: bool
    use_proxy: bool
    extraction_params: str
    expected_schema: str
    llm_engine: str
    extraction_instructions: str


# ---------------------------------------------------------------------------
# Assets (Pipes Docker Execution)
# ---------------------------------------------------------------------------

@dg.asset(
    name="raw_scraped_pages",
    partitions_def=pipeline_partitions,
)
def raw_scraped_pages(
    context: dg.AssetExecutionContext,
    config: ScrapeConfig,
    pipes_docker: PipesDockerClient,
) -> dg.MaterializeResult:
    """Spawns an external container to handle data ingestion via Dagster Pipes."""
    # Build payload payload for container
    extras = config.dict()
    extras["partition_key"] = context.partition_key

    # Run the container
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
    name="extracted_structured_data",
    partitions_def=pipeline_partitions,
    deps=[raw_scraped_pages],
)
def extracted_structured_data(
    context: dg.AssetExecutionContext,
    config: ExtractConfig,
    pipes_docker: PipesDockerClient,
) -> dg.MaterializeResult:
    """Spawns an external container to extract structured data via Dagster Pipes."""
    if not config.requires_extraction:
        context.log.info("Extraction disabled by config blueprint. Skipping processing.")
        return dg.MaterializeResult(metadata={"skipped": True})

    extras = config.dict()
    extras["partition_key"] = context.partition_key

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



# ---------------------------------------------------------------------------
# Automation: Dynamic Sensor
# ---------------------------------------------------------------------------

@dg.sensor(
    name="coeus_blueprint_sensor",
    minimum_interval_seconds=60,
    target=dg.AssetSelection.all(),
)
def coeus_blueprint_sensor(context: dg.SensorEvaluationContext):
    """Fetches blueprints from API, registers partitions, and triggers runs."""
    blueprints = fetch_blueprints()
    if not blueprints:
        return

    # Extract pipeline IDs/names to use as partition keys
    active_partition_keys = [str(bp["id"]) for bp in blueprints if "id" in bp]
    
    # Dynamically register any new keys with the partition definition
    dynamic_partitions_requests = [pipeline_partitions.build_add_request(active_partition_keys)]
    run_requests = []
    for blueprint in blueprints:
        partition_key = str(blueprint.get("id"))
        if not partition_key:
            continue
        run_config = _blueprint_to_run_config(blueprint)
        run_requests.append(dg.RunRequest(
            run_key=f"{partition_key}_{int(time.time() // 60)}",
            partition_key=partition_key,
            run_config=run_config,
        ))
    return dg.SensorResult(run_requests=run_requests, dynamic_partitions_requests=dynamic_partitions_requests)

defs = dg.Definitions(
    assets=[
        raw_scraped_pages,
        extracted_structured_data
    ],
    sensors=[
        coeus_blueprint_sensor
    ],
    resources={
        "pipes_docker": PipesDockerClient(),
    },
)
