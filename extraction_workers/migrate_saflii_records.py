"""
Coeus SAFLII Record Migration Script
=====================================
Consolidates existing SAFLII records from individual per-pipeline record_type
values into the unified ``saflii_courts`` record type.

Usage:
    python -m extraction_workers.migrate_saflii_records

This script:
1. Identifies all extracted_records with record_type matching old SAFLII pipeline names
2. Updates them to use the unified ``saflii_courts`` record_type
3. Reports how many records were migrated per old pipeline name

Safe to re-run — records already using ``saflii_courts`` are not affected.
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

try:
    from .db import get_db_connection
except ImportError:
    from db import get_db_connection

# Old record_type values used by per-pipeline scrapers
OLD_SAFLII_RECORD_TYPES = [
    # Slugified variants (as actually stored in DB)
    "saflii_labour_court___appeals",
    "saflii_labour_court___db",
    "saflii_labour_court___pe",
    "saflii_labour_court___cct",
    "saflii_labour_court___jhb",
    "saflii_high_court___south_gp",
    "saflii_high_court___north_gp",
    "saflii_high_court___western_cape",
    "saflii_high_court___nw_mafikeng",
    "saflii_high_court___nc_kimberley",
    "saflii_high_court___mp_middelburg",
    "saflii_high_court___mp_mbombela",
    "saflii_high_court___lp_thohoy",
    "saflii_high_court___lp_polokwane",
    "saflii_high_court___kzn_pmb",
    "saflii_high_court___kzn_dbn",
    "saflii_high_court___kzn",
    "saflii_high_court___gauteng",
    "saflii_high_court___ec",
    "saflii_high_court___fs_bloem",
    # Display-name variants (legacy runs)
    "Saflii Labour Court - Appeals",
    "Saflii Labour Court - DB",
    "Saflii Labour Court - PE",
    "Saflii Labour Court - CCT",
    "Saflii Labour Court - JHB",
    "Saflii High Court - South GP",
    "Saflii High Court - North GP",
    "Saflii High Court - Western Cape",
    "Saflii High Court - NW Mafikeng",
    "Saflii High Court - NC Kimberley",
    "Saflii High Court - MP Middelburg",
    "Saflii High Court - MP Mbombela",
    "Saflii High Court - LP Thohoy",
    "Saflii High Court - LP Polokwane",
    "Saflii High Court - KZN PMB",
    "Saflii High Court - KZN DBN",
    "Saflii High Court - KZN",
    "Saflii High Court - Gauteng",
    "Saflii High Court - EC",
    "Saflii High Court - FS Bloem",
    # Test records
    "saflii_test",
]

NEW_RECORD_TYPE = "saflii_courts"


async def migrate():
    conn = await get_db_connection()
    try:
        total_migrated = 0

        for old_type in OLD_SAFLII_RECORD_TYPES:
            result = await conn.execute(
                """
                UPDATE extracted_records
                SET record_type = $1
                WHERE record_type = $2
                """,
                NEW_RECORD_TYPE,
                old_type,
            )
            # asyncpg returns e.g. "UPDATE 42"
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info(f"  Migrated {count:>6} records: '{old_type}' → '{NEW_RECORD_TYPE}'")
                total_migrated += count

        if total_migrated > 0:
            logger.info(f"\n✅ Migration complete. {total_migrated} records consolidated to '{NEW_RECORD_TYPE}'.")
        else:
            logger.info(f"\nNo records found to migrate. All records may already use '{NEW_RECORD_TYPE}'.")

        # Report final state
        row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN status = 'indexed' THEN 1 END) AS indexed,
                   COUNT(CASE WHEN status = 'detailed' THEN 1 END) AS detailed
            FROM extracted_records
            WHERE record_type = $1
            """,
            NEW_RECORD_TYPE,
        )
        if row:
            logger.info(
                f"Current state for '{NEW_RECORD_TYPE}': "
                f"{row['total']} total, {row['indexed']} indexed, {row['detailed']} detailed"
            )

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
    sys.exit(0)
