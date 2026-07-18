import asyncio
import os
import sys

# Add parent directory to path to import extraction_workers
sys.path.append("/app")

from extraction_workers.db import get_db_connection
from extraction_workers.utils.utils import load_dotenv

async def main():
    load_dotenv()
    print("Environment variables:")
    print("POSTGRES_HOST:", os.environ.get("POSTGRES_HOST"))
    print("POSTGRES_PORT:", os.environ.get("POSTGRES_PORT"))
    print("POSTGRES_DB:", os.environ.get("POSTGRES_DB"))
    print("POSTGRES_USER:", os.environ.get("POSTGRES_USER"))
    
    try:
        conn = await get_db_connection()
        print("Connected successfully!")
        
        # Check pipelines
        pipelines = await conn.fetch("SELECT name, extraction_params, pipeline_state FROM pipelines_pipelineconfiguration")
        print(f"Found {len(pipelines)} pipelines in pipelines_pipelineconfiguration:")
        for p in pipelines:
            print(f"  - {p['name']}:")
            print(f"    extraction_params: {p['extraction_params']}")
            state_str = str(p['pipeline_state'])
            print(f"    pipeline_state: {state_str[:150]}...")
            
        # Check record types and count in extracted_records
        counts = await conn.fetch("SELECT record_type, status, COUNT(*) FROM extracted_records GROUP BY record_type, status")
        print("Record counts in extracted_records:")
        for r in counts:
            print(f"  - {r['record_type']} (status: {r['status']}): {r['count']}")
            
        pending_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM extracted_records
            WHERE record_type = 'sabinet_ccma'
              AND source_url IS NOT NULL
              AND (status = 'indexed' OR (status IS NULL AND (data->>'details_scraped_at') IS NULL))
            """
        )
        print("Pending cases with details needed:", pending_count)
            
        await conn.close()
    except Exception as e:
        print("Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
