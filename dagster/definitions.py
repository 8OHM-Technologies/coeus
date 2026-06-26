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
CACHE_FILE = "/tmp/coeus_blueprints_cache.json"
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
                    return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read cache file: {e}")

    # Fetch from API
    try:
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        blueprints = response.json().get("pipelines", [])
        
        # Write to cache
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(blueprints, f)
        except Exception as e:
            logger.warning(f"Failed to write cache file: {e}")
            
        logger.info(f"Loaded {len(blueprints)} active pipeline blueprints from API.")
        return blueprints
    except Exception as exc:
        logger.exception("Failed to fetch dynamic pipeline blueprints: %s", exc)
        return []

blueprints = fetch_blueprints()

all_assets = []
all_jobs = []
all_schedules = []

for blueprint in blueprints:
    pipeline_id = blueprint["pipeline_id"]
    scraper_type = blueprint.get("scraper_type", "sedarplus")
    phase1 = blueprint["phase_1_ingestion"]
    phase2 = blueprint["phase_2_extraction"]
    
    document_type = blueprint["metadata"].get("document_type", "pdf")
    worker_script = f"/app/extraction_workers/{scraper_type}_scraper.py"
    
    # We must bind these to local scope variables for the closures
    target_url = phase1.get("start_url", "")
    extraction_params = phase2.get("extraction_params", {})
    expected_schema = phase2.get("expected_schema", "")
    llm_engine = phase2.get("engine", "ollama/phi4-mini")
    extraction_instructions = phase2.get("extraction_instructions", "")
    
    # Shared environment values matching Airflow
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

    # -----------------------------------------------------------------------
    # Scraper Asset
    # -----------------------------------------------------------------------
    # Dagster group names and asset names must be valid identifiers
    clean_pipeline_id = pipeline_id.replace("-", "_")
    scrape_asset_name = f"scrape_{clean_pipeline_id}"
    
    @dg.asset(
        name=scrape_asset_name,
        group_name=clean_pipeline_id,
        op_tags={"scraper_type": scraper_type}
    )
    def scraper_asset(context: dg.AssetExecutionContext, p_id=pipeline_id, s_type=scraper_type, w_script=worker_script, e_params=extraction_params, env=base_env.copy()):
        context.log.info(f"Starting extraction for pipeline {p_id} using {s_type}")
        
        worker_args = ["--pipeline_name", p_id]
        if s_type == "mantech":
            search_keyword = e_params.get("search_keyword")
            category = e_params.get("category") or e_params.get("categories")
            if search_keyword:
                worker_args.extend(["--search_keyword", str(search_keyword)])
            if category:
                category_str = ",".join(str(c) for c in category) if isinstance(category, list) else str(category)
                worker_args.extend(["--category", category_str])
        elif s_type in ("saflii", "new_saflii"):
            worker_args.extend(["--headless", "false"])
            
        if s_type in ("saflii", "new_saflii"):
            # Requires Xvfb virtual display
            cmd_str = " ".join(["python", w_script] + worker_args)
            full_cmd = ["bash", "-c", f"Xvfb :99 -screen 0 1280x720x24 & export DISPLAY=:99 && sleep 1 && {cmd_str}"]
        else:
            full_cmd = ["python", w_script] + worker_args
            
        context.log.info(f"Running command: {' '.join(full_cmd)}")
        
        process = subprocess.Popen(
            full_cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        for line in process.stdout:
            context.log.info(line.rstrip())
            
        process.wait()
        
        if process.returncode != 0:
            raise Exception(f"Scraper failed with return code {process.returncode}")
            
        return dg.MaterializeResult(metadata={"pipeline_id": p_id})

    all_assets.append(scraper_asset)
    pipeline_assets = [scraper_asset]

    # -----------------------------------------------------------------------
    # Extractor Asset
    # -----------------------------------------------------------------------
    if phase2.get("requires_extraction"):
        extract_asset_name = f"extract_{clean_pipeline_id}"
        
        @dg.asset(
            name=extract_asset_name,
            deps=[scraper_asset],
            group_name=clean_pipeline_id,
            op_tags={"engine": llm_engine}
        )
        def extract_asset(context: dg.AssetExecutionContext, p_id=pipeline_id, d_type=document_type, ext_inst=extraction_instructions, l_eng=llm_engine, e_schema=expected_schema, env=base_env.copy()):
            context.log.info(f"Starting LLM extraction for pipeline {p_id}")
            
            # Additional environment for extractor
            env["PIPELINE_NAME"] = p_id
            env["DOCUMENT_TYPE"] = d_type
            env["EXTRACTION_INSTRUCTIONS"] = ext_inst
            env["AI_MODEL"] = l_eng
            
            cmd = [
                "python",
                "/app/extraction_workers/llm_extractor.py",
                "--pipeline_name", p_id,
                "--schema", e_schema
            ]
            
            context.log.info(f"Running command: {' '.join(cmd)}")
            
            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            
            for line in process.stdout:
                context.log.info(line.rstrip())
                
            process.wait()
            
            if process.returncode != 0:
                raise Exception(f"Extractor failed with return code {process.returncode}")
                
            return dg.MaterializeResult(metadata={"pipeline_id": p_id, "schema": e_schema})
            
        all_assets.append(extract_asset)
        pipeline_assets.append(extract_asset)

    # -----------------------------------------------------------------------
    # Job and Schedule
    # -----------------------------------------------------------------------
    job_name = f"pipeline_{clean_pipeline_id}_job"
    pipeline_job = dg.define_asset_job(name=job_name, selection=pipeline_assets)
    all_jobs.append(pipeline_job)
    
    schedule_val = blueprint.get("schedule")
    if schedule_val and schedule_val != "@once":
        # Create a schedule definition
        pipeline_schedule = dg.ScheduleDefinition(
            job=pipeline_job,
            cron_schedule=schedule_val,
        )
        all_schedules.append(pipeline_schedule)

defs = dg.Definitions(
    assets=all_assets,
    jobs=all_jobs,
    schedules=all_schedules
)
