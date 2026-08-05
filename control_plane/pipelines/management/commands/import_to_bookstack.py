"""
Django management command to read unimported scrubbed records from coeus.scrubbed_records
and import them into BookStack under a single shelf ('South African Legal Data'),
with separate books for each court.

Usage:
    python manage.py import_to_bookstack
    python manage.py import_to_bookstack --batch-size 50
    python manage.py import_to_bookstack --dry-run
"""

import json
import logging
from typing import Any, Dict, List, Optional

from django.core.management.base import BaseCommand
from django.db import transaction

from extracted_data.models import BookStackImport, ScrubbedRecord
from pipelines.bookstack_client import BookStackClient, BookStackClientError

logger = logging.getLogger(__name__)

DEFAULT_SHELF_NAME = "South African Legal Data"


def format_markdown_content(
    scrubbed_record: ScrubbedRecord,
    court_name: str,
    data: Dict[str, Any],
) -> str:
    """Format scrubbed record JSON payload into clean markdown for BookStack."""
    extracted = scrubbed_record.extracted_record
    case_number = data.get("case_number") or data.get("metadata", {}).get("case_number") or "N/A"
    judgment_date = (
        data.get("judgment_date")
        or data.get("metadata", {}).get("document_date")
        or str(extracted.document_date)
    )
    record_type = extracted.record_type
    source_url = extracted.source_url or "N/A"
    applicant = data.get("applicant_plaintiff") or "N/A"
    respondent = data.get("respondent_defendant")
    if isinstance(respondent, list):
        respondent_str = ", ".join(str(r) for r in respondent)
    else:
        respondent_str = str(respondent) if respondent else "N/A"

    ai_summary = data.get("ai_summary") or data.get("summary") or "No summary available."
    result = data.get("result") or "N/A"
    judges = data.get("judges")
    judges_str = ", ".join(str(j) for j in judges) if isinstance(judges, list) else (str(judges) if judges else "N/A")

    markdown = f"""# Case Record: {case_number}

| Metadata Field | Detail |
| :--- | :--- |
| **Court** | {court_name} |
| **Case Reference** | `{case_number}` |
| **Judgment Date** | {judgment_date} |
| **Record Type** | {record_type} |
| **Source URL** | [{source_url}]({source_url}) |

## 👥 Parties & Presiding Officers
- **Applicant / Plaintiff**: {applicant}
- **Respondent / Defendant**: {respondent_str}
- **Presiding Judge(s)**: {judges_str}

## ⚖️ Rulings & Result
**Result / Order**: {result}

## 📝 Summary & Headnotes
{ai_summary}

## 🔍 Scrubbed Payload Data
```json
{json.dumps(data, indent=2, default=str)}
```
"""
    return markdown


def extract_court_name(scrubbed_record: ScrubbedRecord, data: Dict[str, Any]) -> str:
    """Derive court name from data payload or associated target/entity."""
    court = data.get("court") or data.get("metadata", {}).get("court")
    if court and isinstance(court, str) and court.strip():
        return court.strip()

    # Fallback to target or entity name
    extracted = scrubbed_record.extracted_record
    if extracted and extracted.target:
        if extracted.target.target_name and extracted.target.target_name != "Default Target":
            return extracted.target.target_name.strip()
        if extracted.target.entity and extracted.target.entity.name:
            return extracted.target.entity.name.strip()

    return "Unspecified Court"


def extract_page_title(scrubbed_record: ScrubbedRecord, data: Dict[str, Any]) -> str:
    """Derive page title for BookStack."""
    case_number = data.get("case_number")
    applicant = data.get("applicant_plaintiff")
    respondent = data.get("respondent_defendant")

    if case_number and applicant:
        resp_str = respondent[0] if isinstance(respondent, list) and respondent else (respondent or "")
        title = f"[{case_number}] {applicant}"
        if resp_str:
            title += f" v {resp_str}"
        return title[:240]

    if case_number:
        return f"Case Reference: {case_number}"[:240]

    if applicant:
        return f"Matter: {applicant}"[:240]

    return f"Scrubbed Record {scrubbed_record.id}"[:240]


class Command(BaseCommand):
    help = (
        "Import scrubbed records from coeus.scrubbed_records into BookStack "
        "under a single shelf ('South African Legal Data'), grouped into court books."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of records to process per run (default: 100).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate import process without writing to BookStack API or DB.",
        )
        parser.add_argument(
            "--shelf-name",
            type=str,
            default=DEFAULT_SHELF_NAME,
            help=f"Target shelf name in BookStack (default: '{DEFAULT_SHELF_NAME}').",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional maximum number of unimported records to process.",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        shelf_name = options["shelf_name"]
        limit = options["limit"]

        self.stdout.write(self.style.NOTICE(f"Starting BookStack import (shelf: '{shelf_name}', dry_run: {dry_run})..."))

        # Query unimported scrubbed records using O(1) indexed LEFT JOIN check
        queryset = (
            ScrubbedRecord.objects.select_related(
                "extracted_record",
                "extracted_record__target",
                "extracted_record__target__entity",
            )
            .filter(bookstack_import__isnull=True)
            .order_by("created_at")
        )

        total_unimported = queryset.count()
        self.stdout.write(self.style.NOTICE(f"Found {total_unimported} unimported scrubbed records."))

        if total_unimported == 0:
            self.stdout.write(self.style.SUCCESS("All scrubbed records are already imported into BookStack."))
            return

        to_process = list(queryset[: limit if limit else batch_size])
        self.stdout.write(self.style.NOTICE(f"Processing batch of {len(to_process)} records..."))

        if dry_run:
            client = None
            shelf_id = 999
            book_cache: Dict[str, int] = {}
        else:
            client = BookStackClient()
            try:
                shelf_obj = client.get_or_create_shelf(shelf_name, "Shelf containing South African court judgments and legal records.")
                shelf_id = shelf_obj["id"]
                self.stdout.write(self.style.SUCCESS(f"Connected to BookStack Shelf '{shelf_name}' (ID: {shelf_id})."))
            except BookStackClientError as exc:
                self.stderr.write(self.style.ERROR(f"Failed to connect to BookStack API: {exc}"))
                return
            book_cache = {}

        imported_count = 0
        error_count = 0

        for record in to_process:
            try:
                data = record.data if isinstance(record.data, dict) else json.loads(record.data or "{}")
                court_name = extract_court_name(record, data)
                page_title = extract_page_title(record, data)
                markdown_content = format_markdown_content(record, court_name, data)

                tags = [
                    {"name": "coeus_scrubbed_id", "value": str(record.id)},
                    {"name": "court", "value": court_name},
                    {"name": "record_type", "value": record.extracted_record.record_type},
                ]
                case_no = data.get("case_number")
                if case_no:
                    tags.append({"name": "case_number", "value": str(case_no)})

                if dry_run:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [DRY RUN] Would import Record #{record.id} -> Book '{court_name}' | Title: '{page_title}'"
                        )
                    )
                    imported_count += 1
                    continue

                # Get or create Book for court
                if court_name not in book_cache:
                    book_obj = client.get_or_create_book(court_name, shelf_id)
                    book_cache[court_name] = book_obj["id"]

                book_id = book_cache[court_name]

                # Create Page in BookStack
                page_obj = client.create_page(
                    book_id=book_id,
                    name=page_title,
                    markdown=markdown_content,
                    tags=tags,
                )
                page_id = page_obj["id"]

                # Record successful import in DB mapping table
                with transaction.atomic():
                    BookStackImport.objects.create(
                        scrubbed_record=record,
                        bookstack_page_id=page_id,
                        bookstack_book_id=book_id,
                        court_name=court_name,
                    )

                imported_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  [OK] Imported Record {record.id} -> Court '{court_name}' (Book #{book_id}, Page #{page_id})"
                    )
                )

            except Exception as exc:
                error_count += 1
                self.stderr.write(self.style.ERROR(f"  [ERROR] Failed to import Record {record.id}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished import run! Successfully imported: {imported_count}, Errors: {error_count}, Remaining: {total_unimported - imported_count}"
            )
        )
