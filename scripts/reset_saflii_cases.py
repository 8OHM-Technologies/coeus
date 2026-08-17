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
import json
import os
import subprocess
import sys
from collections import Counter

# Ensure repo root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def run_psql(sql: str) -> str:
    """Executes SQL via docker exec with psql -c."""
    clean_sql = " ".join(sql.strip().split())
    # 1. Try docker exec postgres
    cmd_docker = ["docker", "exec", "postgres", "psql", "-U", "postgres", "-d", "coeus", "-t", "-A", "-c", clean_sql]
    try:
        res = subprocess.run(cmd_docker, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            return res.stdout.strip()
        elif res.stderr:
            print("Docker psql stderr:", res.stderr.strip())
    except Exception as e:
        print("Docker exec error:", e)

    # 2. Fallback to local psql
    host = os.environ.get("POSTGRES_HOST", "localhost")
    user = os.environ.get("POSTGRES_USER", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "coeus")
    cmd_local = ["psql", "-h", host, "-U", user, "-p", port, "-d", db, "-t", "-A", "-c", sql]
    env = os.environ.copy()
    if "POSTGRES_PASSWORD" in env:
        env["PGPASSWORD"] = env["POSTGRES_PASSWORD"]
    try:
        res = subprocess.run(cmd_local, capture_output=True, text=True, env=env, timeout=10)
        if res.returncode == 0:
            return res.stdout.strip()
        elif res.stderr:
            print("Local psql stderr:", res.stderr.strip())
    except Exception as e:
        print("Local psql error:", e)

    return ""


def reset_cases_sync(
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

    where_extra = ""
    if target_name:
        where_extra += f" AND t.target_name = '{target_name}'"
    if pipeline:
        where_extra += f" AND e.record_type = '{pipeline}'"

    query_fetch = f"""
        SELECT json_build_object(
            'id', e.id,
            'status', e.status,
            'target_name', COALESCE(t.target_name, 'Unknown'),
            'requires_human_review', e.requires_human_review,
            'has_parsed', (p.id IS NOT NULL),
            'has_scrubbed', (s.id IS NOT NULL)
        )::text
        FROM scrubbed_records s
        JOIN extracted_records e ON s.extracted_record_id = e.id
        LEFT JOIN targets t ON e.target_id = t.id
        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
        WHERE e.data->>'category' = '{category}'
        {where_extra}
        ORDER BY t.target_name ASC;
    """

    out = run_psql(query_fetch)
    lines = [l.strip() for l in out.split("\n") if l.strip()]

    if not lines or lines[0] == "":
        print("⚠️  No matching case records found.")
        return

    records = [json.loads(l) for l in lines]
    total_records = len(records)

    target_counts = Counter([r.get("target_name") or "Unknown" for r in records])
    scrubbed_count = sum(1 for r in records if r.get("has_scrubbed"))
    parsed_count = sum(1 for r in records if r.get("has_parsed"))
    review_count = sum(1 for r in records if r.get("requires_human_review"))
    record_ids = [r["id"] for r in records]
    record_ids_sql = ",".join(f"'{rid}'" for rid in record_ids)

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
    reset_sql = f"""
        BEGIN;
        DELETE FROM scrubbed_records WHERE extracted_record_id IN ({record_ids_sql});
        DELETE FROM parsed_records WHERE extracted_record_id IN ({record_ids_sql});
        UPDATE extracted_records
        SET status = 'detailed',
            parsed_at = NULL,
            scrubbed_at = NULL,
            requires_human_review = FALSE,
            review_reason = NULL,
            updated_at = NOW()
        WHERE id IN ({record_ids_sql});
        COMMIT;
    """

    res = run_psql(reset_sql)
    print("=" * 65)
    print("✅ RESET COMPLETE")
    print(f"  • {parsed_count} deleted from parsed_records")
    print(f"  • {scrubbed_count} deleted from scrubbed_records")
    print(f"  • {total_records} reset in extracted_records (status='detailed', parsed_at=NULL, scrubbed_at=NULL)")
    print("=" * 65)
    print("💡 You can now trigger the parser and LLM extractor pipeline:")
    print("   python extraction_workers/llm_extractor.py --pipeline saflii")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Reset SAFLII case records to re-run parsing and LLM extraction.")
    parser.add_argument("--pipeline", "-p", default=None, help="Pipeline name / record_type filter (optional)")
    parser.add_argument("--category", "-c", default="cases", help="Record category filter (default: cases)")
    parser.add_argument("--target", "-t", default=None, help="Target name filter (e.g. ZACC, ZACAC)")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Preview records to reset without making changes")
    parser.add_argument("--force", "-y", action="store_true", help="Execute without interactive confirmation prompt")

    args = parser.parse_args()
    reset_cases_sync(
        pipeline=args.pipeline,
        category=args.category,
        target_name=args.target,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    main()
