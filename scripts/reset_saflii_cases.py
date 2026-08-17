#!/usr/bin/env python3
"""
Reset SAFLII Cases Script
=========================
Resets extracted SAFLII case records to prepare them for re-parsing and re-extraction:
1. Identifies matching records in `extracted_records` (filtering by category = 'cases').
2. Removes corresponding rows from `scrubbed_records`.
3. Removes corresponding rows from `parsed_records`.
4. Resets `extracted_records`:
   - `status = 'detailed'`
   - `parsed_at = NULL`
   - `scrubbed_at = NULL`
   - `requires_human_review = FALSE`
   - `review_reason = NULL`
   - `updated_at = NOW()`

Usage:
    # Dry run (inspect what would be reset without modifying DB):
    python scripts/reset_saflii_cases.py --dry-run

    # Reset all case records (interactive prompt):
    python scripts/reset_saflii_cases.py

    # Reset without confirmation:
    python scripts/reset_saflii_cases.py --force

    # Target specific target (e.g. ZACC or ZACAC):
    python scripts/reset_saflii_cases.py --target ZACC --force
"""

import argparse
import asyncio
import os
import sys
from collections import Counter

# Ensure repo root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Auto-activate .venv if running from system python
venv_python = os.path.join(REPO_ROOT, ".venv", "bin", "python")
if os.path.exists(venv_python) and sys.executable != venv_python:
    try:
        import dotenv
        import asyncpg
    except ImportError:
        os.execv(venv_python, [venv_python] + sys.argv)

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

from extraction_workers.db import get_db_connection


async def reset_cases(
    pipeline: str | None = None,
    category: str = "cases",
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
):
    print("=" * 65)
    print("⚖️  SAFLII CASES RESET UTILITY")
    print("=" * 65)
    print(f"Pipeline     : {pipeline or 'All Pipelines'}")
    print(f"Category     : {category}")
    print(f"Target Filter: {target_name or 'All Targets'}")
    print(f"Mode         : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print("-" * 65)

    try:
        conn = await get_db_connection()
    except Exception as exc:
        print(f"❌ Database connection failed: {exc}")
        sys.exit(1)

    try:
        # Build query matching cases in scrubbed_records or parsed_records
        params = [category]
        target_clause = ""
        if target_name:
            params.append(target_name)
            target_clause = f"AND t.target_name = ${len(params)}"

        query = f"""
            SELECT e.id, e.status, COALESCE(t.target_name, 'Unknown') AS target_name,
                   e.requires_human_review,
                   (p.id IS NOT NULL) AS has_parsed,
                   (s.id IS NOT NULL) AS has_scrubbed
            FROM scrubbed_records s
            JOIN extracted_records e ON s.extracted_record_id = e.id
            LEFT JOIN targets t ON e.target_id = t.id
            LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
            WHERE e.data->>'category' = $1
            {target_clause}
            ORDER BY t.target_name ASC, e.scraped_at ASC;
        """

        records = await conn.fetch(query, *params)
        total_records = len(records)

        if total_records == 0:
            # Check if there are unscrubbed parsed cases
            query_parsed = f"""
                SELECT e.id, e.status, COALESCE(t.target_name, 'Unknown') AS target_name,
                       e.requires_human_review,
                       (p.id IS NOT NULL) AS has_parsed,
                       FALSE AS has_scrubbed
                FROM parsed_records p
                JOIN extracted_records e ON p.extracted_record_id = e.id
                LEFT JOIN targets t ON e.target_id = t.id
                WHERE e.data->>'category' = $1
                {target_clause}
                ORDER BY t.target_name ASC, e.scraped_at ASC;
            """
            records = await conn.fetch(query_parsed, *params)
            total_records = len(records)

        if total_records == 0:
            print("⚠️  No matching case records found.")
            return

        target_counts = Counter([r["target_name"] for r in records])
        scrubbed_count = sum(1 for r in records if r["has_scrubbed"])
        parsed_count = sum(1 for r in records if r["has_parsed"])
        review_count = sum(1 for r in records if r["requires_human_review"])
        record_ids = [r["id"] for r in records]

        print(f"Found {total_records} case record(s) matching criteria:")
        for tname, count in sorted(target_counts.items()):
            print(f"  • {tname:<15}: {count:>4} record(s)")
        print("-" * 65)
        print(f"  • Parsed Records to delete   : {parsed_count}")
        print(f"  • Scrubbed Records to delete : {scrubbed_count}")
        print(f"  • Records currently flagged  : {review_count}")
        print(f"  • Extracted Records to reset : {total_records}")
        print("=" * 65)

        if dry_run:
            print("🔍 DRY RUN COMPLETE — No database modifications were made.")
            return

        if not force:
            confirm = input("\nAre you sure you want to delete parsed/scrubbed data and reset these records? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("❌ Operation cancelled by user.")
                return

        print("\n⏳ Executing reset in transaction...")
        async with conn.transaction():
            deleted_scrubbed = await conn.execute(
                "DELETE FROM scrubbed_records WHERE extracted_record_id = ANY($1)",
                record_ids,
            )
            deleted_parsed = await conn.execute(
                "DELETE FROM parsed_records WHERE extracted_record_id = ANY($1)",
                record_ids,
            )
            updated_extracted = await conn.execute(
                """
                UPDATE extracted_records
                SET status = 'detailed',
                    parsed_at = NULL,
                    scrubbed_at = NULL,
                    requires_human_review = FALSE,
                    review_reason = NULL,
                    updated_at = NOW()
                WHERE id = ANY($1)
                """,
                record_ids,
            )

        print("=" * 65)
        print("✅ RESET COMPLETE")
        print(f"  • {deleted_scrubbed} from scrubbed_records")
        print(f"  • {deleted_parsed} from parsed_records")
        print(f"  • {updated_extracted} in extracted_records (status='detailed', parsed_at=NULL, scrubbed_at=NULL)")
        print("=" * 65)
        print("💡 You can now trigger the parser and LLM extractor pipeline:")
        print("   python extraction_workers/llm_extractor.py --pipeline saflii")
        print("=" * 65)

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Reset SAFLII case records to re-run parsing and LLM extraction.")
    parser.add_argument("--pipeline", "-p", default=None, help="Pipeline name / record_type filter (optional)")
    parser.add_argument("--category", "-c", default="cases", help="Record category filter (default: cases)")
    parser.add_argument("--target", "-t", default=None, help="Target name filter (e.g. ZACC, ZACAC)")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview records to reset without making changes")
    parser.add_argument("--force", "-y", action="store_true", help="Execute without interactive confirmation prompt")

    args = parser.parse_args()
    asyncio.run(reset_cases(
        pipeline=args.pipeline,
        category=args.category,
        target_name=args.target,
        dry_run=args.dry_run,
        force=args.force,
    ))


if __name__ == "__main__":
    main()
