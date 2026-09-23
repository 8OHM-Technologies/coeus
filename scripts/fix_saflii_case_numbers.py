#!/usr/bin/env python3
"""
Fix SAFLII Case Numbers Script
==============================
Resolves, normalizes, and fixes missing, truncated, or corrupt `case_number`
across SAFLII case law records in `scrubbed_records` and `extracted_records`:

1. Scans SAFLII case records in `scrubbed_records` joined with `extracted_records` and `targets`.
2. Extracts authoritative case numbers from document titles (matching `(CASE_NUM) [YEAR] ZA...`).
3. For edge-case titles without a case number, falls back to parsing the judgment header.
4. Clears corrupt neutral citations (e.g. `[2021] ZACC 34`) or foreign precedent numbers mistakenly stored as case numbers.
5. Updates `scrubbed_records.data->'metadata'->>'case_number'` and `scrubbed_records.data->'extracted_data'->>'case_number'`.
6. Synchronizes `extracted_records.data->>'case_number'` with the resolved case number.
7. Executes atomic batch updates in transactions with detailed progress reporting.

Usage:
    # Dry run (inspect what would be changed without modifying DB):
    python scripts/fix_saflii_case_numbers.py --dry-run

    # Fix all records (interactive prompt):
    python scripts/fix_saflii_case_numbers.py

    # Fix without confirmation:
    python scripts/fix_saflii_case_numbers.py --force

    # Filter by target name (e.g. ZACC, ZACT, ZASCA):
    python scripts/fix_saflii_case_numbers.py --target ZACC --force

    # Test on a small limit:
    python scripts/fix_saflii_case_numbers.py --limit 50 --dry-run
"""

import argparse
import asyncio
import os
import re
import sys
import time
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


def extract_case_number_from_title(title: str | None) -> str | None:
    """Extract case number from standard SAFLII title pattern: ... (CASE_NO) [YEAR] ZA..."""
    if not title:
        return None
    m = re.search(r'\(([^()]+)\)\s*\[\d{4}\]\s*ZA', title, re.IGNORECASE)
    if m:
        cand = m.group(1).strip(" ;,()")
        # Must contain at least one digit and not be a non-case keyword
        if (
            re.search(r'\d', cand)
            and not any(w in cand.lower() for w in ["judgment", "appeal", "heard", "delivered", "unreported", "coram"])
            and not re.match(r'^\[?\d{4}\]?\s*ZA', cand, re.IGNORECASE)
            and len(cand) < 100
        ):
            return cand
    return None


def extract_case_number_from_header(header_text: str | None) -> str | None:
    """Fallback extraction of case number from judgment header snippet."""
    if not header_text:
        return None
    header_pat = re.compile(r'case\s*(?:no|number|nos|reference|nr)?[:.\s]+([^\n\r]+)', re.IGNORECASE)
    m = header_pat.search(header_text)
    if m:
        cand = m.group(1).strip()
        cand = re.split(r'[\r\n\t]|(?:date of|before:|heard on|in the matter|judgment)', cand, flags=re.IGNORECASE)[0].strip(" ;,.:()")
        if (
            re.search(r'\d', cand)
            and 2 <= len(cand) < 60
            and not re.match(r'^\[?\d{4}\]?\s*ZA', cand, re.IGNORECASE)
        ):
            return cand
    return None


async def fix_case_numbers(
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    batch_size: int = 200,
    update_extracted_records: bool = True,
):
    print("=" * 70)
    print("⚖️  SAFLII SCRUBBED & EXTRACTED CASE NUMBER NORMALIZER")
    print("=" * 70)
    print(f"Target Filter            : {target_name or 'All Targets'}")
    print(f"Limit                    : {limit or 'No limit'}")
    print(f"Mode                     : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print(f"Batch Size               : {batch_size}")
    print(f"Update Extracted Records : {'Yes' if update_extracted_records else 'No'}")
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

        limit_clause = ""
        if limit:
            limit_clause = f"LIMIT {int(limit)}"

        query = f"""
            SELECT s.id AS scrubbed_id,
                   s.extracted_record_id,
                   s.data ? 'metadata' AS has_metadata,
                   s.data ? 'extracted_data' AS has_extracted_data,
                   s.data->'metadata'->>'case_number' AS current_meta_case_number,
                   s.data->'extracted_data'->>'case_number' AS current_ext_data_case_number,
                   COALESCE(
                       e.data->>'title',
                       s.data->>'title',
                       s.data->'extracted_data'->>'title',
                       e.data->'extracted_data'->>'title'
                   ) AS title,
                   e.data->>'case_number' AS current_ext_rec_case_number,
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
            ORDER BY t.target_name ASC, s.created_at ASC
            {limit_clause};
        """

        print("⏳ Fetching case law records from database...")
        t0 = time.time()
        records = await conn.fetch(query, *params)
        fetch_time = time.time() - t0
        total_scanned = len(records)
        print(f"✅ Fetched {total_scanned} records in {fetch_time:.2f}s.")

        if total_scanned == 0:
            print("⚠️  No matching records found.")
            return

        # Pass 1: Extract from title
        to_check_header = []
        parsed_results = {}

        for rec in records:
            scrubbed_id = rec["scrubbed_id"]
            title = rec["title"] or ""
            cn = extract_case_number_from_title(title)
            if cn:
                parsed_results[scrubbed_id] = (cn, "title")
            else:
                to_check_header.append(rec)

        # Pass 2: Fallback to header snippet for records where title extraction missed
        if to_check_header:
            print(f"🔎 Checking header snippet for {len(to_check_header)} records without case number in title...")
            header_ext_ids = [r["extracted_record_id"] for r in to_check_header]
            header_chunk_size = 300
            for i in range(0, len(header_ext_ids), header_chunk_size):
                chunk = header_ext_ids[i : i + header_chunk_size]
                snippets = await conn.fetch(
                    """
                    SELECT id, substring(COALESCE(data->>'full_text', '') from 1 for 1500) as snippet
                    FROM extracted_records
                    WHERE id = ANY($1::uuid[])
                    """,
                    chunk,
                )
                snippet_map = {row["id"]: row["snippet"] for row in snippets}
                for rec in to_check_header[i : i + header_chunk_size]:
                    snip = snippet_map.get(rec["extracted_record_id"])
                    cn = extract_case_number_from_header(snip)
                    if cn:
                        parsed_results[rec["scrubbed_id"]] = (cn, "header")
                    else:
                        parsed_results[rec["scrubbed_id"]] = (None, "unresolved")

        # Pass 3: Evaluate what needs updating
        to_update = []
        target_counts = Counter()
        missing_to_found = 0
        neutral_to_real = 0
        corrupt_cleared = 0
        updated_to_authoritative = 0

        for rec in records:
            scrubbed_id = rec["scrubbed_id"]
            title = rec["title"] or ""
            current_meta_cn = rec["current_meta_case_number"]
            current_ext_data_cn = rec["current_ext_data_case_number"]
            current_ext_rec_cn = rec["current_ext_rec_case_number"]

            resolved_cn, source = parsed_results.get(scrubbed_id, (None, "none"))

            # Determine whether current value is a corrupt neutral citation
            curr_is_neutral = bool(
                current_meta_cn and re.match(r'^\[?\d{4}\]?\s*ZA', current_meta_cn.strip(), re.IGNORECASE)
            )

            needs_scrub_update = False
            fix_type = ""

            if resolved_cn:
                if current_meta_cn != resolved_cn:
                    needs_scrub_update = True
                    if not current_meta_cn:
                        fix_type = "missing_populated"
                        missing_to_found += 1
                    elif curr_is_neutral:
                        fix_type = "neutral_replaced"
                        neutral_to_real += 1
                    else:
                        fix_type = "case_number_corrected"
                        updated_to_authoritative += 1
            else:
                # Could not resolve a real case number (e.g. advisory note / gazette)
                # If current_meta_cn has a neutral citation or precedent leak, clear it
                if curr_is_neutral or (current_meta_cn and len(current_meta_cn) > 80):
                    needs_scrub_update = True
                    fix_type = "corrupt_cleared"
                    corrupt_cleared += 1

            # Check if extracted_records table needs update
            ext_needs_update = update_extracted_records and (current_ext_rec_cn != resolved_cn)

            if needs_scrub_update or ext_needs_update:
                target_counts[rec["target_name"]] += 1
                to_update.append({
                    "scrubbed_id": scrubbed_id,
                    "extracted_record_id": rec["extracted_record_id"],
                    "target_name": rec["target_name"],
                    "source_url": rec["source_url"],
                    "title": title,
                    "resolved_cn": resolved_cn,
                    "source": source,
                    "fix_type": fix_type,
                    "old_meta_cn": current_meta_cn,
                    "old_ext_data_cn": current_ext_data_cn,
                    "old_ext_rec_cn": current_ext_rec_cn,
                    "needs_scrub_update": needs_scrub_update,
                    "needs_ext_update": ext_needs_update,
                })

        print(f"\n📊 Scanned {total_scanned} records. Found {len(to_update)} requiring case number updates:")
        for tname, count in sorted(target_counts.items()):
            print(f"  • {tname:<15}: {count:>4} record(s)")
        print(f"\nBreakdown of fixes:")
        print(f"  • Missing case numbers populated       : {missing_to_found}")
        print(f"  • Neutral citations replaced with real : {neutral_to_real}")
        print(f"  • Case numbers aligned to authoritative: {updated_to_authoritative}")
        print(f"  • Corrupt citations cleared            : {corrupt_cleared}")
        print("-" * 70)

        if not to_update:
            print("✅ All record case numbers are already fully normalized! No updates needed.")
            return

        # Display sample previews
        sample_size = min(8, len(to_update))
        print(f"🔍 Sample Previews (First {sample_size} records):")
        for i, item in enumerate(to_update[:sample_size], start=1):
            print(f"\n[{i}] Record ID: {item['extracted_record_id']} ({item['target_name']})")
            print(f"    URL      : {item['source_url']}")
            print(f"    TITLE    : {item['title']}")
            print(f"    FIX TYPE : {item['fix_type']} (via {item['source']})")
            print(f"    CURRENT  : {repr(item['old_meta_cn'])}")
            print(f"    RESOLVED : {repr(item['resolved_cn'])}")

        print("=" * 70)

        if dry_run:
            print("🔍 DRY RUN COMPLETE — No database modifications were made.")
            return

        if not force:
            confirm = input(f"\nAre you sure you want to update {len(to_update)} record(s)? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                print("❌ Operation cancelled by user.")
                return

        print(f"\n⏳ Updating {len(to_update)} record(s) in database (batch size {batch_size})...")
        updated_count = 0
        t_start = time.time()

        for i in range(0, len(to_update), batch_size):
            chunk = to_update[i : i + batch_size]
            async with conn.transaction():
                for item in chunk:
                    if item["needs_scrub_update"]:
                        await conn.execute(
                            """
                            UPDATE scrubbed_records
                            SET data = jsonb_set(
                                    jsonb_set(
                                        data,
                                        '{metadata,case_number}',
                                        COALESCE(to_jsonb($1::text), 'null'::jsonb),
                                        true
                                    ),
                                    '{extracted_data,case_number}',
                                    COALESCE(to_jsonb($1::text), 'null'::jsonb),
                                    true
                                ),
                                updated_at = NOW()
                            WHERE id = $2
                            """,
                            item["resolved_cn"],
                            item["scrubbed_id"],
                        )

                    if item["needs_ext_update"]:
                        await conn.execute(
                            """
                            UPDATE extracted_records
                            SET data = jsonb_set(
                                    data,
                                    '{case_number}',
                                    COALESCE(to_jsonb($1::text), 'null'::jsonb),
                                    true
                                ),
                                updated_at = NOW()
                            WHERE id = $2
                            """,
                            item["resolved_cn"],
                            item["extracted_record_id"],
                        )
                    updated_count += 1

            pct = (updated_count / len(to_update)) * 100
            elapsed = time.time() - t_start
            rate = updated_count / elapsed if elapsed > 0 else 0
            print(f"  Processed {updated_count}/{len(to_update)} records ({pct:.1f}%) — {rate:.1f} rec/s...")

        print(f"\n✅ Successfully updated {updated_count} record(s) across database tables in {time.time()-t_start:.2f}s.")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Fix and align case_number across scrubbed_records and extracted_records."
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
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Limit number of records to scan/update.",
    )
    parser.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=200,
        help="Number of records to update per transaction chunk (default: 200).",
    )
    parser.add_argument(
        "--skip-extracted-records",
        action="store_true",
        help="Only update scrubbed_records, skipping extracted_records table.",
    )

    args = parser.parse_args()
    asyncio.run(
        fix_case_numbers(
            target_name=args.target,
            dry_run=args.dry_run,
            force=args.force,
            limit=args.limit,
            batch_size=args.batch_size,
            update_extracted_records=not args.skip_extracted_records,
        )
    )


if __name__ == "__main__":
    main()
