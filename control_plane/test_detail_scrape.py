import asyncio
import os
import sys
import re

sys.path.append("/app")

from extraction_workers.db import get_db_connection
from extraction_workers.utils.utils import fetch_pipeline_config, load_dotenv
from extraction_workers import db_storage

async def main():
    load_dotenv()
    pipeline_name = "sabinet_ccma___oldest_first"
    
    print("Fetching config...")
    config = await fetch_pipeline_config(pipeline_name)
    print("Config name:", config.get("name"))
    print("Config subset:", config.get("subset"))
    print("Config extraction_params:", config.get("extraction_params"))
    
    extraction_params = config.get("extraction_params") or {}
    reverse_direction = extraction_params.get("reverse_direction", False)
    index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', pipeline_name)
    db_record_type = extraction_params.get("shared_record_type") or index_pipeline_name
    
    print("Resolved values:")
    print("  index_pipeline_name:", index_pipeline_name)
    print("  db_record_type:", db_record_type)
    
    conn = await get_db_connection()
    try:
        total_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM extracted_records
            WHERE record_type = $1 AND source_url IS NOT NULL
            """,
            db_record_type
        )
        completed_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM extracted_records
            WHERE record_type = $1
              AND source_url IS NOT NULL
              AND (status = 'detailed' OR (status IS NULL AND (data->>'details_scraped_at') IS NOT NULL))
            """,
            db_record_type
        )
        
        cases = await db_storage.load_records_needing_detail(
            conn, db_record_type, sort_desc=reverse_direction
        )
        
        print("Database counts:")
        print("  total_count:", total_count)
        print("  completed_count:", completed_count)
        print("  cases needing detail:", len(cases))
        
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
