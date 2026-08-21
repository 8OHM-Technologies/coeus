#!/usr/bin/env python3
"""
Fix SAFLII Journal Records Script
=================================
Cleans existing SAFLII Journal entries in `scrubbed_records`:
1. Identifies journal records (category = 'journals' or source_url matching /za/journals/).
2. Strips LawCite navigation breadcrumbs, SAFLII UI noise lines, and excess blank lines
   from `data->'formatted_text'` using `clean_saflii_text`.
3. Updates `scrubbed_records.data` and `scrubbed_records.updated_at`.

Usage:
    # Dry run (inspect what would be changed without modifying DB):
    python scripts/fix_saflii_journal_records.py --dry-run

    # Fix all journal entries (interactive prompt):
    python scripts/fix_saflii_journal_records.py

    # Fix without confirmation:
    python scripts/fix_saflii_journal_records.py --force

    # Filter by target name:
    python scripts/fix_saflii_journal_records.py --target PER --force
"""

import argparse
import asyncio
import json
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
from extraction_workers.utils.saflii_document_parser import clean_saflii_text


async def fix_journal_records(
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
):
    print("=" * 65)
    print("📚 SAFLII JOURNAL SCRUBBED RECORDS CLEANER")
    print("=" * 65)
    print(f"Target Filter: {target_name or 'All Targets'}")
    print(f"Mode         : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print("-" * 65)

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
                s.data ? 'formatted_text'
                OR e.source_url LIKE '%/za/journals/%'
            )
            {target_clause}
            ORDER BY t.target_name ASC;
        """

        print("⏳ Fetching journal records from database...")
        records = await conn.fetch(query, *params)
        total_scanned = len(records)

        if total_scanned == 0:
            print("⚠️  No matching journal records found in scrubbed_records.")
            return

        to_update = []
        target_counts = Counter()

        for rec in records:
            scrubbed_data = rec["scrubbed_data"]
            if isinstance(scrubbed_data, str):
                try:
                    scrubbed_data = json.loads(scrubbed_data)
                except Exception:
                    continue

            if not isinstance(scrubbed_data, dict):
                continue

            raw_text = scrubbed_data.get("formatted_text")
            if not raw_text or not isinstance(raw_text, str):
                continue

            cleaned_formatted = clean_saflii_text(raw_text)
            original_in_scrubbed = scrubbed_data.get("formatted_text", "")

            if cleaned_formatted != original_in_scrubbed:
                updated_data = dict(scrubbed_data)
                updated_data["formatted_text"] = cleaned_formatted
                target_counts[rec["target_name"]] += 1
                to_update.append({
                    "scrubbed_id": rec["scrubbed_id"],
                    "extracted_record_id": rec["extracted_record_id"],
                    "target_name": rec["target_name"],
                    "source_url": rec["source_url"],
                    "old_len": len(original_in_scrubbed),
                    "new_len": len(cleaned_formatted),
                    "updated_data": updated_data,
                    "preview_old": (original_in_scrubbed[:200] if original_in_scrubbed else "[EMPTY]").replace("\n", " "),
                    "preview_new": cleaned_formatted[:200].replace("\n", " "),
                })

        print(f"Scanned {total_scanned} journal record(s). Found {len(to_update)} requiring updates:")
        for tname, count in sorted(target_counts.items()):
            print(f"  • {tname:<15}: {count:>4} record(s) need cleaning")
        print("-" * 65)

        if not to_update:
            print("✅ All journal records are already clean! No updates needed.")
            return

        # Display preview for sample records
        sample_size = min(3, len(to_update))
        print(f"\n🔍 Sample Previews (First {sample_size} records):")
        for i, item in enumerate(to_update[:sample_size], start=1):
            print(f"\n[{i}] Record ID: {item['extracted_record_id']} ({item['target_name']})")
            print(f"    URL     : {item['source_url']}")
            print(f"    Size    : {item['old_len']} chars -> {item['new_len']} chars (stripped {item['old_len'] - item['new_len']} chars)")
            print(f"    BEFORE  : {item['preview_old']}...")
            print(f"    AFTER   : {item['preview_new']}...")

        print("=" * 65)

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

        print(f"✅ Successfully updated {updated_count} journal record(s) in scrubbed_records.")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Clean LawCite, UI noise lines, and excess blank lines from existing journal entries in scrubbed_records."
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Filter by specific target name (e.g. PER)",
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
        fix_journal_records(
            target_name=args.target,
            dry_run=args.dry_run,
            force=args.force,
        )
    )


if __name__ == "__main__":
    main()
