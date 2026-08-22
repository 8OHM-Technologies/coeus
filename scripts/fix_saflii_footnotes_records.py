#!/usr/bin/env python3
"""
Fix SAFLII Footnotes & Structured Precedents Script
====================================================
Retroactively migrates existing database records in `parsed_records` and `scrubbed_records`:

1. `parsed_records`:
   - Renames section key `citations` -> `footnotes`.
   - Ensures `data['footnotes']` contains `raw_text` and `targets`.
   - Updates `parsed_records.data` and `updated_at`.

2. `scrubbed_records`:
   - Migrates `data['extracted_data']['precedents_cited']` to the structured 5-part schema:
     * `raw_citation`: Verbatim legal citation string
     * `case_name`: Party names / Style of cause (e.g. 'VVC v JRM and Others')
     * `case_number`: Court tracking number (e.g. 'CCT202/24' or 'CCT 25/24; CCT 27/24')
     * `neutral_citation`: Electronic medium-neutral citation (e.g. '[2026] ZACC 2')
     * `commercial_citations`: List of commercial report citations (e.g. ['2026 (3) BCLR 234 (CC)'])
     * `decision_date`: Date delivered (YYYY-MM-DD)
     * `treatment`: 'Applied/Followed', 'Distinguished/Overruled', or 'Referred'
     * `reasoning`: Summary of judicial treatment
     * `url`: Hyperlink target
   - Migrates metadata `citation` -> `neutral_citation`.
   - Updates `scrubbed_records.data` and `scrubbed_records.updated_at` in batch transactions.

Usage:
    # Dry run preview (inspect what would be changed without writing to DB):
    python scripts/fix_saflii_footnotes_records.py --dry-run

    # Fix all records across all courts (interactive confirmation):
    python scripts/fix_saflii_footnotes_records.py

    # Fix without confirmation:
    python scripts/fix_saflii_footnotes_records.py --force

    # Target specific court (e.g. ZACC):
    python scripts/fix_saflii_footnotes_records.py --target ZACC --force
"""

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

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
from extraction_workers.utils.citation_parser import parse_legal_citation_string


def migrate_precedent_entry(p: Any) -> Dict[str, Any]:
    """Migrate a single precedent entry to the structured 5-part footnote citation schema."""
    if not isinstance(p, dict):
        raw_str = str(p) if p else ""
        parsed = parse_legal_citation_string(raw_str)
        return {
            "raw_citation": raw_str,
            "case_name": parsed.get("case_name"),
            "case_number": parsed.get("case_number"),
            "neutral_citation": parsed.get("neutral_citation"),
            "commercial_citations": parsed.get("commercial_citations") or [],
            "decision_date": parsed.get("decision_date"),
            "treatment": "Referred",
            "reasoning": f"Cited in judgment ({raw_str})." if raw_str else "Cited in judgment.",
            "url": None,
        }

    raw_cit = (
        p.get("raw_citation")
        or p.get("case_name_citation")
        or p.get("precedent_name")
        or p.get("citation")
        or ""
    ).strip()

    parsed = parse_legal_citation_string(raw_cit)

    case_name = p.get("case_name") or parsed.get("case_name")
    case_number = p.get("case_number") or parsed.get("case_number")
    neutral_citation = p.get("neutral_citation") or parsed.get("neutral_citation")
    commercial_citations = p.get("commercial_citations") or parsed.get("commercial_citations") or []
    decision_date = p.get("decision_date") or parsed.get("decision_date")

    treatment = p.get("treatment") or "Referred"
    reasoning = p.get("reasoning") or p.get("relevance_summary") or (f"Cited in judgment ({raw_cit})." if raw_cit else "Cited in judgment.")
    url = (p.get("url") or "").strip() or None

    return {
        "raw_citation": raw_cit if raw_cit else (case_name or "Unspecified Reference"),
        "case_name": case_name,
        "case_number": case_number,
        "neutral_citation": neutral_citation,
        "commercial_citations": commercial_citations,
        "decision_date": decision_date,
        "treatment": treatment,
        "reasoning": reasoning,
        "url": url,
    }


async def fix_footnotes_records(
    target_name: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    limit: Optional[int] = None,
    only_parsed: bool = False,
    only_scrubbed: bool = False,
):
    print("=" * 75)
    print("⚖️  SAFLII FOOTNOTES & STRUCTURED PRECEDENTS MIGRATOR")
    print("=" * 75)
    print(f"Target Filter: {target_name or 'All Targets'}")
    print(f"Mode         : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    if limit:
        print(f"Limit        : {limit}")
    print("-" * 75)

    try:
        conn = await get_db_connection()
    except Exception as exc:
        print(f"❌ Database connection failed: {exc}")
        sys.exit(1)

    try:
        # =====================================================================
        # Step 1: Migrate parsed_records (citations -> footnotes)
        # =====================================================================
        if not only_scrubbed:
            print("\n" + "=" * 50)
            print("📦 STEP 1: Migrating parsed_records (citations -> footnotes)")
            print("=" * 50)

            parsed_params = []
            parsed_target_clause = ""
            if target_name:
                parsed_params.append(target_name)
                parsed_target_clause = f"AND t.target_name = ${len(parsed_params)}"

            parsed_limit_clause = f"LIMIT {limit}" if limit else ""

            parsed_query = f"""
                SELECT p.id AS parsed_id,
                       p.extracted_record_id,
                       p.data AS parsed_data,
                       COALESCE(t.target_name, 'Unknown') AS target_name
                FROM parsed_records p
                JOIN extracted_records e ON p.extracted_record_id = e.id
                LEFT JOIN targets t ON e.target_id = t.id
                WHERE p.data ? 'citations'
                {parsed_target_clause}
                ORDER BY p.created_at ASC
                {parsed_limit_clause}
            """

            parsed_rows = await conn.fetch(parsed_query, *parsed_params)
            print(f"Found {len(parsed_rows)} parsed_records with legacy 'citations' key.")

            parsed_to_update = []
            for row in parsed_rows:
                p_id = row["parsed_id"]
                raw_data = row["parsed_data"]
                if isinstance(raw_data, str):
                    try:
                        data = json.loads(raw_data)
                    except Exception:
                        continue
                elif isinstance(raw_data, dict):
                    data = copy.deepcopy(raw_data)
                else:
                    continue

                if "citations" in data:
                    citations_content = data.pop("citations")
                    if "footnotes" not in data:
                        data["footnotes"] = citations_content
                    parsed_to_update.append((p_id, json.dumps(data, ensure_ascii=False)))

            print(f"Prepared {len(parsed_to_update)} parsed_records for update.")

            if parsed_to_update and not dry_run:
                if not force:
                    confirm = input(f"\nUpdate {len(parsed_to_update)} parsed_records in DB? [y/N]: ").strip().lower()
                    if confirm not in ("y", "yes"):
                        print("Skipped parsed_records update.")
                        parsed_to_update = []

                if parsed_to_update:
                    batch_size = 200
                    updated_parsed = 0
                    for i in range(0, len(parsed_to_update), batch_size):
                        batch = parsed_to_update[i:i + batch_size]
                        async with conn.transaction():
                            for p_id, new_json in batch:
                                await conn.execute(
                                    "UPDATE parsed_records SET data = $1::jsonb, updated_at = NOW() WHERE id = $2",
                                    new_json,
                                    p_id,
                                )
                        updated_parsed += len(batch)
                        print(f"  • Updated {updated_parsed}/{len(parsed_to_update)} parsed_records...")
                    print(f"✅ Successfully updated {updated_parsed} parsed_records.")

        # =====================================================================
        # Step 2: Migrate scrubbed_records (precedents_cited -> structured)
        # =====================================================================
        if not only_parsed:
            print("\n" + "=" * 50)
            print("📦 STEP 2: Migrating scrubbed_records (precedents_cited structure)")
            print("=" * 50)

            scrub_params = []
            scrub_target_clause = ""
            if target_name:
                scrub_params.append(target_name)
                scrub_target_clause = f"AND t.target_name = ${len(scrub_params)}"

            scrub_limit_clause = f"LIMIT {limit}" if limit else ""

            scrub_query = f"""
                SELECT s.id AS scrubbed_id,
                       s.extracted_record_id,
                       s.data AS scrubbed_data,
                       e.source_url,
                       COALESCE(t.target_name, 'Unknown') AS target_name
                FROM scrubbed_records s
                JOIN extracted_records e ON s.extracted_record_id = e.id
                LEFT JOIN targets t ON e.target_id = t.id
                WHERE (
                    s.data->'extracted_data' ? 'precedents_cited'
                    OR s.data ? 'precedents_cited'
                )
                {scrub_target_clause}
                ORDER BY s.created_at ASC
                {scrub_limit_clause}
            """

            scrub_rows = await conn.fetch(scrub_query, *scrub_params)
            print(f"Found {len(scrub_rows)} candidate scrubbed_records.")

            scrub_to_update = []
            precedents_migrated = 0
            sample_diffs = []

            for row in scrub_rows:
                s_id = row["scrubbed_id"]
                target = row["target_name"]
                raw_data = row["scrubbed_data"]

                if isinstance(raw_data, str):
                    try:
                        data = json.loads(raw_data)
                    except Exception:
                        continue
                elif isinstance(raw_data, dict):
                    data = copy.deepcopy(raw_data)
                else:
                    continue

                modified = False
                ext_data = data.get("extracted_data") if isinstance(data.get("extracted_data"), dict) else data

                if isinstance(ext_data, dict) and "precedents_cited" in ext_data:
                    raw_precedents = ext_data.get("precedents_cited")
                    if isinstance(raw_precedents, list):
                        new_precedents = []
                        for p in raw_precedents:
                            migrated = migrate_precedent_entry(p)
                            new_precedents.append(migrated)
                            precedents_migrated += 1
                        
                        if new_precedents != raw_precedents:
                            ext_data["precedents_cited"] = new_precedents
                            modified = True
                            if len(sample_diffs) < 5 and raw_precedents:
                                sample_diffs.append({
                                    "target": target,
                                    "old_sample": raw_precedents[0],
                                    "new_sample": new_precedents[0] if new_precedents else None,
                                })

                # Migrate metadata citation -> neutral_citation if needed
                meta = data.get("metadata")
                if isinstance(meta, dict) and "citation" in meta:
                    cit_val = meta.pop("citation")
                    if "neutral_citation" not in meta:
                        meta["neutral_citation"] = cit_val
                    modified = True

                if "citation" in data:
                    cit_val = data.pop("citation")
                    if "neutral_citation" not in data:
                        data["neutral_citation"] = cit_val
                    modified = True

                if modified:
                    scrub_to_update.append((s_id, json.dumps(data, ensure_ascii=False)))

            print(f"Identified {len(scrub_to_update)} scrubbed_records requiring updates.")
            print(f"Total precedent / footnote citation entries processed: {precedents_migrated}")

            if sample_diffs:
                print("\n--- SAMPLE MIGRATION DIFFS ---")
                for idx, diff in enumerate(sample_diffs, 1):
                    print(f"\nSample {idx} ({diff['target']}):")
                    print("  [OLD]:", json.dumps(diff["old_sample"], indent=4))
                    print("  [NEW]:", json.dumps(diff["new_sample"], indent=4))

            if scrub_to_update and not dry_run:
                if not force:
                    confirm = input(f"\nUpdate {len(scrub_to_update)} scrubbed_records in DB? [y/N]: ").strip().lower()
                    if confirm not in ("y", "yes"):
                        print("Aborted scrubbed_records update.")
                        return

                batch_size = 200
                updated_scrubbed = 0
                for i in range(0, len(scrub_to_update), batch_size):
                    batch = scrub_to_update[i:i + batch_size]
                    async with conn.transaction():
                        for s_id, new_json in batch:
                            await conn.execute(
                                "UPDATE scrubbed_records SET data = $1::jsonb, updated_at = NOW() WHERE id = $2",
                                new_json,
                                s_id,
                            )
                    updated_scrubbed += len(batch)
                    print(f"  • Updated {updated_scrubbed}/{len(scrub_to_update)} scrubbed_records...")
                print(f"✅ Successfully updated {updated_scrubbed} scrubbed_records.")

    finally:
        await conn.close()
        print("\n🏁 Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Retroactively migrate parsed_records and scrubbed_records to structured footnotes schema."
    )
    parser.add_argument("--target", "-t", type=str, default=None, help="Target court name filter (e.g. ZACC, ZASCA)")
    parser.add_argument("--dry-run", action="store_true", help="Preview migrations without updating database")
    parser.add_argument("--force", "-f", action="store_true", help="Execute updates without interactive confirmation")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Max records to process")
    parser.add_argument("--only-parsed", action="store_true", help="Only process parsed_records")
    parser.add_argument("--only-scrubbed", action="store_true", help="Only process scrubbed_records")

    args = parser.parse_args()

    asyncio.run(
        fix_footnotes_records(
            target_name=args.target,
            dry_run=args.dry_run,
            force=args.force,
            limit=args.limit,
            only_parsed=args.only_parsed,
            only_scrubbed=args.only_scrubbed,
        )
    )


if __name__ == "__main__":
    main()
