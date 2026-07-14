"""PII Scrubber container entrypoint.

This script runs inside the coeus-extractor Docker image, spawned by Dagster via
PipesDockerClient. It receives its configuration through the Dagster Pipes context,
queries the database for unscrubbed records, scrubs their 'data' field, and marks
them as cleaned.
"""

import os
import sys
import json
import logging
import asyncio
import asyncpg
from datetime import datetime

from dagster_pipes import PipesContext, open_dagster_pipes

# Ensure we can import extraction_workers
sys.path.append("/app")
from extraction_workers.utils.ppi_scrub import Scrub
from extraction_workers.db import get_db_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.scrubber")


async def run_scrub(pipes: PipesContext) -> None:
    """Main scrubbing logic."""
    context = PipesContext.get()
    partition_key: str = context.get_extra("partition_key")

    logger.info(f"Starting PII scrubbing for partition/pipeline: '{partition_key}'")

    # Connect to PostgreSQL
    conn = None
    try:
        conn = await get_db_connection()
        logger.info("Connected to PostgreSQL database.")
    except Exception as exc:
        logger.error(f"Failed to connect to database: {exc}")
        raise exc

    try:
        # Get total records for this pipeline
        total_records = await conn.fetchval(
            "SELECT COUNT(*) FROM extracted_records WHERE record_type = $1",
            partition_key
        )

        # Get count of already scrubbed records
        already_scrubbed = await conn.fetchval(
            "SELECT COUNT(*) FROM extracted_records WHERE record_type = $1 AND cleaned_at IS NOT NULL",
            partition_key
        )

        # Fetch records that need scrubbing
        records_to_scrub = await conn.fetch(
            "SELECT id, data FROM extracted_records WHERE record_type = $1 AND cleaned_at IS NULL ORDER BY extracted_at ASC",
            partition_key
        )

        logger.info(f"Total records in DB: {total_records}")
        logger.info(f"Already scrubbed: {already_scrubbed}")
        logger.info(f"Records to scrub in this run: {len(records_to_scrub)}")

        scrubbed_count = already_scrubbed
        pii_scrubber = Scrub()

        for idx, row in enumerate(records_to_scrub, start=1):
            record_id = row["id"]
            data_raw = row["data"]

            if isinstance(data_raw, str):
                data = json.loads(data_raw)
            else:
                data = dict(data_raw) if data_raw else {}

            # Scrub the data dictionary recursively
            scrubbed_data = pii_scrubber.scrub_dict(data)

            # Save back to database and set cleaned_at
            await conn.execute(
                """
                UPDATE extracted_records
                SET data = $1, cleaned_at = NOW()
                WHERE id = $2
                """,
                json.dumps(scrubbed_data, ensure_ascii=False),
                record_id
            )

            scrubbed_count += 1
            logger.info(
                f"[{idx}/{len(records_to_scrub)}] Scrubbed record {record_id}. "
                f"Progress: {scrubbed_count}/{total_records} total scrubbed."
            )

        pipes.report_asset_materialization(
            metadata={
                "partition_key": partition_key,
                "total_records": total_records,
                "already_scrubbed_before": already_scrubbed,
                "scrubbed_this_run": len(records_to_scrub),
                "total_scrubbed_after": scrubbed_count,
                "status": "SUCCESS",
            }
        )
        logger.info(f"Scrubbing complete for partition='{partition_key}'")

    finally:
        if conn:
            await conn.close()


if __name__ == "__main__":
    try:
        with open_dagster_pipes() as pipes:
            # Forward python logging to Dagster Pipes
            for handler in pipes.log.handlers:
                logging.getLogger().addHandler(handler)
            asyncio.run(run_scrub(pipes))
    except Exception as exc:
        logger.error(f"Fatal scrubber error: {exc}", exc_info=True)
        sys.exit(1)
