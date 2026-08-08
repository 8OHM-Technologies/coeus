import asyncio
import os
import sys
import json
import argparse
import logging
import uuid
import urllib.parse
import asyncpg
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("consolidate_targets")

# Ensure python path is correct to import from coeus
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def extract_court_code_from_url(url: str) -> str | None:
    """Extract SAFLII court/dataset code from URL path."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        for i, part in enumerate(parts):
            if part == "za" and i + 2 < len(parts):
                category = parts[i + 1]
                if category in ("cases", "gaz", "journals", "other"):
                    return parts[i + 2]
    except Exception:
        pass
    return None

def deduce_base_url_from_url(court_code: str, url: str) -> str:
    """Deduce court base URL from case URL."""
    if not url:
        return f"https://www.saflii.org/za/cases/{court_code}/"
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        for i, part in enumerate(parts):
            if part == "za" and i + 2 < len(parts):
                category = parts[i + 1]
                if category in ("cases", "gaz", "journals", "other"):
                    return f"{parsed.scheme}://{parsed.netloc}/za/{category}/{parts[i + 2]}/"
    except Exception:
        pass
    return f"https://www.saflii.org/za/cases/{court_code}/"

async def run_consolidation(dry_run: bool = True):
    # Load .env
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(dotenv_path=env_path)
    
    host = os.environ.get("POSTGRES_HOST")
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    database = os.environ.get("POSTGRES_DB")
    port = int(os.environ.get("POSTGRES_PORT") or 5432)
    
    if not all([host, user, password, database]):
        logger.error("Missing database environment variables in .env.")
        sys.exit(1)
        
    logger.info(f"Connecting to database {database} at {host}:{port} (Dry Run = {dry_run})...")
    conn = await asyncpg.connect(
        host=host,
        user=user,
        password=password,
        database=database,
        port=port
    )
    
    # Start transaction
    tr = conn.transaction()
    await tr.start()
    
    try:
        # =====================================================================
        # 1. Sabinet & Standalone Targets Fix
        # =====================================================================
        logger.info("\n--- STEP 1: Fixing Sabinet & Standalone Target Names ---")
        targets = await conn.fetch("""
            SELECT t.id, t.target_name, t.location, e.name as entity_name 
            FROM targets t 
            JOIN entities e ON t.entity_id = e.id
            WHERE t.target_name = 'Default Target'
        """)
        
        for target in targets:
            target_id = target["id"]
            location = target["location"] or ""
            entity_name = target["entity_name"]
            new_name = None
            
            if "sabinet.co.za" in location:
                if "ccmabargainingcouncilawards" in location:
                    new_name = "sabinet_ccma"
                elif "sabinetlabourjudgments" in location:
                    new_name = "sabinet_labour"
                elif "sabinetjudgments" in location:
                    new_name = "sabinet_judgments"
            elif entity_name == "Mantech":
                new_name = "mantech"
            elif entity_name == "Livestainable":
                new_name = "livestainable"
                
            if new_name:
                logger.info(f"Renaming target ID {target_id} under Entity '{entity_name}': 'Default Target' -> '{new_name}'")
                await conn.execute("""
                    UPDATE targets 
                    SET target_name = $1 
                    WHERE id = $2
                """, new_name, target_id)
            else:
                logger.warning(f"Could not deduce correct target name for 'Default Target' under Entity '{entity_name}' (Location: {location})")

        # =====================================================================
        # 2. Consolidation of SAFLII Targets under 'Saflii All Courts'
        # =====================================================================
        logger.info("\n--- STEP 2: Consolidating SAFLII Targets under 'Saflii All Courts' ---")
        
        # Resolve 'Saflii All Courts' entity ID
        saflii_entity_id = await conn.fetchval("""
            SELECT id FROM entities WHERE name = 'Saflii All Courts'
        """)
        if not saflii_entity_id:
            # Create entity if missing
            saflii_entity_id = uuid.uuid4()
            logger.info(f"Creating missing Entity 'Saflii All Courts' (ID: {saflii_entity_id})")
            await conn.execute("""
                INSERT INTO entities (id, name, created_at)
                VALUES ($1, 'Saflii All Courts', NOW())
            """, saflii_entity_id)
        else:
            logger.info(f"Found active Entity 'Saflii All Courts' (ID: {saflii_entity_id})")
            
        # Target lookup dictionary for court codes under 'Saflii All Courts'
        saflii_targets_cache = {}
        existing_targets = await conn.fetch("""
            SELECT id, target_name, location FROM targets WHERE entity_id = $1
        """, saflii_entity_id)
        for t in existing_targets:
            saflii_targets_cache[t["target_name"]] = t["id"]
            
        # Helper to get/create target ID for a court code under 'Saflii All Courts'
        async def get_or_create_saflii_target(court_code: str, case_url: str) -> uuid.UUID:
            if court_code not in saflii_targets_cache:
                base_url = deduce_base_url_from_url(court_code, case_url)
                target_id = uuid.uuid4()
                logger.info(f"Creating target '{court_code}' under 'Saflii All Courts' (Location: {base_url})")
                await conn.execute("""
                    INSERT INTO targets (id, entity_id, target_name, location, created_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (entity_id, target_name) DO UPDATE SET target_name = EXCLUDED.target_name
                    RETURNING id
                """, target_id, saflii_entity_id, court_code, base_url)
                
                # Fetch actual ID in case of conflict race
                actual_id = await conn.fetchval("""
                    SELECT id FROM targets WHERE entity_id = $1 AND target_name = $2
                """, saflii_entity_id, court_code)
                saflii_targets_cache[court_code] = actual_id
                
            return saflii_targets_cache[court_code]

        # Fetch all distinct court codes and their representative source_urls in a single query
        logger.info("Discovering distinct court codes and representative URLs...")
        distinct_courts = await conn.fetch("""
            SELECT DISTINCT ON (court_code)
                COALESCE(
                    NULLIF(TRIM(split_part(r.data->>'court', '/', 1)), ''),
                    substring(r.source_url from 'za/(?:cases|gaz|journals|other)/([^/]+)/'),
                    'SAFLII'
                ) AS court_code,
                r.source_url
            FROM extracted_records r
            JOIN targets t ON r.target_id = t.id
            JOIN entities e ON t.entity_id = e.id
            WHERE e.name ILIKE '%saflii%' OR r.record_type ILIKE '%saflii%'
        """)
        
        logger.info(f"Discovered {len(distinct_courts)} distinct court codes from existing records.")

        # Ensure a target exists for each discovered court code
        for row in distinct_courts:
            await get_or_create_saflii_target(row["court_code"], row["source_url"])

        # Perform the single bulk update query
        logger.info("Executing bulk update of extracted_records target mappings...")
        result = await conn.execute("""
            UPDATE extracted_records r
            SET target_id = t.id, record_type = 'saflii_courts'
            FROM targets t
            WHERE t.entity_id = $1
              AND t.target_name = COALESCE(
                  NULLIF(TRIM(split_part(r.data->>'court', '/', 1)), ''),
                  substring(r.source_url from 'za/(?:cases|gaz|journals|other)/([^/]+)/'),
                  'SAFLII'
              )
              AND (
                  r.target_id IN (
                      SELECT t2.id FROM targets t2
                      JOIN entities e2 ON t2.entity_id = e2.id
                      WHERE e2.name ILIKE '%saflii%'
                  )
                  OR r.record_type ILIKE '%saflii%'
              )
        """, saflii_entity_id)

        update_count = int(result.split()[-1]) if result else 0
        logger.info(f"Successfully bulk updated {update_count} extracted_records.")

        # =====================================================================
        # 3. Clean Up Empty targets & Obsolete Entities
        # =====================================================================
        logger.info("\n--- STEP 3: Cleaning Up Empty targets & Obsolete Entities ---")
        
        # Find targets with 0 records
        empty_targets = await conn.fetch("""
            SELECT t.id, t.target_name, e.name as entity_name
            FROM targets t
            LEFT JOIN entities e ON t.entity_id = e.id
            WHERE NOT EXISTS (
                SELECT 1 FROM extracted_records r WHERE r.target_id = t.id
            )
        """)
        
        logger.info(f"Found {len(empty_targets)} empty targets.")
        for target in empty_targets:
            logger.info(f"Deleting empty target: '{target['target_name']}' under Entity '{target['entity_name']}' (ID: {target['id']})")
            await conn.execute("DELETE FROM targets WHERE id = $1", target["id"])
            
        # Find entities with 0 targets
        empty_entities = await conn.fetch("""
            SELECT id, name FROM entities e
            WHERE NOT EXISTS (
                SELECT 1 FROM targets t WHERE t.entity_id = e.id
            )
        """)
        
        logger.info(f"Found {len(empty_entities)} empty entities.")
        for entity in empty_entities:
            logger.info(f"Deleting empty entity: '{entity['name']}' (ID: {entity['id']})")
            await conn.execute("DELETE FROM entities WHERE id = $1", entity["id"])

        if dry_run:
            logger.info("\n[DRY RUN] Rolling back all changes.")
            await tr.rollback()
        else:
            logger.info("\n[EXECUTE] Committing all changes.")
            await tr.commit()
            
    except Exception as e:
        logger.error(f"Error occurred: {e}")
        await tr.rollback()
        raise e
    finally:
        await conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consolidate targets and clean database.")
    parser.add_argument("--execute", action="store_true", help="Apply changes to the database (default is dry-run)")
    args = parser.parse_args()
    
    asyncio.run(run_consolidation(dry_run=not args.execute))
