"""Standalone PII Scrubber CLI Script.

Can be run locally from host environment (.venv) or inside control plane container:
    PYTHONPATH=. python extraction_workers/scrub_standalone.py [record_type]
"""

import os
import sys
import json
import logging
import asyncio
import uuid

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extraction_workers.utils.pii_scrub import Scrub
from extraction_workers.db import get_db_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.standalone_scrubber")


async def run_standalone_scrub(record_type: str = None, batch_size: int = 200) -> None:
    """Fetches unscrubbed detailed records in batches and scrubs PII."""
    conn = await get_db_connection()
    try:
        scrubber = Scrub()
        total_scrubbed = 0

        while True:
            if record_type:
                logger.info(f"Fetching batch of up to {batch_size} records for record_type: '{record_type}'...")
                records_to_scrub = await conn.fetch(
                    "SELECT id, data FROM extracted_records WHERE record_type = $1 AND status = 'detailed' AND cleaned_at IS NULL ORDER BY scraped_at ASC LIMIT $2",
                    record_type,
                    batch_size,
                )
            else:
                logger.info(f"Fetching batch of up to {batch_size} uncleaned detailed records across ALL record types...")
                records_to_scrub = await conn.fetch(
                    "SELECT id, data FROM extracted_records WHERE status = 'detailed' AND cleaned_at IS NULL ORDER BY scraped_at ASC LIMIT $1",
                    batch_size,
                )

            if not records_to_scrub:
                logger.info("No more uncleaned records found.")
                break

            logger.info(f"Processing batch of {len(records_to_scrub)} records...")

            for idx, row in enumerate(records_to_scrub, start=1):
                record_id = row["id"]
                data_raw = row["data"]

                if isinstance(data_raw, str):
                    data = json.loads(data_raw)
                else:
                    data = dict(data_raw) if data_raw else {}

                scrubbed_data = scrubber.scrub_dict(data)

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

                await conn.execute(
                    "UPDATE extracted_records SET cleaned_at = NOW() WHERE id = $1",
                    record_id,
                )

                total_scrubbed += 1
                logger.info(f"[{idx}/{len(records_to_scrub)}] (Total: {total_scrubbed}) Scrubbed record {record_id}")

        logger.info(f"Standalone scrubbing operation completed. Total records scrubbed: {total_scrubbed}")
    finally:
        await conn.close()


if __name__ == "__main__":
    rec_type = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_standalone_scrub(rec_type))
