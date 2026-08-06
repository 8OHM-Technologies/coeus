"""Standalone PII Scrubber CLI Script.

Can be run locally from host environment or inside worker/extractor container:
    python extraction_workers/scrub_standalone.py [record_type]
    python extraction_workers/scrub_standalone.py --record-type saflii_courts --batch-size 100
"""

import os
import sys
import json
import logging
import asyncio
import argparse
import uuid

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extraction_workers.utils.pii_scrub import Scrub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.standalone_scrubber")


async def run_standalone_scrub(
    record_type: str | None = None,
    batch_size: int = 200,
    model_name: str | None = None,
    threshold: float = 0.5,
) -> None:
    """Fetches unscrubbed detailed records in batches and scrubs PII."""
    from extraction_workers.db import get_db_connection

    conn = await get_db_connection()
    try:
        scrubber = Scrub(model_name=model_name, threshold=threshold) if model_name else Scrub(threshold=threshold)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone PII scrubber for extracted records.")
    parser.add_argument("record_type_pos", nargs="?", default=None, help="Record type to scrub (positional)")
    parser.add_argument("--record-type", "-r", dest="record_type_opt", default=None, help="Record type to scrub")
    parser.add_argument("--batch-size", "-b", type=int, default=200, help="Batch size per fetch")
    parser.add_argument("--threshold", "-t", type=float, default=0.5, help="GLiNER prediction confidence threshold")
    parser.add_argument("--model", "-m", dest="model_name", default=None, help="Custom GLiNER model name")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    record_type = args.record_type_opt or args.record_type_pos
    asyncio.run(
        run_standalone_scrub(
            record_type=record_type,
            batch_size=args.batch_size,
            model_name=args.model_name,
            threshold=args.threshold,
        )
    )
