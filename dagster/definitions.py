import os
import json
import time
import logging
import requests
import subprocess
from typing import Optional

import dagster as dg

logger = logging.getLogger(__name__)

API_URL = os.getenv("COEUS_API_URL")
CACHE_FILE = "/app/data/coeus_blueprints_cache.json"
CACHE_TTL = 300  # 5 minutes

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

    This is the bridge between the external API shape and the Config classes below.
    All extraction params are baked in here at sensor/schedule evaluation time so
    that assets never need to call fetch_blueprints() at run time.
    """
    phase1 = blueprint.get("phase_1_ingestion", {})
    phase2 = blueprint.get("phase_2_extraction", {})
    metadata = blueprint.get("metadata", {})
    extraction_params = phase2.get("extraction_params", {})

    use_proxy = (
        phase1.get("use_proxy", False)
        or extraction_params.get("use_proxy", False)
        or os.environ.get("USE_PROXY", "False").lower() == "true"
    )

    return {
        "ops": {
            "scrape": {
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
            "extract": {
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


# ---------------------------------------------------------------------------
# Hybrid approach: DynamicPartitions (who) + Config (how)
# ---------------------------------------------------------------------------

pipeline_partitions = dg.DynamicPartitionsDefinition(name="pipelines")


class ScrapeConfig(dg.Config):
    """Runtime config for the scrape asset — baked in by the sensor/schedule."""
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
    """Runtime config for the extract asset — baked in by the sensor/schedule."""
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
# Assets
# ---------------------------------------------------------------------------

@dg.asset(
    name="scrape",
    partitions_def=pipeline_partitions,
    group_name="pipelines",
)
def scrape_asset(context: dg.AssetExecutionContext, config: ScrapeConfig):
    """Scrape documents for a pipeline partition.

    The partition key identifies *which* pipeline to run.
    All extraction parameters are supplied via config (baked in by the sensor/schedule),
    so this asset never needs to call the external API at run time.
    """
    pipeline_id = context.partition_key
    scraper_type = config.scraper_type

    worker_script = f"/app/extraction_workers/{scraper_type}_scraper.py"

    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = "/app:/app/extraction_workers"
    base_env["START_URL"] = config.start_url
    base_env["DOCUMENT_TYPE"] = config.document_type
    base_env["CAT_SELECTOR"] = config.cat_selector
    base_env["DOC_SELECTOR"] = config.doc_selector
    base_env["ALLOW_INSECURE_HTTPS"] = str(config.allow_insecure_https)
    base_env["ALLOW_INSECURE_REQUESTS"] = str(config.allow_insecure_requests)
    base_env["USE_PROXY"] = str(config.use_proxy)
    base_env["EXTRACTION_PARAMS"] = config.extraction_params

    worker_args = ["--pipeline_name", pipeline_id]
    if scraper_type == "mantech":
        extraction_params = json.loads(config.extraction_params)
        search_keyword = extraction_params.get("search_keyword")
        category = extraction_params.get("category") or extraction_params.get("categories")
        if search_keyword:
            worker_args.extend(["--search_keyword", str(search_keyword)])
        if category:
            category_str = ",".join(str(c) for c in category) if isinstance(category, list) else str(category)
            worker_args.extend(["--category", category_str])
    elif scraper_type in ("saflii", "new_saflii"):
        worker_args.extend(["--headless", "false"])

    if scraper_type in ("saflii", "new_saflii"):
        cmd_str = " ".join(["python", worker_script] + worker_args)
        full_cmd = ["bash", "-c", f"Xvfb :99 -screen 0 1280x720x24 & export DISPLAY=:99 && sleep 1 && {cmd_str}"]
    else:
        full_cmd = ["python", worker_script] + worker_args

    context.log.info(f"Starting scrape for pipeline '{pipeline_id}' using '{scraper_type}'")
    context.log.info(f"Command: {' '.join(full_cmd)}")

    process = subprocess.Popen(
        full_cmd,
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for line in process.stdout:
        context.log.info(line.rstrip())

    process.wait()

    if process.returncode != 0:
        raise Exception(f"Scraper failed with return code {process.returncode}")

    return dg.MaterializeResult(
        metadata={"pipeline_id": pipeline_id, "scraper_type": scraper_type}
    )


@dg.asset(
    name="extract",
    partitions_def=pipeline_partitions,
    deps=[scrape_asset],
    group_name="pipelines",
)
def extract_asset(context: dg.AssetExecutionContext, config: ExtractConfig):
    """Extract structured data for a pipeline partition.

    The partition key identifies *which* pipeline to run.
    All extraction parameters are supplied via config (baked in by the sensor/schedule),
    so this asset never needs to call the external API at run time.
    """
    pipeline_id = context.partition_key

    if not config.requires_extraction:
        context.log.info(f"Pipeline '{pipeline_id}' does not require extraction. Skipping.")
        return dg.MaterializeResult(metadata={"pipeline_id": pipeline_id, "skipped": True})

    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = "/app:/app/extraction_workers"
    base_env["PIPELINE_NAME"] = pipeline_id
    base_env["START_URL"] = config.start_url
    base_env["DOCUMENT_TYPE"] = config.document_type
    base_env["CAT_SELECTOR"] = config.cat_selector
    base_env["DOC_SELECTOR"] = config.doc_selector
    base_env["ALLOW_INSECURE_HTTPS"] = str(config.allow_insecure_https)
    base_env["ALLOW_INSECURE_REQUESTS"] = str(config.allow_insecure_requests)
    base_env["USE_PROXY"] = str(config.use_proxy)
    base_env["EXTRACTION_PARAMS"] = config.extraction_params
    base_env["EXTRACTION_INSTRUCTIONS"] = config.extraction_instructions
    base_env["AI_MODEL"] = config.llm_engine

    cmd = [
        "python",
        "/app/extraction_workers/llm_extractor.py",
        "--pipeline_name", pipeline_id,
        "--schema", config.expected_schema,
    ]

    context.log.info(f"Starting extraction for pipeline '{pipeline_id}'")
    context.log.info(f"Command: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    for line in process.stdout:
        context.log.info(line.rstrip())

    process.wait()

    if process.returncode != 0:
        raise Exception(f"Extractor failed with return code {process.returncode}")

    return dg.MaterializeResult(
        metadata={"pipeline_id": pipeline_id, "schema": config.expected_schema}
    )


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
      - The full extraction config is baked into the RunRequest so assets never
        need to call the external API at run time (hybrid approach).
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
    # (Existing partitions are handled by their schedules.)
    run_requests = []
    for blueprint in blueprints:
        pid = blueprint["pipeline_id"]
        if pid in new_keys:
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
)
