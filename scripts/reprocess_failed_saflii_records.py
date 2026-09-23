#!/usr/bin/env python3
"""
Reprocess Failed SAFLII Records Script
=======================================
Re-runs the updated `split_saflii_document` parser on `saflii_courts` records
flagged with `requires_human_review = TRUE` and `review_reason = 'Document parsing failed'`.

When the enhanced section parser successfully isolates the court order:
1. Updates `parsed_records.data` with the newly parsed sections (header, judgment, order, appearances, footnotes).
2. Clears the `requires_human_review` flag back to `FALSE` and resets `review_reason` to `NULL` in `extracted_records`.

Usage:
    # Dry run (inspect what would be recovered without modifying DB):
    python scripts/reprocess_failed_saflii_records.py --dry-run

    # Dry run on a small sample:
    python scripts/reprocess_failed_saflii_records.py --limit 50 --dry-run

    # Execute reprocessing with confirmation prompt:
    python scripts/reprocess_failed_saflii_records.py

    # Execute reprocessing without confirmation:
    python scripts/reprocess_failed_saflii_records.py --force
"""

import argparse
import asyncio
import json
import os
import sys
import time

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
from extraction_workers.utils.saflii_document_parser import split_saflii_document


async def reprocess_failed_records(
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    batch_size: int = 200,
):
    print("=" * 70)
    print("⚖️  SAFLII FAILED RECORDS REPROCESSOR & REVIEW RESOLVER")
    print("=" * 70)
    print(f"Limit        : {limit or 'All matching records'}")
    print(f"Mode         : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print("-" * 70)

    try:
        conn = await get_db_connection()
    except Exception as exc:
        print(f"❌ Database connection failed: {exc}")
        sys.exit(1)

    try:
        query_ids = """
            SELECT id
            FROM extracted_records
            WHERE record_type = 'saflii_courts'
              AND requires_human_review = TRUE
              AND review_reason = 'Document parsing failed'
            ORDER BY id ASC
        """
        if limit:
            query_ids += f" LIMIT {int(limit)}"

        print("🔍 Querying failed candidate IDs from database...")
        id_rows = await conn.fetch(query_ids)
        candidate_ids = [r["id"] for r in id_rows]
        total_found = len(candidate_ids)
        print(f"📊 Found {total_found} candidate records needing reprocessing.")

        if total_found == 0:
            print("✅ No records found requiring human review under 'Document parsing failed'.")
            return

        recovered_records = []
        still_failing = []

        print("⚙️  Fetching records and running enhanced parser in chunks...")
        t0 = time.time()
        fetch_chunk_size = 200

        for i in range(0, total_found, fetch_chunk_size):
            chunk_ids = candidate_ids[i : i + fetch_chunk_size]
            records = await conn.fetch(
                """
                SELECT id, data->>'full_text' as full_text, data->>'center_content' as center_content
                FROM extracted_records
                WHERE id = ANY($1::uuid[])
                """,
                chunk_ids,
            )

            for rec in records:
                rec_id = rec["id"]
                ft = rec["full_text"] or ""
                cc = rec["center_content"] or ""

                parsed = split_saflii_document(ft, cc)
                null_vals = parsed.get("null_values") or []

                # A record is recovered if "order" is successfully isolated
                if "order" not in null_vals and parsed.get("order", "").strip():
                    recovered_records.append((rec_id, parsed))
                else:
                    still_failing.append((rec_id, null_vals))

            processed_so_far = min(i + fetch_chunk_size, total_found)
            elapsed = time.time() - t0
            print(f"   Processed {processed_so_far}/{total_found} records ({len(recovered_records)} recovered so far, {elapsed:.1f}s)...")

        recovered_count = len(recovered_records)
        recovery_rate = (recovered_count / total_found * 100) if total_found else 0.0

        print("\n" + "=" * 70)
        print("📈 REPROCESSING RESULTS SUMMARY")
        print("=" * 70)
        print(f"Total Evaluated       : {total_found}")
        print(f"Successfully Recovered: {recovered_count} ({recovery_rate:.1f}%)")
        print(f"Still Needing Review  : {len(still_failing)} ({100 - recovery_rate:.1f}%)")
        print("=" * 70)

        if dry_run:
            print("\n🔍 DRY RUN: No database records were modified.")
            return

        if not force and recovered_count > 0:
            confirm = input(f"\n❓ Proceed with applying fixes to {recovered_count} database records? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Aborted by user.")
                return

        if recovered_count == 0:
            print("No records recovered to update.")
            return

        print(f"\n⚡ Applying updates to {recovered_count} records in batches of {batch_size}...")
        updated_count = 0
        t1 = time.time()

        for i in range(0, recovered_count, batch_size):
            chunk = recovered_records[i : i + batch_size]
            async with conn.transaction():
                # 1. Update parsed_records data
                parsed_args = [
                    (
                        json.dumps(parsed_payload, ensure_ascii=False),
                        rec_id,
                    )
                    for rec_id, parsed_payload in chunk
                ]
                await conn.executemany(
                    """
                    UPDATE parsed_records
                    SET data = $1::jsonb,
                        updated_at = NOW()
                    WHERE extracted_record_id = $2
                    """,
                    parsed_args,
                )

                # 2. Clear requires_human_review flag on extracted_records
                id_args = [(rec_id,) for rec_id, _ in chunk]
                await conn.executemany(
                    """
                    UPDATE extracted_records
                    SET requires_human_review = FALSE,
                        review_reason = NULL,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    id_args,
                )

            updated_count += len(chunk)
            print(f"   Saved {updated_count}/{recovered_count} records...")

        elapsed_update = time.time() - t1
        print(f"\n🎉 Successfully updated and cleared review flags for {updated_count} records in {elapsed_update:.1f}s!")

    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Reprocess SAFLII records marked for human review")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of records to reprocess")
    parser.add_argument("--dry-run", action="store_true", help="Inspect recovery without modifying DB")
    parser.add_argument("--force", action="store_true", help="Execute without interactive confirmation")
    parser.add_argument("--batch-size", type=int, default=200, help="DB update batch size (default: 200)")

    args = parser.parse_args()
    asyncio.run(
        reprocess_failed_records(
            limit=args.limit,
            dry_run=args.dry_run,
            force=args.force,
            batch_size=args.batch_size,
        )
    )


if __name__ == "__main__":
    main()
