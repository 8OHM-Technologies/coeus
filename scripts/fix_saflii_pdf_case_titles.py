#!/usr/bin/env python3
"""
Fix SAFLII PDF Case Titles & Document Dates Script
==================================================
Identifies case records whose source document is a PDF (where titles were originally
extracted from the PDF filename or internal PDF metadata), fetches the companion
SAFLII .html page (e.g., `.../20.html` instead of `.../20.pdf`) via an authenticated
SeleniumBase session, and updates:

1. `extracted_records.data->'title'` with the true case title extracted from `<h2>`.
2. `extracted_records.document_date` with the resolved date parsed from the title.
3. `scrubbed_records.data->'title'` with the true case title.
4. `scrubbed_records.data->'metadata'->>'document_date'` with the resolved date.
5. `scrubbed_records.data->'extracted_data'->>'judgment_date'` with the resolved date.

Usage:
    # Dry run preview (fetches HTML titles and displays diffs without saving):
    python scripts/fix_saflii_pdf_case_titles.py --dry-run --limit 10

    # Fix all PDF cases across all courts (interactive confirmation):
    python scripts/fix_saflii_pdf_case_titles.py

    # Fix without confirmation prompt:
    python scripts/fix_saflii_pdf_case_titles.py --force

    # Target specific court/tribunal (e.g. ZACT, ZACC, ZAGPJHC):
    python scripts/fix_saflii_pdf_case_titles.py --target ZACT --force
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
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
        import seleniumbase
    except ImportError:
        os.execv(venv_python, [venv_python] + sys.argv)

from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

from seleniumbase import SB
from extraction_workers.db import get_db_connection
from extraction_workers.new_saflii_scraper import extract_date_from_title


def fetch_companion_title_from_html(sb: SB, pdf_url: str) -> Optional[str]:
    """Fetch companion .html page via browser navigation to extract true <h2> title."""
    html_url = re.sub(r'\.pdf$', '.html', pdf_url, flags=re.IGNORECASE)
    try:
        sb.open(html_url)
        sb.sleep(1.2)
        soup = BeautifulSoup(sb.get_page_source(), "lxml")
        h2_el = soup.find("h2")
        if h2_el and h2_el.get_text(strip=True):
            txt = h2_el.get_text(strip=True)
            if not any(k in txt.lower() for k in ["verification", "moment", "security", "cloudflare", "saflii"]):
                return txt
        if soup.title and soup.title.get_text(strip=True):
            txt = soup.title.get_text(strip=True)
            if not any(k in txt.lower() for k in ["verification", "moment", "security", "cloudflare", "404", "saflii"]):
                return txt
    except Exception:
        pass
    return None


async def _async_fetch_records(target_name: str | None, limit: int | None) -> List[Dict[str, Any]]:
    conn = await get_db_connection()
    try:
        params = []
        target_clause = ""
        if target_name:
            params.append(target_name)
            target_clause = f"AND t.target_name = ${len(params)}"

        limit_clause = ""
        if limit:
            params.append(limit)
            limit_clause = f"LIMIT ${len(params)}"

        query = f"""
            SELECT e.id AS extracted_id,
                   e.source_url,
                   e.data AS extracted_data,
                   e.document_date,
                   COALESCE(t.target_name, 'Unknown') AS target_name,
                   s.id AS scrubbed_id,
                   s.data AS scrubbed_data
            FROM extracted_records e
            LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
            LEFT JOIN targets t ON e.target_id = t.id
            WHERE e.source_url LIKE '%.pdf'
              AND e.source_url LIKE '%/za/cases/%'
              AND e.source_url NOT LIKE '%/za/gaz/%'
              AND e.source_url NOT LIKE '%/za/journals/%'
              AND e.source_url NOT LIKE '%/za/other/%'
              {target_clause}
            ORDER BY t.target_name ASC, e.scraped_at ASC
            {limit_clause};
        """
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _async_apply_updates(to_update: List[Dict[str, Any]]) -> int:
    conn = await get_db_connection()
    try:
        updated_count = 0
        batch_size = 50
        for i in range(0, len(to_update), batch_size):
            chunk = to_update[i : i + batch_size]
            async with conn.transaction():
                for item in chunk:
                    # 1. Update extracted_records
                    if item["resolved_date"]:
                        await conn.execute(
                            """
                            UPDATE extracted_records
                            SET data = jsonb_set(data, '{title}', to_jsonb($1::text), true),
                                document_date = $2,
                                updated_at = NOW()
                            WHERE id = $3
                            """,
                            item["new_title"],
                            item["resolved_date"],
                            item["extracted_id"],
                        )
                    else:
                        await conn.execute(
                            """
                            UPDATE extracted_records
                            SET data = jsonb_set(data, '{title}', to_jsonb($1::text), true),
                                updated_at = NOW()
                            WHERE id = $2
                            """,
                            item["new_title"],
                            item["extracted_id"],
                        )

                    # 2. Update scrubbed_records if present
                    if item["has_scrubbed"]:
                        if item["resolved_iso"]:
                            await conn.execute(
                                """
                                UPDATE scrubbed_records
                                SET data = jsonb_set(
                                        jsonb_set(
                                            jsonb_set(
                                                data,
                                                '{title}',
                                                to_jsonb($1::text),
                                                true
                                            ),
                                            '{metadata,document_date}',
                                            to_jsonb($2::text),
                                            true
                                        ),
                                        '{extracted_data,judgment_date}',
                                        to_jsonb($2::text),
                                        true
                                    ),
                                    updated_at = NOW()
                                WHERE id = $3
                                """,
                                item["new_title"],
                                item["resolved_iso"],
                                item["scrubbed_id"],
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE scrubbed_records
                                SET data = jsonb_set(data, '{title}', to_jsonb($1::text), true),
                                    updated_at = NOW()
                                WHERE id = $2
                                """,
                                item["new_title"],
                                item["scrubbed_id"],
                            )

                    updated_count += 1
        return updated_count
    finally:
        await conn.close()


def run_fix_pdf_titles(
    target_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
):
    print("=" * 75)
    print("📑 SAFLII PDF CASE TITLES & DOCUMENT DATES NORMALIZER")
    print("=" * 75)
    print(f"Target Filter : {target_name or 'All Targets'}")
    print(f"Mode          : {'🔍 DRY RUN (No changes)' if dry_run else '⚡ EXECUTE'}")
    print(f"Record Limit  : {limit or 'No limit'}")
    print("-" * 75)

    print("⏳ Fetching candidate PDF case records from database...", flush=True)
    records = asyncio.run(_async_fetch_records(target_name, limit))
    total_scanned = len(records)

    if total_scanned == 0:
        print("⚠️  No matching PDF case records found.")
        return

    print(f"Found {total_scanned} candidate PDF case record(s). Starting SeleniumBase browser...", flush=True)

    to_update = []
    target_counts = Counter()

    with SB(
        uc=True,
        headless=True,
        test=True,
        xvfb=True,
        chromium_arg="--no-sandbox,--disable-dev-shm-usage",
    ) as sb:
        for i, rec in enumerate(records, start=1):
            pdf_url = rec["source_url"]
            e_data = json.loads(rec["extracted_data"]) if isinstance(rec["extracted_data"], str) else (rec["extracted_data"] or {})
            old_title = e_data.get("title")

            # Fetch true case title from companion HTML
            new_title = fetch_companion_title_from_html(sb, pdf_url)
            if not new_title or new_title == old_title:
                if i % 25 == 0 or i == total_scanned:
                    print(f"  Processed {i}/{total_scanned} records (Found {len(to_update)} to update)...", flush=True)
                time.sleep(0.15)
                continue

            resolved_date = extract_date_from_title(new_title)
            resolved_iso = resolved_date.isoformat() if resolved_date else None

            target_counts[rec["target_name"]] += 1
            to_update.append({
                "extracted_id": rec["extracted_id"],
                "scrubbed_id": rec["scrubbed_id"],
                "target_name": rec["target_name"],
                "source_url": pdf_url,
                "old_title": old_title,
                "new_title": new_title,
                "old_date": rec["document_date"],
                "resolved_date": resolved_date,
                "resolved_iso": resolved_iso,
                "has_scrubbed": rec["scrubbed_id"] is not None,
            })

            print(f"  [{i}/{total_scanned}] Found update: {new_title[:65]}...", flush=True)
            time.sleep(0.15)

    print("-" * 75)
    print(f"Scanned {total_scanned} PDF record(s). Found {len(to_update)} requiring title/date updates:")
    for tname, count in sorted(target_counts.items()):
        print(f"  • {tname:<15}: {count:>4} record(s)")
    print("-" * 75)

    if not to_update:
        print("✅ All PDF case records already possess standardized titles! No updates needed.")
        return

    # Display preview for sample records
    sample_size = min(5, len(to_update))
    print(f"\n🔍 Sample Previews (First {sample_size} records):")
    for i, item in enumerate(to_update[:sample_size], start=1):
        print(f"\n[{i}] Record ID : {item['extracted_id']} ({item['target_name']})")
        print(f"    URL       : {item['source_url']}")
        print(f"    OLD TITLE : {item['old_title']}")
        print(f"    NEW TITLE : {item['new_title']}")
        if item["resolved_date"]:
            print(f"    DATE      : {item['old_date']}  ->  {item['resolved_iso']}")

    print("=" * 75)

    if dry_run:
        print("🔍 DRY RUN COMPLETE — No database modifications were made.")
        return

    if not force:
        confirm = input(f"\nAre you sure you want to update {len(to_update)} record(s) across database tables? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("❌ Operation cancelled by user.")
            return

    print(f"\n⏳ Updating {len(to_update)} record(s) in database...", flush=True)
    updated_count = asyncio.run(_async_apply_updates(to_update))
    print(f"✅ Successfully updated {updated_count} PDF case record(s) across database tables.")


def main():
    parser = argparse.ArgumentParser(
        description="Fix SAFLII PDF case titles and document dates by fetching companion .html pages."
    )
    parser.add_argument(
        "--target",
        "-t",
        type=str,
        default=None,
        help="Filter by target name (e.g. ZACT, ZACC, ZAGPJHC). Default: all targets.",
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
        help="Limit number of candidate records to process.",
    )

    args = parser.parse_args()
    run_fix_pdf_titles(
        target_name=args.target,
        dry_run=args.dry_run,
        force=args.force,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
