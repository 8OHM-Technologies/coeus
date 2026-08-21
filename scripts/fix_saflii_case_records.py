#!/usr/bin/env python3
"""
Fix SAFLII Case Records Script
===============================
Standardizes court names and judge name formatting in existing `scrubbed_records`:
1. Identifies case records in `scrubbed_records` (having `extracted_data`, `court`, or `judges`).
2. Standardizes `court` into canonical court names using `normalize_court_name` (e.g., 'ZACC' -> 'Constitutional Court of South Africa').
3. Normalizes `judges` into standard Title Case with uppercase judicial title acronyms using `format_judge_name` (e.g., 'MAHLANGA AJ' -> 'Mahlanga AJ').
4. Updates `scrubbed_records.data` and `scrubbed_records.updated_at` in batch transactions.

Usage:
    # Dry run (inspect what would be changed without modifying DB):
    python scripts/fix_saflii_case_records.py --dry-run

    # Fix all case entries (interactive prompt):
    python scripts/fix_saflii_case_records.py

    # Fix without confirmation:
    python scripts/fix_saflii_case_records.py --force

    # Filter by target name:
    python scripts/fix_saflii_case_records.py --target ZACC --force
"""

import argparse
import asyncio
import copy
import json
import os
import re
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
from extraction_workers.llm_extractor import (
    normalize_court_name,
    format_judge_name,
    sanitize_and_format_judges,
)


async def fix_case_records(
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
):
    print("=" * 70)
    print("⚖️  SAFLII CASE RECORDS COURT & JUDGE NORMALIZER")
    print("=" * 70)
    print(f"Target Filter: {target_name or 'All Targets'}")
    print(f"Mode         : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
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
                   s.data AS scrubbed_data,
                   e.source_url,
                   COALESCE(t.target_name, 'Unknown') AS target_name
            FROM scrubbed_records s
            JOIN extracted_records e ON s.extracted_record_id = e.id
            LEFT JOIN targets t ON e.target_id = t.id
            WHERE (
                s.data ? 'extracted_data'
                OR s.data ? 'court'
                OR s.data ? 'judges'
                OR e.record_type = 'saflii_courts'
            )
            {target_clause}
            ORDER BY t.target_name ASC;
        """

        print("⏳ Fetching case records from database...")
        records = await conn.fetch(query, *params)
        total_scanned = len(records)

        if total_scanned == 0:
            print("⚠️  No matching case records found in scrubbed_records.")
            return

        to_update = []
        target_counts = Counter()
        changed_courts_count = 0
        changed_judges_count = 0

        for rec in records:
            scrubbed_data = rec["scrubbed_data"]
            if isinstance(scrubbed_data, str):
                try:
                    scrubbed_data = json.loads(scrubbed_data)
                except Exception:
                    continue

            if not isinstance(scrubbed_data, dict):
                continue

            rec_target = rec["target_name"]
            meta = scrubbed_data.get("metadata")
            if isinstance(meta, dict) and meta.get("target_name"):
                rec_target = meta.get("target_name") or rec_target

            updated_data = copy.deepcopy(scrubbed_data)
            has_modifications = False
            old_court_preview = None
            new_court_preview = None
            old_judges_preview = None
            new_judges_preview = None

            # Case A: nested extracted_data payload (SafliiCaseExtraction)
            if isinstance(updated_data.get("extracted_data"), dict):
                ext = updated_data["extracted_data"]
                original_court = ext.get("court")
                normalized_court = normalize_court_name(original_court, rec_target)

                if normalized_court != original_court:
                    ext["court"] = normalized_court
                    has_modifications = True
                    changed_courts_count += 1
                    old_court_preview = original_court
                    new_court_preview = normalized_court

                original_judges = ext.get("judges") or []
                cleaned_judges = sanitize_and_format_judges(original_judges)

                if cleaned_judges != original_judges:
                    ext["judges"] = cleaned_judges
                    has_modifications = True
                    changed_judges_count += 1
                    old_judges_preview = original_judges
                    new_judges_preview = cleaned_judges

            # Case B: top-level court and judges payload
            if "court" in updated_data and not isinstance(updated_data.get("extracted_data"), dict):
                original_court = updated_data.get("court")
                normalized_court = normalize_court_name(original_court, rec_target)

                if normalized_court != original_court:
                    updated_data["court"] = normalized_court
                    has_modifications = True
                    changed_courts_count += 1
                    old_court_preview = original_court
                    new_court_preview = normalized_court

            if "judges" in updated_data and not isinstance(updated_data.get("extracted_data"), dict):
                original_judges = updated_data.get("judges") or []
                cleaned_judges = sanitize_and_format_judges(original_judges)

                if cleaned_judges != original_judges:
                    updated_data["judges"] = cleaned_judges
                    has_modifications = True
                    changed_judges_count += 1
                    old_judges_preview = original_judges
                    new_judges_preview = cleaned_judges

            if has_modifications:
                target_counts[rec["target_name"]] += 1
                to_update.append({
                    "scrubbed_id": rec["scrubbed_id"],
                    "extracted_record_id": rec["extracted_record_id"],
                    "target_name": rec["target_name"],
                    "source_url": rec["source_url"],
                    "updated_data": updated_data,
                    "old_court": old_court_preview,
                    "new_court": new_court_preview,
                    "old_judges": old_judges_preview,
                    "new_judges": new_judges_preview,
                })

        print(f"Scanned {total_scanned} case record(s). Found {len(to_update)} requiring normalization:")
        for tname, count in sorted(target_counts.items()):
            print(f"  • {tname:<15}: {count:>4} record(s)")
        print(f"Total Court normalizations: {changed_courts_count}")
        print(f"Total Judge normalizations: {changed_judges_count}")
        print("-" * 70)

        if not to_update:
            print("✅ All case records are already fully standardized! No updates needed.")
            return

        # Display preview for sample records
        sample_size = min(5, len(to_update))
        print(f"\n🔍 Sample Previews (First {sample_size} records):")
        for i, item in enumerate(to_update[:sample_size], start=1):
            print(f"\n[{i}] Record ID: {item['extracted_record_id']} ({item['target_name']})")
            print(f"    URL      : {item['source_url']}")
            if item["old_court"] is not None:
                print(f"    COURT    : {item['old_court']}  ->  {item['new_court']}")
            if item["old_judges"] is not None:
                print(f"    JUDGES   : {item['old_judges']}  ->  {item['new_judges']}")

        print("=" * 70)

        if dry_run:
            print("🔍 DRY RUN COMPLETE — No database modifications were made.")
            return

        if not force:
            confirm = input(f"\nAre you sure you want to update {len(to_update)} record(s) in scrubbed_records? [y/N]: ").strip().lower()
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
                    await conn.execute(
                        """
                        UPDATE scrubbed_records
                        SET data = $1, updated_at = NOW()
                        WHERE id = $2
                        """,
                        json.dumps(item["updated_data"], ensure_ascii=False),
                        item["scrubbed_id"],
                    )
                    updated_count += 1
            print(f"  Processed {updated_count}/{len(to_update)} records...")

        print(f"✅ Successfully normalized and updated {updated_count} case record(s) in scrubbed_records.")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Standardize court names and judge name formatting in existing scrubbed_records."
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Filter by specific target name (e.g. ZACC, ZASCA, ZAGPJHC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect changes without modifying the database",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Bypass interactive confirmation prompt",
    )

    args = parser.parse_args()
    asyncio.run(
        fix_case_records(
            target_name=args.target,
            dry_run=args.dry_run,
            force=args.force,
        )
    )


if __name__ == "__main__":
    main()
