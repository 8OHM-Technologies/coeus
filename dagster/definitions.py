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

# Container images spawned by PipesDockerClient
SCRAPER_IMAGE = os.getenv(
    "COEUS_SCRAPER_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-scraper:latest",
)
EXTRACTOR_IMAGE = os.getenv(
    "COEUS_EXTRACTOR_IMAGE",
    "ghcr.io/8ohm-technologies/coeus-extractor:latest",
)

# Shared volume: host path → container path
# /var/shared_scraping_data is bind-mounted into every spawned container
# so scrapers can write intermediate files that extractors can read.
HOST_DATA_DIR = os.getenv("COEUS_HOST_DATA_DIR", "/var/shared_scraping_data")
CONTAINER_DATA_DIR = "/app/scraping_data"

# Docker network that all spawned containers must join so they can reach
# postgres, ollama-server, and other services on 8ohm-network.
DOCKER_NETWORK = os.getenv("COEUS_DOCKER_NETWORK", "8ohm-network")


# ---------------------------------------------------------------------------
# Blueprint fetching (used only at sensor/schedule evaluation time, not in assets)
# ---------------------------------------------------------------------------

def fetch_blueprints() -> list[dict]:
    """Fetch active pipeline blueprints from the Coeus API with caching + retry."""
    if not API_URL:
        logger.warning("Environment variable COEUS_API_URL is not set.")
        return []

    # Return fresh cache if available
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

    # Fetch from API with exponential backoff
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

    # Fallback: use stale cache rather than nothing
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cached_data = json.load(f)
                logger.warning(f"Falling back to EXPIRED cache ({len(cached_data)} blueprints).")
                return cached_data
        except Exception as e:
            logger.error(f"Failed to read fallback cache: {e}")

    return []


def _blueprint_to_run_config(blueprint: dict) -> dict:
    """Convert a pipeline blueprint dict into a Dagster run config dict.

    Called at sensor/schedule evaluation time. Bakes all extraction params into
    the run so that assets receive typed config without re-fetching the API.
    Asset names must match the @dg.asset name= parameters below.
    """
    phase1 = blueprint.get("phase_1_ingestion", {})
    phase2 = blueprint.get("phase_2_extraction", {})
    metadata = blueprint.get("metadata", {})
    extraction_params = phase2.get("extraction_params", {})

    use_proxy: bool = (
        phase1.get("use_proxy", False)
        or extraction_params.get("use_proxy", False)
        or os.environ.get("USE_PROXY", "False").lower() == "true"
    )

    return {
        "ops": {
            # Must match name="raw_scraped_pages" in @dg.asset
            "raw_scraped_pages": {
                "config": {
                    "scraper_type": blueprint.get("scraper_type", "sedarplus"),
                    "document_type": metadata.get("document_type", "pdf"),
                    "start_url": phase1.get("start_url", ""),
                    "cat_selector": phase1.get("target_css_selector_categories", ""),
                    "doc_selector": phase1.get("target_css_selector_documents", ""),
                    "allow_insecure_https": phase1.get("allow_insecure_https", False),
                    "allow_insecure_requests": phase1.get("allow_insecure_requests", False),
                    "use_proxy": use_proxy,
                    "extraction_params": json.dumps(extraction_params),
                }
            },
            # Must match name="extracted_structured_data" in @dg.asset
            "extracted_structured_data": {
                "config": {
                    "requires_extraction": phase2.get("requires_extraction", False),
                    "document_type": metadata.get("document_type", "pdf"),
                    "start_url": phase1.get("start_url", ""),
                    "cat_selector": phase1.get("target_css_selector_categories", ""),
                    "doc_selector": phase1.get("target_css_selector_documents", ""),
                    "allow_insecure_https": phase1.get("allow_insecure_https", False),
                    "allow_insecure_requests": phase1.get("allow_insecure_requests", False),
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
    """Forward critical host env vars into spawned containers.

    Scrapers and extractors need DB credentials and API URLs to function.
    These are passed as environment variables — not baked into the image.
    """
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
    """Runtime config for the scrape asset — baked in by the sensor/schedule.

    These fields are forwarded as Dagster Pipes 'extras' into the scraper container,
    so the container entrypoint knows which scraper to run and how to configure it.
    """
    scraper_type: str = "sedarplus"
    document_type: str = "pdf"
    start_url: str = ""
    cat_selector: str = ""
    doc_selector: str = ""
    allow_insecure_https: bool = False
    allow_insecure_requests: bool = False
    use_proxy: bool = False
    extraction_params: str = "{}"  # JSON-serialised dict


class ExtractConfig(dg.Config):
    """Runtime config for the extract asset — baked in by the sensor/schedule.

    These fields are forwarded as Dagster Pipes 'extras' into the extractor container.
    """
    requires_extraction: bool = False
    document_type: str = "pdf"
    start_url: str = ""
    cat_selector: str = ""
    doc_selector: str = ""
    allow_insecure_https: bool = False
    allow_insecure_requests: bool = False
    use_proxy: bool = False
    extraction_params: str = "{}"  # JSON-serialised dict
    expected_schema: str = ""
    llm_engine: str = "ollama/phi4-mini"
    extraction_instructions: str = ""


# ---------------------------------------------------------------------------
# Shared volume mount config for all spawned containers
# ---------------------------------------------------------------------------

_SHARED_VOLUME = {HOST_DATA_DIR: {"bind": CONTAINER_DATA_DIR, "mode": "rw"}}


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

@dg.asset(
    name="raw_scraped_pages",
    partitions_def=pipeline_partitions,
    group_name="pipelines",
)
def scrape_asset(
    context: dg.AssetExecutionContext,
    config: ScrapeConfig,
    pipes_docker: PipesDockerClient,
):
    """Scrape documents for a pipeline partition.

    Spawns a coeus-scraper container via Dagster Pipes. The container receives
    all scraping parameters as Pipes 'extras' and writes output to the shared
    volume at {HOST_DATA_DIR}/{partition_key}/. Metadata (pages, status) is
    reported back through the Pipes channel and surfaces in the Dagster UI.

    Architecture:
      Partition key = *which* pipeline to run.
      Config fields = *how* to run it (baked in by sensor/schedule).
      Container = isolated execution environment with correct Playwright version.
    """
    pipeline_id = context.partition_key

    context.log.info(
        f"Launching scraper container for partition='{pipeline_id}' "
        f"scraper='{config.scraper_type}' image='{SCRAPER_IMAGE}'"
    )

    return pipes_docker.run(
        image=SCRAPER_IMAGE,
        context=context,
        extras={
            "partition_key": pipeline_id,
            "scraper_type": config.scraper_type,
            "document_type": config.document_type,
            "start_url": config.start_url,
            "cat_selector": config.cat_selector,
            "doc_selector": config.doc_selector,
            "allow_insecure_https": config.allow_insecure_https,
            "allow_insecure_requests": config.allow_insecure_requests,
            "use_proxy": config.use_proxy,
            "extraction_params": config.extraction_params,
            "output_dir": CONTAINER_DATA_DIR,
        },
        container_kwargs={
            "network": DOCKER_NETWORK,
            "volumes": _SHARED_VOLUME,
            "environment": _build_container_env(),
        },
    ).get_results()


@dg.asset(
    name="extracted_structured_data",
    partitions_def=pipeline_partitions,
    deps=[scrape_asset],
    group_name="pipelines",
)
def extract_asset(
    context: dg.AssetExecutionContext,
    config: ExtractConfig,
    pipes_docker: PipesDockerClient,
):
    """Extract structured data for a pipeline partition.

    Spawns a coeus-extractor container via Dagster Pipes. The container reads
    the output from the upstream scrape step (via the shared volume or DB),
    calls the configured LLM engine, and reports extraction metrics back through
    the Pipes channel.

    Short-circuits cleanly when requires_extraction=False — the container itself
    handles the skip logic and reports a no-op materialization.
    """
    pipeline_id = context.partition_key

    context.log.info(
        f"Launching extractor container for partition='{pipeline_id}' "
        f"engine='{config.llm_engine}' image='{EXTRACTOR_IMAGE}'"
    )

    return pipes_docker.run(
        image=EXTRACTOR_IMAGE,
        context=context,
        extras={
            "partition_key": pipeline_id,
            "requires_extraction": config.requires_extraction,
            "document_type": config.document_type,
            "start_url": config.start_url,
            "cat_selector": config.cat_selector,
            "doc_selector": config.doc_selector,
            "allow_insecure_https": config.allow_insecure_https,
            "allow_insecure_requests": config.allow_insecure_requests,
            "use_proxy": config.use_proxy,
            "extraction_params": config.extraction_params,
            "expected_schema": config.expected_schema,
            "llm_engine": config.llm_engine,
            "extraction_instructions": config.extraction_instructions,
            "input_dir": CONTAINER_DATA_DIR,
        },
        container_kwargs={
            "network": DOCKER_NETWORK,
            "volumes": _SHARED_VOLUME,
            "environment": _build_container_env(),
        },
    ).get_results()


@dg.asset(group_name="system")
def coeus_system_health():
    """Health-check asset to verify that Dagster definitions load correctly."""
    return dg.MaterializeResult(metadata={"status": "healthy"})


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

coeus_pipeline_job = dg.define_asset_job(
    name="coeus_pipeline_job",
    selection=[scrape_asset, extract_asset],
    partitions_def=pipeline_partitions,
)


# ---------------------------------------------------------------------------
# Schedules — each blueprint becomes a dedicated schedule with config baked in
# ---------------------------------------------------------------------------

def _build_schedules(blueprints: list[dict]) -> list[dg.ScheduleDefinition]:
    """Build one ScheduleDefinition per active blueprint that has a cron schedule."""
    schedules: list[dg.ScheduleDefinition] = []

    for blueprint in blueprints:
        pipeline_id: str = blueprint.get("pipeline_id", "")
        clean_id = pipeline_id.replace("-", "_")
        is_active: bool = blueprint.get("is_active", True)
        schedule_val: Optional[str] = blueprint.get("schedule")

        if not is_active or not schedule_val or schedule_val == "@once":
            continue

        run_config = _blueprint_to_run_config(blueprint)

        def make_execution_fn(p_id: str, cfg: dict):
            def execution_fn(context: dg.ScheduleEvaluationContext):
                return dg.RunRequest(partition_key=p_id, run_config=cfg)
            return execution_fn

        try:
            schedule = dg.ScheduleDefinition(
                name=f"schedule_{clean_id}",
                job=coeus_pipeline_job,
                cron_schedule=schedule_val,
                execution_fn=make_execution_fn(pipeline_id, run_config),
            )
            schedules.append(schedule)
        except Exception as e:
            logger.error(f"Failed to create schedule for '{pipeline_id}' (cron='{schedule_val}'): {e}")

    return schedules


blueprints_at_load = fetch_blueprints()
all_schedules = _build_schedules(blueprints_at_load)


# ---------------------------------------------------------------------------
# Sensor — syncs dynamic partition registry AND emits run requests with config
# ---------------------------------------------------------------------------

@dg.sensor(
    name="sync_pipelines_partitions_sensor",
    job=coeus_pipeline_job,
    minimum_interval_seconds=30,
)
def sync_pipelines_partitions_sensor(context: dg.SensorEvaluationContext):
    """Keep the DynamicPartitionsDefinition in sync with active blueprints.

    For each blueprint:
      - The partition_key (pipeline_id) is the primary segmentation dimension.
      - Full extraction config is baked into RunRequests for newly discovered
        partitions (initial trigger on discovery).
      - Recurring runs for existing partitions are driven by their schedules.

    Hybrid approach:
      partition_key = who (tracked by Dagster for history/backfills)
      run_config    = how (extraction params, baked in at sensor/schedule time)
      extras        = runtime params passed into spawned containers via Pipes
    """
    blueprints = fetch_blueprints()

    if not blueprints:
        context.log.warning("No blueprints returned from API; skipping sync.")
        return dg.SensorResult()

    active_keys = [bp["pipeline_id"] for bp in blueprints]
    existing_keys = set(context.instance.get_dynamic_partitions("pipelines"))

    # --- Partition registry sync ---
    new_keys = [k for k in active_keys if k not in existing_keys]
    dynamic_partitions_requests = []
    if new_keys:
        context.log.info(f"Registering new partitions: {new_keys}")
        dynamic_partitions_requests.append(pipeline_partitions.build_add_request(new_keys))

    for k in list(existing_keys):
        if k not in active_keys:
            context.log.info(f"Removing stale partition: {k}")
            try:
                context.instance.delete_dynamic_partition("pipelines", k)
            except Exception as e:
                context.log.warning(f"Could not remove partition '{k}': {e}")

    # --- Emit run requests only for newly registered partitions ---
    # Existing partitions are handled by their per-blueprint schedules.
    blueprints_by_id = {bp["pipeline_id"]: bp for bp in blueprints}
    run_requests = []
    for pid in new_keys:
        blueprint = blueprints_by_id.get(pid)
        if blueprint:
            run_config = _blueprint_to_run_config(blueprint)
            run_requests.append(
                dg.RunRequest(partition_key=pid, run_config=run_config)
            )

    return dg.SensorResult(
        run_requests=run_requests,
        dynamic_partitions_requests=dynamic_partitions_requests,
    )


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------

defs = dg.Definitions(
    assets=[coeus_system_health, scrape_asset, extract_asset],
    jobs=[coeus_pipeline_job],
    schedules=all_schedules,
    sensors=[sync_pipelines_partitions_sensor],
    resources={
        # PipesDockerClient uses the host Docker socket (mounted via docker-compose)
        # to spawn coeus-scraper and coeus-extractor sibling containers.
        "pipes_docker": PipesDockerClient(),
    },
)
