import argparse
import asyncio
import logging
import os
import sys

from utils import fetch_pipeline_config
from db import get_db_connection

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

    target_table = config.get("target_table", "extracted_records")
    logger.info(
        f"Scanning for documents in: /app/data/{pipeline_name}/{config.get('document_type', '').lower()}"
    )
    logger.info(f"Target Table: {target_table}")

    # Mocking extraction results
    extracted_data = [
        {"entity_name": "Example Corp", "registration_number": "123456", "status": "Active"},
        {"entity_name": "Test Ltd", "registration_number": "789012", "status": "In Liquidation"},
    ]

    # Database Upsert using asyncpg and Cloud SQL Connector
    try:
        conn = await get_db_connection()
        logger.info(f"Connected to Cloud SQL for upserting to {target_table}")
        
        for record in extracted_data:
            # Example upsert logic (assuming a simple table structure for demonstration)
            query = f"""
                INSERT INTO {target_table} (data, pipeline_id)
                VALUES ($1, $2)
                ON CONFLICT (pipeline_id, (data->>'registration_number')) 
                DO UPDATE SET data = EXCLUDED.data, updated_at = CURRENT_TIMESTAMP;
            """
            await conn.execute(query, str(record), pipeline_name)
            logger.info(f"  [+] Upserted record for: {record['entity_name']}")
            
        await conn.close()
        logger.info("✅ Database upsert complete.")
        
    except Exception as e:
        logger.error(f"❌ Failed to upsert records to database: {e}")

    logger.info("✅ LLM Extraction complete.")


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
