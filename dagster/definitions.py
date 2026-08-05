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

# Container limits (Pipes)
SCRAPER_MEM_LIMIT = os.getenv("COEUS_SCRAPER_MEM_LIMIT", "1g")
SCRAPER_CPU_LIMIT = os.getenv("COEUS_SCRAPER_CPU_LIMIT", "1.0")
EXTRACTOR_MEM_LIMIT = os.getenv("COEUS_EXTRACTOR_MEM_LIMIT", "2g")
EXTRACTOR_CPU_LIMIT = os.getenv("COEUS_EXTRACTOR_CPU_LIMIT", "1.5")


def _parse_cpu_limit(cpu_limit_str: Optional[str]) -> Optional[int]:
    if not cpu_limit_str or cpu_limit_str.lower() in ("0", "none", ""):
        return None
    try:
        return int(float(cpu_limit_str) * 1e9)
    except (ValueError, TypeError):
        logger.warning(f"Invalid CPU limit value: {cpu_limit_str}. CPU limiting disabled.")
        return None


def _parse_mem_limit(mem_limit_str: Optional[str]) -> Optional[str]:
    if not mem_limit_str or mem_limit_str.lower() in ("0", "none", ""):
        return None
    return mem_limit_str



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
        "PROXY_URL",
        "DAGSTER_PIPES_DEBUG",
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

    # Respect pipeline-specific proxy configuration, fallback to environment default if not specified
    use_proxy_val = phase1.get("use_proxy")
    if use_proxy_val is None:
        use_proxy_val = extraction_params.get("use_proxy")
    
    if use_proxy_val is not None:
        use_proxy = to_bool(use_proxy_val)
    else:
        use_proxy = to_bool(os.environ.get("USE_PROXY", "false"))

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

    container_kwargs = {
        "network": DOCKER_NETWORK,
        "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
        "user": "root",
        "auto_remove": False,
        "shm_size": "2g",
    }
    scraper_mem = _parse_mem_limit(SCRAPER_MEM_LIMIT)
    if scraper_mem:
        container_kwargs["mem_limit"] = scraper_mem
    scraper_nano_cpus = _parse_cpu_limit(SCRAPER_CPU_LIMIT)
    if scraper_nano_cpus:
        container_kwargs["nano_cpus"] = scraper_nano_cpus

    result = pipes_docker.run(
        context=context,
        image=SCRAPER_IMAGE,
        env=_build_container_env(),
        extras=extras,
        container_kwargs=container_kwargs,
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

    container_kwargs = {
        "network": DOCKER_NETWORK,
        "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
        "auto_remove": False,
    }
    extractor_mem = _parse_mem_limit(EXTRACTOR_MEM_LIMIT)
    if extractor_mem:
        container_kwargs["mem_limit"] = extractor_mem
    extractor_nano_cpus = _parse_cpu_limit(EXTRACTOR_CPU_LIMIT)
    if extractor_nano_cpus:
        container_kwargs["nano_cpus"] = extractor_nano_cpus

    result = pipes_docker.run(
        context=context,
        image=EXTRACTOR_IMAGE,
        env=_build_container_env(),
        extras=extras,
        container_kwargs=container_kwargs,
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

    phase2 = blueprint.get("phase_2_extraction", {})
    extraction_params = phase2.get("extraction_params", {})
    shared_record_type = extraction_params.get("shared_record_type")

    extras = {
        "partition_key": context.partition_key,
    }
    if shared_record_type:
        extras["shared_record_type"] = shared_record_type

    container_kwargs = {
        "network": DOCKER_NETWORK,
        "volumes": [f"{HOST_DATA_DIR}:{CONTAINER_DATA_DIR}"],
        "command": ["python", "/app/scrub_entrypoint.py"],
        "auto_remove": False,
    }
    extractor_mem = _parse_mem_limit(EXTRACTOR_MEM_LIMIT)
    if extractor_mem:
        container_kwargs["mem_limit"] = extractor_mem
    extractor_nano_cpus = _parse_cpu_limit(EXTRACTOR_CPU_LIMIT)
    if extractor_nano_cpus:
        container_kwargs["nano_cpus"] = extractor_nano_cpus

    result = pipes_docker.run(
        context=context,
        image=EXTRACTOR_IMAGE,
        env=_build_container_env(),
        extras=extras,
        container_kwargs=container_kwargs,
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
    target=dg.AssetSelection.assets("raw_scraped_pages"),
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def coeus_blueprint_sensor(context: dg.SensorEvaluationContext):
    """Fetches blueprints from API, registers partitions, and triggers runs at scheduled cron times."""
    blueprints = fetch_blueprints()
    if not blueprints:
        return

    active_partition_keys = [str(bp["pipeline_id"]) for bp in blueprints if "pipeline_id" in bp]
    dynamic_partitions_requests = [pipeline_partitions.build_add_request(active_partition_keys)]

    # Clean up stale partitions that are no longer in the active blueprints
    try:
        existing_keys = set(context.instance.get_dynamic_partitions(pipeline_partitions.name))
        stale_keys = existing_keys - set(active_partition_keys)
        if stale_keys:
            context.log.info(f"Removing {len(stale_keys)} stale partition keys: {stale_keys}")
            dynamic_partitions_requests.append(
                pipeline_partitions.build_delete_request(list(stale_keys))
            )
    except Exception as e:
        context.log.warning(f"Failed to clean up stale partitions: {e}")

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

downstream_extraction_scrubbing_job = dg.define_asset_job(
    name="downstream_extraction_scrubbing_job",
    selection=dg.AssetSelection.assets("extracted_structured_data", "scrubbed_extracted_records"),
)

sabinet_scrubbing_job = dg.define_asset_job(
    name="sabinet_scrubbing_job",
    selection=dg.AssetSelection.assets("scrubbed_extracted_records"),
)

general_scrubbing_job = dg.define_asset_job(
    name="general_scrubbing_job",
    selection=dg.AssetSelection.assets("scrubbed_extracted_records"),
)


def get_db_conn():
    import psycopg2

    def clean_val(v):
        if not v:
            return v
        v = v.strip()
        for sep in (" #", "\t#"):
            if sep in v:
                v = v.split(sep, 1)[0]
                break
        return v.strip("'\"").strip()

    db_host = clean_val(os.environ.get("POSTGRES_HOST", "localhost"))
    db_user = clean_val(os.environ.get("POSTGRES_USER", "postgres"))
    db_pass = clean_val(os.environ.get("POSTGRES_PASSWORD", "super_secret_password"))
    db_name = clean_val(os.environ.get("POSTGRES_DB", "coeus"))
    db_port = clean_val(os.environ.get("POSTGRES_PORT", "5432"))

    return psycopg2.connect(
        host=db_host,
        user=db_user,
        password=db_pass,
        database=db_name,
        port=int(db_port)
    )


@dg.sensor(
    name="raw_scraped_pages_sensor",
    minimum_interval_seconds=60,
    job=general_scrubbing_job,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def raw_scraped_pages_sensor(context: dg.SensorEvaluationContext):
    try:
        conn = get_db_conn()
    except Exception as e:
        context.log.error(f"Failed to connect to database in raw_scraped_pages_sensor: {e}")
        return

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT record_type FROM extracted_records WHERE status = 'detailed' AND cleaned_at IS NULL"
            )
            rows = cursor.fetchall()
    except Exception as e:
        context.log.error(f"Failed to query database in raw_scraped_pages_sensor: {e}")
        return
    finally:
        conn.close()

    if not rows:
        return

    # Fetch blueprints to match record_type to partition key
    blueprints = fetch_blueprints()
    run_requests = []

    for row in rows:
        record_type = row[0]
        partition_key = None
        for bp in blueprints:
            p_id = str(bp.get("pipeline_id"))
            extraction_params = (bp.get("phase_2_extraction") or {}).get("extraction_params") or {}
            shared_rec_type = extraction_params.get("shared_record_type")
            if p_id == record_type or shared_rec_type == record_type:
                partition_key = p_id
                break

        if not partition_key:
            partition_key = record_type

        context.log.info(f"Found detailed, uncleaned records for record_type '{record_type}'. Triggering scrubbing for partition '{partition_key}'.")
        run_requests.append(
            dg.RunRequest(
                run_key=f"scrub_{record_type}_{int(time.time() / 60)}",
                partition_key=partition_key,
            )
        )

    return dg.SensorResult(run_requests=run_requests)


@dg.sensor(
    name="sabinet_sync_sensor",
    minimum_interval_seconds=60,
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def sabinet_sync_sensor(context: dg.SensorEvaluationContext):
    """Monitors both Sabinet scrapers (oldest & newest first) and triggers scrubbing after both complete successfully."""
    blueprints = fetch_blueprints()
    if not blueprints:
        return

    oldest_pid = None
    newest_pid = None
    for bp in blueprints:
        if bp.get("scraper_type") == "sabinet":
            params = bp.get("extraction_params") or {}
            is_reverse = to_bool(params.get("reverse_direction"))
            pid = str(bp.get("pipeline_id"))
            if is_reverse:
                newest_pid = pid
            else:
                oldest_pid = pid

    if not oldest_pid or not newest_pid:
        context.log.warning("Could not resolve both Sabinet oldest and newest pipeline partition keys from blueprints.")
        return

    instance = context.instance
    
    # Query latest materialization for oldest_pid
    oldest_records = instance.get_event_records(
        dg.EventRecordsFilter(
            event_type=dg.DagsterEventType.ASSET_MATERIALIZATION,
            asset_key=dg.AssetKey("raw_scraped_pages"),
            partition_key=oldest_pid,
        ),
        limit=1,
    )
    
    # Query latest materialization for newest_pid
    newest_records = instance.get_event_records(
        dg.EventRecordsFilter(
            event_type=dg.DagsterEventType.ASSET_MATERIALIZATION,
            asset_key=dg.AssetKey("raw_scraped_pages"),
            partition_key=newest_pid,
        ),
        limit=1,
    )
    
    if not oldest_records or not newest_records:
        context.log.info("One or both Sabinet scrapers have not materialized yet. Skipping sync sensor.")
        return

    oldest_ts = oldest_records[0].event_log_entry.timestamp
    newest_ts = newest_records[0].event_log_entry.timestamp

    # Parse cursor
    cursor_data = {}
    if context.cursor:
        try:
            cursor_data = json.loads(context.cursor)
        except Exception:
            pass

    last_oldest_ts = cursor_data.get("last_oldest_ts", 0.0)
    last_newest_ts = cursor_data.get("last_newest_ts", 0.0)

    # Initialize cursor on first execution to current timestamps to prevent immediate scrub on startup
    if not context.cursor:
        context.log.info(f"Initializing Sabinet sync sensor cursor to oldest={oldest_ts}, newest={newest_ts}")
        return dg.SensorResult(
            cursor=json.dumps({
                "last_oldest_ts": oldest_ts,
                "last_newest_ts": newest_ts,
            })
        )

    if oldest_ts > last_oldest_ts and newest_ts > last_newest_ts:
        context.log.info(
            f"New Sabinet scraper runs detected (oldest materialized at {oldest_ts}, "
            f"newest materialized at {newest_ts}). Triggering scrubbing job."
        )
        new_cursor = json.dumps({
            "last_oldest_ts": oldest_ts,
            "last_newest_ts": newest_ts,
        })
        return dg.SensorResult(
            run_requests=[
                dg.RunRequest(
                    run_key=f"sabinet_scrub_{oldest_ts}_{newest_ts}",
                    partition_key=oldest_pid,
                )
            ],
            cursor=new_cursor,
        )


@dg.asset(
    name="bookstack_imported_pages",
    partitions_def=pipeline_partitions,
    deps=[scrubbed_extracted_records],
)
def bookstack_imported_pages(
    context: dg.AssetExecutionContext,
) -> dg.MaterializeResult:
    """Imports new scrubbed records into BookStack under South African Legal Data shelf."""
    import subprocess
    cmd = ["python", "control_plane/manage.py", "import_to_bookstack", "--batch-size", "200"]
    context.log.info(f"Executing BookStack import: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout:
        context.log.info(res.stdout)
    if res.returncode != 0:
        context.log.error(res.stderr)
        raise RuntimeError(f"BookStack import failed (exit code {res.returncode}): {res.stderr}")
    return dg.MaterializeResult(metadata={"status": "success"})


@dg.asset_sensor(
    name="scrubbed_sensor",
    asset_key=dg.AssetKey("scrubbed_extracted_records"),
    default_status=dg.DefaultSensorStatus.RUNNING,
)
def scrubbed_sensor(
    context: dg.SensorEvaluationContext,
    asset_event: dg.EventLogEntry,
):
    partition_key = asset_event.dagster_event.partition
    context.log.info(
        f"scrubbed_extracted_records materialized for partition '{partition_key}'. "
        f"Triggering BookStack import job."
    )
    return dg.RunRequest(
        run_key=f"bookstack_import_{partition_key}_{asset_event.timestamp}",
        partition_key=partition_key,
    )


bookstack_import_job = dg.define_asset_job(
    name="bookstack_import_job",
    selection=dg.AssetSelection.assets("bookstack_imported_pages"),
)


defs = dg.Definitions(
    assets=[
        fetch_github_repo_info,
        raw_scraped_pages,
        extracted_structured_data,
        scrubbed_extracted_records,
        bookstack_imported_pages
    ],
    jobs=[
        downstream_extraction_scrubbing_job,
        sabinet_scrubbing_job,
        general_scrubbing_job,
        bookstack_import_job
    ],
    sensors=[
        coeus_blueprint_sensor,
        raw_scraped_pages_sensor,
        sabinet_sync_sensor,
        scrubbed_sensor
    ],
    resources={
        "pipes_docker": PipesDockerClient(),
        "github_api": github_pat_resource,
    },
)

if HAS_APPRISE and APPRISE_CONN_STRING:
    try:
        notification_config = AppriseNotificationsConfig(
            urls=[APPRISE_CONN_STRING],
            events=["STEP_FAILURE","RUN_FAILURE"],
            include_jobs=["*"]
        )
        defs = dg.Definitions.merge(defs, apprise_notifications(notification_config))
    except Exception as e:
        logger.warning(f"Failed to configure Apprise notifications: {e}")

