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
import uuid

from dagster_pipes import PipesContext, open_dagster_pipes

# Ensure we can import extraction_workers in container or host
sys.path.append("/app")
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from extraction_workers.utils.pii_scrub import Scrub
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
    shared_record_type: str = context.get_extra("shared_record_type")
    record_type_to_scrub = shared_record_type or partition_key

    logger.info(f"Starting PII scrubbing for partition/pipeline: '{partition_key}' (record type: '{record_type_to_scrub}')")

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
            record_type_to_scrub,
        )

        # Get count of already scrubbed records
        already_scrubbed = await conn.fetchval(
            "SELECT COUNT(*) FROM extracted_records WHERE record_type = $1 AND cleaned_at IS NOT NULL",
            record_type_to_scrub,
        )

        logger.info(f"Total records in DB: {total_records}")
        logger.info(f"Already scrubbed before run: {already_scrubbed}")

        scrubbed_count = already_scrubbed
        scrubbed_this_run = 0
        pii_scrubber = Scrub()
        batch_size = 200

        while True:
            records_to_scrub = await conn.fetch(
                "SELECT id, data FROM extracted_records WHERE record_type = $1 AND status = 'detailed' AND cleaned_at IS NULL ORDER BY scraped_at ASC LIMIT $2",
                record_type_to_scrub,
                batch_size,
            )
            if not records_to_scrub:
                break

            for idx, row in enumerate(records_to_scrub, start=1):
                record_id = row["id"]
                data_raw = row["data"]

                if isinstance(data_raw, str):
                    data = json.loads(data_raw)
                else:
                    data = dict(data_raw) if data_raw else {}

                # Scrub the data dictionary recursively
                scrubbed_data = pii_scrubber.scrub_dict(data)

                # Save to scrubbed_records table (upsert on conflict of extracted_record_id)
                await conn.execute(
                    """
                    INSERT INTO scrubbed_records (id, extracted_record_id, data, created_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (extracted_record_id)
                    DO UPDATE SET data = EXCLUDED.data
                    """,
                    str(uuid.uuid4()),
                    record_id,
                    json.dumps(scrubbed_data, ensure_ascii=False),
                )

                # Update the extracted_records entry's cleaned_at datetime
                await conn.execute(
                    """
                    UPDATE extracted_records
                    SET cleaned_at = NOW()
                    WHERE id = $1
                    """,
                    record_id,
                )

                scrubbed_count += 1
                scrubbed_this_run += 1
                logger.info(
                    f"Scrubbed record {record_id}. "
                    f"Progress: {scrubbed_count}/{total_records} total scrubbed."
                )

        pipes.report_asset_materialization(
            metadata={
                "partition_key": partition_key,
                "total_records": total_records,
                "already_scrubbed_before": already_scrubbed,
                "scrubbed_this_run": scrubbed_this_run,
                "total_scrubbed_after": scrubbed_count,
                "status": "SUCCESS",
            }
        )
        logger.info(f"Scrubbing complete for partition='{partition_key}'. Total scrubbed this run: {scrubbed_this_run}")

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
