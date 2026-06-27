import os
import json
import time
import logging
import requests
import subprocess
from datetime import datetime

import dagster as dg

logger = logging.getLogger(__name__)

API_URL = os.getenv("COEUS_API_URL")
CACHE_FILE = "/app/data/coeus_blueprints_cache.json"
CACHE_TTL = 300  # 5 minutes

def fetch_blueprints():
    if not API_URL:
        logger.warning("Environment variable COEUS_API_URL is not set.")
        return []
        
    # Check cache
    if os.path.exists(CACHE_FILE):
        try:
            mtime = os.path.getmtime(CACHE_FILE)
            if time.time() - mtime < CACHE_TTL:
                with open(CACHE_FILE, "r") as f:
                    cached_data = json.load(f)
                    logger.info(f"Loaded {len(cached_data)} active pipeline blueprints from fresh cache.")
                    return cached_data
        except Exception as e:
            logger.warning(f"Failed to read cache file: {e}")

    # Fetch from API with retry and exponential backoff
    max_retries = 6
    backoff_factor = 2
    initial_delay = 1
    
    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"Attempt {attempt}/{max_retries} to fetch dynamic pipeline blueprints from {API_URL}...")
            response = requests.get(API_URL, timeout=10)
            response.raise_for_status()
            blueprints = response.json().get("pipelines", [])
            
            # Write to cache
            try:
                os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
                with open(CACHE_FILE, "w") as f:
                    json.dump(blueprints, f)
            except Exception as e:
                logger.warning(f"Failed to write cache file: {e}")
                
            logger.info(f"Loaded {len(blueprints)} active pipeline blueprints from API.")
            return blueprints
        except Exception as exc:
            last_exception = exc
            logger.warning(f"Attempt {attempt} failed: {exc}")
            if attempt < max_retries:
                sleep_time = initial_delay * (backoff_factor ** (attempt - 1))
                logger.info(f"Waiting {sleep_time} seconds before retrying...")
                time.sleep(sleep_time)

    logger.error(f"Failed to fetch dynamic pipeline blueprints from API after {max_retries} attempts: {last_exception}")
    
    # Fallback to cache even if expired
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                cached_data = json.load(f)
                logger.warning(f"API failed. Falling back to EXPIRED cache. Loaded {len(cached_data)} blueprints.")
                return cached_data
        except Exception as e:
            logger.error(f"Failed to read fallback cache file: {e}")

    return []

pipeline_partitions = dg.DynamicPartitionsDefinition(name="pipelines")

@dg.asset(
    name="scrape",
    partitions_def=pipeline_partitions,
    group_name="pipelines"
)
def scrape_asset(context: dg.AssetExecutionContext):
    pipeline_id = context.partition_key
    blueprints = fetch_blueprints()
    blueprints_dict = {bp["pipeline_id"]: bp for bp in blueprints}
    blueprint = blueprints_dict.get(pipeline_id)
    if not blueprint:
        raise Exception(f"No blueprint found for pipeline ID: {pipeline_id}")
        
    scraper_type = blueprint.get("scraper_type", "sedarplus")
    phase1 = blueprint["phase_1_ingestion"]
    phase2 = blueprint["phase_2_extraction"]
    
    document_type = blueprint["metadata"].get("document_type", "pdf")
    worker_script = f"/app/extraction_workers/{scraper_type}_scraper.py"
    
    target_url = phase1.get("start_url", "")
    extraction_params = phase2.get("extraction_params", {})
    
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = "/app:/app/extraction_workers"
    base_env["PIPELINE_CONFIG"] = API_URL or ""
    base_env["START_URL"] = target_url
    base_env["DOCUMENT_TYPE"] = document_type
    base_env["CAT_SELECTOR"] = phase1.get("target_css_selector_categories", "")
    base_env["DOC_SELECTOR"] = phase1.get("target_css_selector_documents", "")
    base_env["ALLOW_INSECURE_HTTPS"] = str(phase1.get("allow_insecure_https", False))
    base_env["ALLOW_INSECURE_REQUESTS"] = str(phase1.get("allow_insecure_requests", False))
    
    use_proxy = str(
        phase1.get("use_proxy", False)
        or extraction_params.get("use_proxy", False)
        or os.environ.get("USE_PROXY", "False").lower() == "true"
    )
    base_env["USE_PROXY"] = use_proxy
    base_env["EXTRACTION_PARAMS"] = json.dumps(extraction_params)

    context.log.info(f"Starting extraction for pipeline {pipeline_id} using {scraper_type}")
    
    worker_args = ["--pipeline_name", pipeline_id]
    if scraper_type == "mantech":
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
        # Requires Xvfb virtual display
        cmd_str = " ".join(["python", worker_script] + worker_args)
        full_cmd = ["bash", "-c", f"Xvfb :99 -screen 0 1280x720x24 & export DISPLAY=:99 && sleep 1 && {cmd_str}"]
    else:
        full_cmd = ["python", worker_script] + worker_args
        
    context.log.info(f"Running command: {' '.join(full_cmd)}")
    
    process = subprocess.Popen(
        full_cmd,
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    for line in process.stdout:
        context.log.info(line.rstrip())
        
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Scraper failed with return code {process.returncode}")
        
    return dg.MaterializeResult(metadata={"pipeline_id": pipeline_id})


@dg.asset(
    name="extract",
    partitions_def=pipeline_partitions,
    deps=[scrape_asset],
    group_name="pipelines"
)
def extract_asset(context: dg.AssetExecutionContext):
    pipeline_id = context.partition_key
    blueprints = fetch_blueprints()
    blueprints_dict = {bp["pipeline_id"]: bp for bp in blueprints}
    blueprint = blueprints_dict.get(pipeline_id)
    if not blueprint:
        raise Exception(f"No blueprint found for pipeline ID: {pipeline_id}")
        
    phase1 = blueprint["phase_1_ingestion"]
    phase2 = blueprint["phase_2_extraction"]
    
    if not phase2.get("requires_extraction"):
        context.log.info(f"Pipeline {pipeline_id} does not require extraction. Skipping.")
        return dg.MaterializeResult(metadata={"pipeline_id": pipeline_id, "skipped": True})
        
    document_type = blueprint["metadata"].get("document_type", "pdf")
    target_url = phase1.get("start_url", "")
    extraction_params = phase2.get("extraction_params", {})
    expected_schema = phase2.get("expected_schema", "")
    llm_engine = phase2.get("engine", "ollama/phi4-mini")
    extraction_instructions = phase2.get("extraction_instructions", "")
    
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = "/app:/app/extraction_workers"
    base_env["PIPELINE_CONFIG"] = API_URL or ""
    base_env["START_URL"] = target_url
    base_env["DOCUMENT_TYPE"] = document_type
    base_env["CAT_SELECTOR"] = phase1.get("target_css_selector_categories", "")
    base_env["DOC_SELECTOR"] = phase1.get("target_css_selector_documents", "")
    base_env["ALLOW_INSECURE_HTTPS"] = str(phase1.get("allow_insecure_https", False))
    base_env["ALLOW_INSECURE_REQUESTS"] = str(phase1.get("allow_insecure_requests", False))
    
    use_proxy = str(
        phase1.get("use_proxy", False)
        or extraction_params.get("use_proxy", False)
        or os.environ.get("USE_PROXY", "False").lower() == "true"
    )
    base_env["USE_PROXY"] = use_proxy
    base_env["EXTRACTION_PARAMS"] = json.dumps(extraction_params)
    
    base_env["PIPELINE_NAME"] = pipeline_id
    base_env["DOCUMENT_TYPE"] = document_type
    base_env["EXTRACTION_INSTRUCTIONS"] = extraction_instructions
    base_env["AI_MODEL"] = llm_engine
    
    cmd = [
        "python",
        "/app/extraction_workers/llm_extractor.py",
        "--pipeline_name", pipeline_id,
        "--schema", expected_schema
    ]
    
    context.log.info(f"Running command: {' '.join(cmd)}")
    
    process = subprocess.Popen(
        cmd,
        env=base_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    
    for line in process.stdout:
        context.log.info(line.rstrip())
        
    process.wait()
    
    if process.returncode != 0:
        raise Exception(f"Extractor failed with return code {process.returncode}")
        
    return dg.MaterializeResult(metadata={"pipeline_id": pipeline_id, "schema": expected_schema})


@dg.asset(group_name="system")
def coeus_system_health():
    """A placeholder health-check asset to ensure the Dagster definitions load correctly."""
    return dg.MaterializeResult(metadata={"status": "healthy"})


# Define the single partitioned job
coeus_pipeline_job = dg.define_asset_job(
    name="coeus_pipeline_job",
    selection=[scrape_asset, extract_asset],
    partitions_def=pipeline_partitions
)


# Dynamically load schedules for active pipelines
all_schedules = []
blueprints_at_load = fetch_blueprints()

for blueprint in blueprints_at_load:
    pipeline_id = blueprint["pipeline_id"]
    clean_pipeline_id = pipeline_id.replace("-", "_")
    is_active = blueprint.get("is_active", True)
    schedule_val = blueprint.get("schedule")
    
    if is_active and schedule_val and schedule_val != "@once":
        try:
            # Helper function to capture pipeline_id in closure
            def make_schedule_fn(p_id):
                return lambda context: dg.RunRequest(partition_key=p_id)

            pipeline_schedule = dg.ScheduleDefinition(
                name=f"schedule_{clean_pipeline_id}",
                job=coeus_pipeline_job,
                cron_schedule=schedule_val,
                execution_fn=make_schedule_fn(pipeline_id)
            )
            all_schedules.append(pipeline_schedule)
        except Exception as e:
            logger.error(f"Failed to create schedule for pipeline {pipeline_id} with cron '{schedule_val}': {e}")


# Sensor to automatically synchronize the dynamic partition keys with active blueprints
@dg.sensor(
    name="sync_pipelines_partitions_sensor",
    job=coeus_pipeline_job,
    minimum_interval_seconds=30
)
def sync_pipelines_partitions_sensor(context: dg.SensorEvaluationContext):
    blueprints = fetch_blueprints()
    active_keys = [bp["pipeline_id"] for bp in blueprints]
    
    if active_keys:
        existing_keys = context.instance.get_dynamic_partitions("pipelines")
        new_keys = [k for k in active_keys if k not in existing_keys]
        
        dynamic_partitions_requests = []
        if new_keys:
            context.log.info(f"Adding new dynamic partitions: {new_keys}")
            dynamic_partitions_requests.append(
                pipeline_partitions.build_add_request(new_keys)
            )
            
        for k in existing_keys:
            if k not in active_keys:
                context.log.info(f"Deleting dynamic partition: {k}")
                try:
                    context.instance.delete_dynamic_partition("pipelines", k)
                except Exception as e:
                    context.log.warning(f"Failed to delete partition {k}: {e}")
                    
        return dg.SensorResult(dynamic_partitions_requests=dynamic_partitions_requests)
    return dg.SensorResult()


defs = dg.Definitions(
    assets=[coeus_system_health, scrape_asset, extract_asset],
    jobs=[coeus_pipeline_job],
    schedules=all_schedules,
    sensors=[sync_pipelines_partitions_sensor]
)

