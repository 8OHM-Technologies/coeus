#!/usr/bin/env python3
"""
Fix SAFLII Case Document Dates Script
=====================================
Resolves and fixes `document_date` across existing SAFLII case records (excluding
gazettes, journals, and other non-case datasets) by extracting the true judgment date
from document titles (e.g., matching the trailing brackets `(13 April 2004)`):

1. Scans SAFLII case records in `scrubbed_records` joined with `extracted_records` and `targets`.
2. Extracts document titles from `extracted_data->title` or root `title`.
3. Resolves document dates using `extract_date_from_title` from `new_saflii_scraper`.
4. Updates `scrubbed_records.data->'metadata'->>'document_date'` where mismatched or missing.
5. Optionally synchronizes `extracted_records.document_date` with the resolved date.
6. Executes atomic batch updates in transactions with progress reporting.

Usage:
    # Dry run (inspect what would be changed without modifying DB):
    python scripts/fix_saflii_document_dates.py --dry-run

    # Fix all records (interactive prompt):
    python scripts/fix_saflii_document_dates.py

    # Fix without confirmation:
    python scripts/fix_saflii_document_dates.py --force

    # Filter by target name (e.g. ZACC, ZACT, ZASCA):
    python scripts/fix_saflii_document_dates.py --target ZACC --force

    # Only fix scrubbed_records metadata, skipping extracted_records table:
    python scripts/fix_saflii_document_dates.py --skip-extracted-records --force
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
from extraction_workers.new_saflii_scraper import extract_date_from_title


async def fix_document_dates(
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    update_extracted_records: bool = True,
):
    print("=" * 70)
    print("📅 SAFLII SCRUBBED & EXTRACTED DOCUMENT DATE NORMALIZER")
    print("=" * 70)
    print(f"Target Filter            : {target_name or 'All Targets'}")
    print(f"Mode                     : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print(f"Update Extracted Records : {'Yes' if update_extracted_records else 'No (Scrubbed metadata only)'}")
    print("-" * 70)

    try:
        conn = await get_db_connection()
    except Exception as exc:
        print(f"❌ Database connection failed: {exc}")
        sys.exit(1)

    try:
        params = []
        target_clause = ""
        if target_name:
            params.append(target_name)
            target_clause = f"AND t.target_name = ${len(params)}"

        query = f"""
            SELECT s.id AS scrubbed_id,
                   s.extracted_record_id,
                   s.data ? 'metadata' AS has_metadata,
                   s.data ? 'extracted_data' AS has_extracted_data,
                   s.data->'metadata'->>'document_date' AS current_meta_date,
                   s.data->'extracted_data'->>'judgment_date' AS current_judgment_date,
                   s.data->'extracted_data'->>'hearing_date' AS current_hearing_date,
                   COALESCE(
                       e.data->>'title',
                       s.data->>'title',
                       s.data->'extracted_data'->>'title',
                       e.data->'extracted_data'->>'title'
                   ) AS title,
                   e.document_date AS current_ext_date,
                   e.source_url,
                   COALESCE(t.target_name, 'Unknown') AS target_name
            FROM scrubbed_records s
            JOIN extracted_records e ON s.extracted_record_id = e.id
            LEFT JOIN targets t ON e.target_id = t.id
            WHERE (
                s.data ? 'extracted_data'
                OR e.data->>'category' = 'cases'
                OR e.source_url LIKE '%/za/cases/%'
            )
            AND e.source_url NOT LIKE '%/za/gaz/%'
            AND e.source_url NOT LIKE '%/za/journals/%'
            AND e.source_url NOT LIKE '%/za/other/%'
            {target_clause}
            ORDER BY t.target_name ASC, s.created_at ASC;
        """

        print("⏳ Fetching records from database...")
        records = await conn.fetch(query, *params)
        total_scanned = len(records)

        if total_scanned == 0:
            print("⚠️  No matching records found.")
            return

        to_update = []
        target_counts = Counter()
        meta_update_count = 0
        ext_update_count = 0
        no_date_count = 0

        for rec in records:
            title = rec["title"]
            resolved_date = extract_date_from_title(title)
            if not resolved_date:
                no_date_count += 1
                continue

            resolved_iso = resolved_date.isoformat()
            current_meta_date = rec["current_meta_date"]
            current_judgment_date = rec["current_judgment_date"]
            current_hearing_date = rec["current_hearing_date"]
            current_ext_date = rec["current_ext_date"]
            has_metadata = rec["has_metadata"]
            has_extracted_data = rec["has_extracted_data"]

            meta_needs_fix = has_metadata and (current_meta_date != resolved_iso)
            judgment_needs_fix = has_extracted_data and (current_judgment_date != resolved_iso)
            hearing_needs_fix = has_extracted_data and (current_hearing_date == "2026-08-22" or current_hearing_date == current_judgment_date)
            ext_needs_fix = update_extracted_records and (current_ext_date != resolved_date)

            scrub_needs_fix = meta_needs_fix or judgment_needs_fix or (has_extracted_data and current_hearing_date == "2026-08-22")

            if scrub_needs_fix or ext_needs_fix:
                target_counts[rec["target_name"]] += 1
                if scrub_needs_fix:
                    meta_update_count += 1
                if ext_needs_fix:
                    ext_update_count += 1

                to_update.append({
                    "scrubbed_id": rec["scrubbed_id"],
                    "extracted_record_id": rec["extracted_record_id"],
                    "target_name": rec["target_name"],
                    "source_url": rec["source_url"],
                    "title": title,
                    "resolved_date": resolved_date,
                    "resolved_iso": resolved_iso,
                    "scrub_needs_fix": scrub_needs_fix,
                    "meta_needs_fix": meta_needs_fix,
                    "judgment_needs_fix": judgment_needs_fix,
                    "hearing_needs_fix": hearing_needs_fix,
                    "old_meta_date": current_meta_date,
                    "old_judgment_date": current_judgment_date,
                    "ext_needs_fix": ext_needs_fix,
                    "old_ext_date": current_ext_date,
                })

        print(f"Scanned {total_scanned} record(s). Found {len(to_update)} requiring date updates:")
        for tname, count in sorted(target_counts.items()):
            print(f"  • {tname:<15}: {count:>4} record(s)")
        print(f"Total scrubbed record fixes   : {meta_update_count}")
        if update_extracted_records:
            print(f"Total extracted_records fixes : {ext_update_count}")
        print(f"Titles with no trailing date : {no_date_count}")
        print("-" * 70)

        if not to_update:
            print("✅ All record document dates are already fully aligned! No updates needed.")
            return

        # Display preview for sample records
        sample_size = min(5, len(to_update))
        print(f"\n🔍 Sample Previews (First {sample_size} records):")
        for i, item in enumerate(to_update[:sample_size], start=1):
            print(f"\n[{i}] Record ID: {item['extracted_record_id']} ({item['target_name']})")
            print(f"    URL        : {item['source_url']}")
            print(f"    TITLE      : {item['title']}")
            print(f"    RESOLVED   : {item['resolved_iso']}")
            if item["meta_needs_fix"]:
                print(f"    METADATA   : {item['old_meta_date']}  ->  {item['resolved_iso']}")
            if item["judgment_needs_fix"]:
                print(f"    JUDGMENT   : {item['old_judgment_date']}  ->  {item['resolved_iso']}")
            if item["ext_needs_fix"]:
                print(f"    EXT_TABLE  : {item['old_ext_date']}  ->  {item['resolved_iso']}")

        print("=" * 70)

        if dry_run:
            print("🔍 DRY RUN COMPLETE — No database modifications were made.")
            return

        if not force:
            confirm = input(f"\nAre you sure you want to update {len(to_update)} record(s)? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("❌ Operation cancelled by user.")
                return

        print(f"\n⏳ Updating {len(to_update)} record(s) in database (batch size 100)...")
        updated_count = 0
        batch_size = 100

        for i in range(0, len(to_update), batch_size):
            chunk = to_update[i : i + batch_size]
            async with conn.transaction():
                for item in chunk:
                    if item["scrub_needs_fix"]:
                        await conn.execute(
                            """
                            UPDATE scrubbed_records
                            SET data = jsonb_set(
                                    jsonb_set(
                                        jsonb_set(
                                            data,
                                            '{metadata,document_date}',
                                            to_jsonb($1::text),
                                            true
                                        ),
                                        '{extracted_data,judgment_date}',
                                        to_jsonb($1::text),
                                        true
                                    ),
                                    '{extracted_data,hearing_date}',
                                    CASE 
                                        WHEN data->'extracted_data'->>'hearing_date' = '2026-08-22' 
                                          OR data->'extracted_data'->>'hearing_date' = data->'extracted_data'->>'judgment_date'
                                        THEN to_jsonb($1::text)
                                        ELSE COALESCE(data->'extracted_data'->'hearing_date', to_jsonb($1::text))
                                    END,
                                    true
                                ),
                                updated_at = NOW()
                            WHERE id = $2
                            """,
                            item["resolved_iso"],
                            item["scrubbed_id"],
                        )
                    if item["ext_needs_fix"]:
                        await conn.execute(
                            """
                            UPDATE extracted_records
                            SET document_date = $1,
                                updated_at = NOW()
                            WHERE id = $2
                            """,
                            item["resolved_date"],
                            item["extracted_record_id"],
                        )
                    updated_count += 1
            print(f"  Processed {updated_count}/{len(to_update)} records...")

        print(f"✅ Successfully updated {updated_count} record(s) across database tables.")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Fix and align document_date across scrubbed_records and extracted_records based on title strings."
    )
    parser.add_argument(
        "--target",
        "-t",
        type=str,
        default=None,
        help="Filter by target name (e.g. ZACC, ZACT, ZASCA). Default: all targets.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without making any database changes (preview mode).",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Execute updates without interactive confirmation.",
    )
    parser.add_argument(
        "--skip-extracted-records",
        action="store_true",
        help="Only update scrubbed_records.data metadata, skipping extracted_records table.",
    )

    args = parser.parse_args()
    asyncio.run(
        fix_document_dates(
            target_name=args.target,
            dry_run=args.dry_run,
            force=args.force,
            update_extracted_records=not args.skip_extracted_records,
        )
    )


if __name__ == "__main__":
    main()
