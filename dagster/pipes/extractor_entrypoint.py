"""LLM Extractor container entrypoint.

This script runs inside the coeus-extractor Docker image, spawned by Dagster via
PipesDockerClient. It receives its configuration through the Dagster Pipes context
and reports telemetry back via dagster_pipes.

Data flow:
  Dagster (pipes_docker.run extras={...})
    → DAGSTER_PIPES_CONTEXT env var
      → open_dagster_pipes() decodes config
        → run llm_extractor.py subprocess
          → pipes.report_asset_materialization(metadata={...})
"""

import os
import sys
import json
import logging
import subprocess

from dagster_pipes import PipesContext, open_dagster_pipes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.extractor")


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

    for line in process.stdout:
        logger.info(line.rstrip())

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"Subprocess exited with code {process.returncode}: {' '.join(cmd)}"
        )


def run_extractor(pipes: PipesContext) -> None:
    """Main extraction logic — calls the LLM extractor script."""
    context = PipesContext.get()

    partition_key: str = context.get_extra("partition_key")
    requires_extraction: bool = context.get_extra("requires_extraction")
    document_type: str = context.get_extra("document_type")
    start_url: str = context.get_extra("start_url")
    cat_selector: str = context.get_extra("cat_selector")
    doc_selector: str = context.get_extra("doc_selector")
    allow_insecure_https: bool = context.get_extra("allow_insecure_https")
    allow_insecure_requests: bool = context.get_extra("allow_insecure_requests")
    use_proxy: bool = context.get_extra("use_proxy")
    extraction_params_raw: str = context.get_extra("extraction_params")
    expected_schema: str = context.get_extra("expected_schema")
    llm_engine: str = context.get_extra("llm_engine")
    extraction_instructions: str = context.get_extra("extraction_instructions")
    input_dir: str = context.get_extra("input_dir")

    if not requires_extraction:
        logger.info(f"Pipeline '{partition_key}' does not require extraction. Skipping.")
        pipes.report_asset_materialization(
            metadata={
                "partition_key": partition_key,
                "skipped": True,
                "reason": "requires_extraction=False",
            }
        )
        return

    logger.info(f"Starting extraction for partition='{partition_key}' engine='{llm_engine}'")

    base_env: dict[str, str] = {
        "PYTHONPATH": "/app:/app/extraction_workers",
        "PIPELINE_NAME": partition_key,
        "START_URL": start_url,
        "DOCUMENT_TYPE": document_type,
        "CAT_SELECTOR": cat_selector,
        "DOC_SELECTOR": doc_selector,
        "ALLOW_INSECURE_HTTPS": str(allow_insecure_https),
        "ALLOW_INSECURE_REQUESTS": str(allow_insecure_requests),
        "USE_PROXY": str(use_proxy),
        "EXTRACTION_PARAMS": extraction_params_raw,
        "EXTRACTION_INSTRUCTIONS": extraction_instructions,
        "AI_MODEL": llm_engine,
        "INPUT_DIR": input_dir,
    }

    cmd = [
        "python",
        "/app/extraction_workers/llm_extractor.py",
        "--pipeline_name", partition_key,
        "--schema", expected_schema,
    ]

    _run_subprocess(cmd, base_env)

    pipes.report_asset_materialization(
        metadata={
            "partition_key": partition_key,
            "llm_engine": llm_engine,
            "expected_schema": expected_schema,
            "status": "SUCCESS",
        }
    )
    logger.info(f"Extraction complete for partition='{partition_key}'")


if __name__ == "__main__":
    try:
        with open_dagster_pipes() as pipes:
            run_extractor(pipes)
    except Exception as exc:
        logger.error(f"Fatal extractor error: {exc}", exc_info=True)
        sys.exit(1)
