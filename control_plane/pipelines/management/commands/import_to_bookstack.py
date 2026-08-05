"""
Django management command to read unimported scrubbed records from coeus.scrubbed_records
and import them into BookStack as HTML pages under a single shelf ('South African Legal Data'),
grouped into court books.

Usage:
    python manage.py import_to_bookstack
    python manage.py import_to_bookstack --batch-size 50
    python manage.py import_to_bookstack --clear-first
    python manage.py import_to_bookstack --dry-run
"""

import html
import json
import logging
from typing import Any, Dict, List, Optional

from django.core.management.base import BaseCommand
from django.db import transaction

from extracted_data.models import BookStackImport, ScrubbedRecord
from pipelines.bookstack_client import BookStackClient, BookStackClientError

logger = logging.getLogger(__name__)

DEFAULT_SHELF_NAME = "South African Legal Data"


def extract_court_name(scrubbed_record: ScrubbedRecord, data: Dict[str, Any]) -> str:
    """Derive book/category name from court, publisher, journal, or target/entity."""
    court = (
        data.get("court")
        or data.get("metadata", {}).get("court")
        or data.get("publisher")
        or data.get("journal_name")
    )
    if court and isinstance(court, str) and court.strip():
        return court.strip()

    # Fallback to target or entity name
    extracted = scrubbed_record.extracted_record
    if extracted and extracted.target:
        if extracted.target.target_name and extracted.target.target_name != "Default Target":
            return extracted.target.target_name.strip()
        if extracted.target.entity and extracted.target.entity.name:
            return extracted.target.entity.name.strip()

    return "General Publications"


def extract_page_title(scrubbed_record: ScrubbedRecord, data: Dict[str, Any]) -> str:
    """Derive a clean page title for any document type (Cases, Gazettes, Journals, Court Rolls)."""
    # Check explicit title / name field
    title_field = data.get("title") or data.get("name") or data.get("heading")
    if title_field and isinstance(title_field, str) and title_field.strip():
        return title_field.strip()[:240]

    case_number = data.get("case_number") or data.get("metadata", {}).get("case_number")
    applicant = data.get("applicant_plaintiff")
    respondent = data.get("respondent_defendant")

    # Case law title format
    if case_number and applicant:
        resp_str = respondent[0] if isinstance(respondent, list) and respondent else (respondent or "")
        title = f"[{case_number}] {applicant}"
        if resp_str:
            title += f" v {resp_str}"
        return title[:240]

    if case_number:
        return f"Case Reference: {case_number}"[:240]

    # Gazette format
    gazette_no = data.get("gazette_number") or data.get("notice_number")
    if gazette_no:
        return f"Gazette Notice #{gazette_no}"[:240]

    # Journal format
    journal_name = data.get("journal_name")
    if journal_name and applicant:
        return f"{journal_name}: {applicant}"[:240]

    if applicant:
        return f"Matter: {applicant}"[:240]

    # Fallback to record type & ID
    extracted = scrubbed_record.extracted_record
    rec_type = extracted.record_type.replace("_", " ").title() if extracted else "Record"
    return f"{rec_type} ({scrubbed_record.id})"[:240]


def format_html_content(
    scrubbed_record: ScrubbedRecord,
    court_name: str,
    data: Dict[str, Any],
) -> str:
    """
    Format scrubbed record JSON payload into clean BookStack HTML:
    - Wrapped in .page-content (no duplicate <h1> title, as BookStack renders title natively).
    - Only hardcodes exclusion of technical scraper fields (detail_url, detail_title, index_scraped_at, preview_image_url, details_scraped_at, source_url, metadata).
    - Dynamically populates all payload fields for any dataset (CCMA, High Court, Labour Court, Gazettes, etc.).
    """
    extracted = scrubbed_record.extracted_record

    # Set of technical/scraper fields to ALWAYS exclude from view
    EXCLUDED_KEYS = {
        "detail_url",
        "detail_title",
        "index_scraped_at",
        "preview_image_url",
        "details_scraped_at",
        "scraped_at",
        "source_url",
        "metadata",
    }

    record_type_str = (extracted.record_type if extracted and extracted.record_type else "")
    doc_date = (
        data.get("judgment_date")
        or data.get("publication_date")
        or data.get("Award Date")
        or data.get("date")
        or data.get("metadata", {}).get("document_date")
        or (str(extracted.document_date) if extracted and extracted.document_date else "N/A")
    )
    doc_date_esc = html.escape(str(doc_date))
    rec_type_display = (data.get("Document Type") or record_type_str.replace("_", " ").title() or "Legal Document")
    rec_type_esc = html.escape(str(rec_type_display))
    court_esc = html.escape(court_name)

    ref_number = (
        data.get("case_number")
        or data.get("gazette_number")
        or data.get("notice_number")
        or data.get("Award Number")
        or data.get("award_number")
        or data.get("volume")
    )
    ref_esc = html.escape(str(ref_number)) if ref_number else None

    html_parts = ['<div class="page-content">']

    # -------------------------------------------------------------------------
    # 1. Aligned Header Info Callout
    # -------------------------------------------------------------------------
    html_parts.append('<div class="callout info"><p style="margin: 0; line-height: 1.6;">')
    html_parts.append(f'<strong>Forum / Source:</strong> {court_esc}')
    if ref_esc:
        html_parts.append(f' &nbsp;|&nbsp; <strong>Reference:</strong> <code>{ref_esc}</code>')
    html_parts.append(f' &nbsp;|&nbsp; <strong>Date:</strong> {doc_date_esc} &nbsp;|&nbsp; <strong>Type:</strong> {rec_type_esc}')
    html_parts.append('</p></div>')

    # -------------------------------------------------------------------------
    # 2. Key Highlights Callout Boxes (Holding, Reason for Dismissal, Summary)
    # -------------------------------------------------------------------------
    result = data.get("result") or data.get("order") or data.get("holding")
    if result:
        html_parts.append(
            '<div class="callout success">'
            f'<p style="margin: 0;"><strong>Holding & Final Order:</strong> {html.escape(str(result))}</p>'
            '</div>'
        )

    reason_dismissal = (
        data.get("reason_for_dismissal")
        or data.get("dismissal_reason")
        or data.get("reasons_for_dismissal")
    )
    if reason_dismissal:
        html_parts.append(
            '<div class="callout danger">'
            f'<p style="margin: 0;"><strong>Reason for Dismissal:</strong> {html.escape(str(reason_dismissal))}</p>'
            '</div>'
        )

    ai_summary = (
        data.get("ai_summary")
        or data.get("summary")
        or data.get("headnotes")
        or data.get("abstract")
    )
    if ai_summary:
        ai_summary_esc = html.escape(str(ai_summary)).replace("\n", "<br>")
        html_parts.append(
            '<h2>📖 Summary & Headnotes</h2>'
            '<div class="callout warning">'
            f'<p style="margin: 0;">{ai_summary_esc}</p>'
            '</div>'
        )

    # -------------------------------------------------------------------------
    # 3. Document Body / Text Content (when present)
    # -------------------------------------------------------------------------
    body_text = (
        data.get("full_text")
        or data.get("text")
        or data.get("content")
        or data.get("raw_text")
        or data.get("judgment_text")
    )
    if body_text:
        text_formatted = html.escape(str(body_text)).replace("\n\n", "</p><p>").replace("\n", "<br>")
        html_parts.append('<h2>📄 Document Text</h2>')
        html_parts.append(f'<div style="font-size: 14.5px; line-height: 1.7;"><p>{text_formatted}</p></div>')

    # -------------------------------------------------------------------------
    # 4. Dynamic Field Renderer for All Remaining Payload Metadata
    # -------------------------------------------------------------------------
    handled_keys = EXCLUDED_KEYS.union({
        "result", "order", "holding",
        "reason_for_dismissal", "dismissal_reason", "reasons_for_dismissal",
        "summary", "ai_summary", "headnotes", "abstract",
        "full_text", "text", "content", "raw_text", "judgment_text",
        "title", "name", "heading",
    })
    remaining_fields = {k: v for k, v in data.items() if k not in handled_keys and v is not None and v != ""}

    if remaining_fields:
        html_parts.append('<h2>📋 Document Details</h2><ul>')
        for k, v in remaining_fields.items():
            k_label = k.replace("_", " ").title()
            if isinstance(v, list):
                v_str = ", ".join(str(item) for item in v)
            elif isinstance(v, dict):
                v_str = json.dumps(v, indent=2)
            else:
                v_str = str(v)
            html_parts.append(f'<li><strong>{html.escape(k_label)}:</strong> {html.escape(v_str)}</li>')
        html_parts.append('</ul>')

    html_parts.append('</div>')
    return "".join(html_parts)






class Command(BaseCommand):
    help = (
        "Import scrubbed records from coeus.scrubbed_records into BookStack "
        "as styled HTML pages under a single shelf ('South African Legal Data'), grouped into court books."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of records to process per run (default: 100).",
        )
        parser.add_argument(
            "--clear-first",
            action="store_true",
            help="If specified, clears all existing BookStack pages/books and tracking entries before importing.",
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
        clear_first = options["clear_first"]
        dry_run = options["dry_run"]
        shelf_name = options["shelf_name"]
        limit = options["limit"]

        self.stdout.write(self.style.NOTICE(f"Starting BookStack HTML import (shelf: '{shelf_name}', clear_first: {clear_first}, dry_run: {dry_run})..."))

        if dry_run:
            client = None
        else:
            client = BookStackClient()

        if clear_first:
            self.stdout.write(self.style.WARNING("Option --clear-first specified. Clearing local import records and BookStack tables..."))
            if not dry_run and client:
                try:
                    client.clear_all_shelves_and_books()
                    self.stdout.write(self.style.SUCCESS("Cleared BookStack shelves and books via API."))
                except Exception as exc:
                    self.stderr.write(self.style.ERROR(f"Error clearing BookStack: {exc}"))

            with transaction.atomic():
                deleted_count, _ = BookStackImport.objects.all().delete()
                self.stdout.write(self.style.SUCCESS(f"Cleared {deleted_count} local BookStackImport tracking records."))

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
            shelf_id = 999
            book_cache: Dict[str, int] = {}
        else:
            try:
                shelf_obj = client.get_or_create_shelf(shelf_name, "Shelf containing South African court judgments, gazettes, and legal records.")
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
                html_content = format_html_content(record, court_name, data)

                tags = [
                    {"name": "coeus_scrubbed_id", "value": str(record.id)},
                    {"name": "court", "value": court_name},
                    {"name": "record_type", "value": record.extracted_record.record_type if record.extracted_record else "unknown"},
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

                # Get or create Book for court / source
                if court_name not in book_cache:
                    book_obj = client.get_or_create_book(court_name, shelf_id)
                    book_cache[court_name] = book_obj["id"]

                book_id = book_cache[court_name]

                # Create HTML Page in BookStack
                page_obj = client.create_page(
                    book_id=book_id,
                    name=page_title,
                    html=html_content,
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
