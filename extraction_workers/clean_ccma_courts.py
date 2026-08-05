"""Standalone Court Name Normalization Script for Scrubbed Records.

Normalizes variation court names (e.g., 'Bargaining Council', 'CCMA Awards', empty/null)
in coeus.scrubbed_records for 'sabinet_ccma' to 'CCMA'.

Usage:
    python extraction_workers/clean_ccma_courts.py
"""

import os
import sys
import logging
import asyncio

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extraction_workers.db import get_db_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("coeus.clean_ccma_courts")

CLEANUP_QUERY = """
UPDATE scrubbed_records sr
SET data = jsonb_set(sr.data, '{court}', to_jsonb('CCMA'::text))
FROM extracted_records er
WHERE sr.extracted_record_id = er.id
  AND er.record_type = 'sabinet_ccma'
  AND (
    sr.data->>'court' IN ('CCMA Awards', 'Bargaining Council', 'CCMA Bargaining Council Awards')
    OR sr.data->>'court' IS NULL
    OR sr.data->>'court' = ''
    OR sr.data->>'court' ILIKE '%bargaining council%'
    OR sr.data->>'court' ILIKE '%ccma awards%'
  );
"""


async def clean_ccma_court_names() -> int:
    """Updates court names in scrubbed_records data payload to CCMA."""
    conn = await get_db_connection()
    try:
        logger.info("Running CCMA court name normalization cleanup query...")
        status = await conn.execute(CLEANUP_QUERY)
        logger.info(f"Cleanup query result: {status}")
        
        # Extract count from asyncpg status string (e.g., 'UPDATE 117271')
        count = 0
        if status and status.startswith("UPDATE "):
            count = int(status.split(" ")[1])
        return count
    finally:
        await conn.close()


def main():
    updated_count = asyncio.run(clean_ccma_court_names())
    logger.info(f"Successfully normalized {updated_count} CCMA court entries in scrubbed_records.")


if __name__ == "__main__":
    main()
