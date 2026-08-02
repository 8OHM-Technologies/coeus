"""Scraper container entrypoint.

This script runs inside the coeus-scraper Docker image, spawned by Dagster via
PipesDockerClient. It receives its configuration through the Dagster Pipes context
(injected via DAGSTER_PIPES_CONTEXT env var) and reports telemetry back to the
Dagster UI via DAGSTER_PIPES_MESSAGES.

Data flow:
  Dagster (pipes_docker.run extras={...})
    → DAGSTER_PIPES_CONTEXT env var
      → open_dagster_pipes() decodes config
        → run the appropriate scraper subprocess
          → pipes.report_asset_materialization(metadata={...})
            → DAGSTER_PIPES_MESSAGES env var
              → Dagster UI shows metadata
"""

import os
import sys
import json
import logging
import subprocess

from dagster_pipes import PipesContext, open_dagster_pipes
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.scraper")


def _run_subprocess(cmd: list[str], extra_env: dict[str, str]) -> None:
    """Run a command, streaming its output to the logger, raising on failure."""
    env = os.environ.copy()
    env.update(extra_env)

    logger.info(f"Executing: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if process.stdout is not None:
        for line in process.stdout:
            logger.info(line.rstrip())

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"Subprocess exited with code {process.returncode}: {' '.join(cmd)}"
        )


def run_scraper(pipes: PipesContext) -> None:
    """Main scraping logic — dispatches to the correct worker script."""

    partition_key: str = pipes.get_extra("partition_key")
    scraper_type: str = pipes.get_extra("scraper_type")
    start_url: str = pipes.get_extra("start_url")
    document_type: str = pipes.get_extra("document_type")
    allow_insecure_https: bool = pipes.get_extra("allow_insecure_https")
    allow_insecure_requests: bool = pipes.get_extra("allow_insecure_requests")
    extraction_params_raw = pipes.get_extra("extraction_params")
    # Handle both JSON string and direct dict input for robustness
    if isinstance(extraction_params_raw, str):
        extraction_params = json.loads(extraction_params_raw) if extraction_params_raw else {}
    elif isinstance(extraction_params_raw, dict):
        extraction_params = extraction_params_raw
    else:
        extraction_params = {}

    def _to_bool(val) -> bool:
        if val is None:
            return False
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "on", "t")
        return False

    use_proxy: bool = (
        _to_bool(extraction_params.get("use_proxy"))
        or _to_bool(pipes.get_extra("use_proxy"))
        or _to_bool(os.getenv("USE_PROXY"))
    )

    output_dir: str = pipes.get_extra("output_dir") or "/app/data"

    # Ensure partition-scoped output directory exists on the shared volume
    partition_dir = os.path.join(output_dir, partition_key)
    os.makedirs(partition_dir, exist_ok=True)

    logger.info(
        f"Starting scrape for partition='{partition_key}' "
        f"scraper='{scraper_type}' url='{start_url}' use_proxy={use_proxy}"
    )

    # Build base environment variables — mirrors what definitions.py previously set
    base_env: dict[str, str] = {
        "PYTHONPATH": "/app:/app/extraction_workers",
        "START_URL": start_url,
        "DOCUMENT_TYPE": document_type,
        "ALLOW_INSECURE_HTTPS": str(allow_insecure_https),
        "ALLOW_INSECURE_REQUESTS": str(allow_insecure_requests),
        "USE_PROXY": str(use_proxy),
        "PROXY_URL": os.getenv("PROXY_URL", ""),
        "EXTRACTION_PARAMS": extraction_params_raw,
        "OUTPUT_DIR": partition_dir,
        "PIPELINE_NAME": pipes.get_extra("pipeline_name") or partition_key,
        "SUBSET": pipes.get_extra("subset") or "",
    }

    worker_module = f"extraction_workers.{scraper_type}_scraper"

    worker_args = ["--pipeline_name", partition_key]

    if scraper_type == "mantech":
        search_keyword = extraction_params.get("search_keyword")
        category = extraction_params.get("category") or extraction_params.get("categories")
        if search_keyword:
            worker_args.extend(["--search_keyword", str(search_keyword)])
        if category:
            category_str = (
                ",".join(str(c) for c in category)
                if isinstance(category, list)
                else str(category)
            )
            worker_args.extend(["--category", category_str])
    elif scraper_type in ("saflii", "new_saflii"):
        worker_args.extend(["--headless", "false"])

    # Xvfb is required for headed Playwright/UC scrapers
    if scraper_type in ("saflii", "new_saflii"):
        cmd_str = " ".join(["python", "-m", worker_module] + worker_args)
        full_cmd = [
            "bash",
            "-c",
            f"Xvfb :99 -screen 0 1280x720x24 > /dev/null 2>&1 & XVFB_PID=$! && export DISPLAY=:99 && sleep 1 && {cmd_str}; STATUS=$?; kill $XVFB_PID 2>/dev/null; exit $STATUS",
        ]
    else:
        full_cmd = ["python", "-m", worker_module] + worker_args

    _run_subprocess(full_cmd, base_env)

    # Count output files produced (if any written by the scraper)
    try:
        files_written = len(os.listdir(partition_dir))
    except Exception:
        files_written = 0

    pipes.report_asset_materialization(
        metadata={
            "partition_key": partition_key,
            "scraper_type": scraper_type,
            "output_directory": partition_dir,
            "files_written": files_written,
            "status": "SUCCESS",
        }
    )
    logger.info(f"Scrape complete for partition='{partition_key}'")


if __name__ == "__main__":
    try:
        with open_dagster_pipes() as pipes:
            # Forward python logging to Dagster Pipes
            for handler in pipes.log.handlers:
                logging.getLogger().addHandler(handler)
            run_scraper(pipes)
    except Exception as exc:
        logger.error(f"Fatal scraper error: {exc}", exc_info=True)
        sys.exit(1)
