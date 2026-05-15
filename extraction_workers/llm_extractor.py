import argparse
import asyncio
import logging
import os
import sys

from utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(pipeline_name: str, schema_name: str):
    # Fetch configuration from API or Env
    config = await fetch_pipeline_config(pipeline_name)

    logger.info("==================================================")
    logger.info(f"🚀 COEUS LLM EXTRACTOR INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Schema: {schema_name}")
    logger.info(f"Engine: {config.get('llm_engine', 'gemini-cli')}")
    logger.info("==================================================")

    # In a real implementation, this would:
    # 1. Load the Pydantic schema from schemas.py
    # 2. Iterate through files in /app/data/{pipeline_name}/{document_type}/
    # 3. Call the LLM with the extraction instructions
    # 4. Upsert results to the target table

    logger.info(
        f"Scanning for documents in: /app/data/{pipeline_name}/{config.get('document_type', '').lower()}"
    )
    logger.info(f"Target Table: {config.get('target_table', 'extracted_records')}")

    # Mocking completion
    await asyncio.sleep(1)
    logger.info("✅ LLM Extraction simulation complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus LLM Extractor")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    parser.add_argument(
        "--schema",
        required=True,
        help="The Pydantic schema class name to use for extraction",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name, args.schema))
