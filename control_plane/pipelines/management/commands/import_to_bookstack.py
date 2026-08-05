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
    Format scrubbed record JSON payload into formal, elegant HTML for BookStack,
    styled like official legal archives, law reports, and public records.
    """
    extracted = scrubbed_record.extracted_record
    raw_title = extract_page_title(scrubbed_record, data)
    title_esc = html.escape(raw_title)

    doc_date = (
        data.get("judgment_date")
        or data.get("publication_date")
        or data.get("date")
        or data.get("metadata", {}).get("document_date")
        or (str(extracted.document_date) if extracted and extracted.document_date else "N/A")
    )
    doc_date_esc = html.escape(str(doc_date))

    record_type_str = (extracted.record_type.replace("_", " ").title() if extracted and extracted.record_type else "Legal Record")
    record_type_esc = html.escape(record_type_str)

    source_url = extracted.source_url if extracted and extracted.source_url else None
    ref_number = (
        data.get("case_number")
        or data.get("gazette_number")
        or data.get("notice_number")
        or data.get("volume")
        or "N/A"
    )
    ref_number_esc = html.escape(str(ref_number))
    court_esc = html.escape(court_name)

    ai_summary = (
        data.get("ai_summary")
        or data.get("summary")
        or data.get("abstract")
        or "No formal headnote or summary recorded."
    )
    ai_summary_esc = html.escape(str(ai_summary)).replace("\n", "<br>")

    # Build HTML sections
    html_parts = []

    # Container & Header Banner
    html_parts.append(
        '<div style="font-family: \'Georgia\', \'Times New Roman\', serif; color: #1a202c; max-width: 900px; margin: 0 auto; line-height: 1.6;">'
    )

    # Document Header Banner
    html_parts.append(
        '<div style="border-bottom: 3px double #2b6cb0; padding-bottom: 12px; margin-bottom: 24px; text-align: center;">'
        '<span style="font-family: sans-serif; font-size: 11px; font-weight: 700; text-transform: uppercase; tracking: 1.5px; color: #4a5568; background-color: #edf2f7; padding: 4px 10px; border-radius: 4px; display: inline-block; margin-bottom: 8px;">'
        'South African Legal Archive & Law Reports</span>'
        f'<h1 style="font-size: 24px; font-weight: 700; color: #1a202c; margin: 8px 0 4px 0;">{title_esc}</h1>'
        '</div>'
    )

    # Metadata Grid Table
    html_parts.append(
        '<div style="background-color: #f7fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 16px; margin-bottom: 24px;">'
        '<table style="width: 100%; border-collapse: collapse; font-family: sans-serif; font-size: 13px;">'
        '<tbody>'
        '<tr>'
        '<td style="width: 25%; font-weight: 700; color: #4a5568; padding: 6px 0;">Jurisdiction / Forum:</td>'
        f'<td style="color: #2d3748; padding: 6px 0;"><strong>{court_esc}</strong></td>'
        '</tr>'
        '<tr>'
        '<td style="font-weight: 700; color: #4a5568; padding: 6px 0;">Official Reference:</td>'
        f'<td style="color: #2d3748; padding: 6px 0;"><code style="background: #edf2f7; padding: 2px 6px; border-radius: 3px; font-family: monospace;">{ref_number_esc}</code></td>'
        '</tr>'
        '<tr>'
        '<td style="font-weight: 700; color: #4a5568; padding: 6px 0;">Date Delivered / Published:</td>'
        f'<td style="color: #2d3748; padding: 6px 0;">{doc_date_esc}</td>'
        '</tr>'
        '<tr>'
        '<td style="font-weight: 700; color: #4a5568; padding: 6px 0;">Classification:</td>'
        f'<td style="color: #2d3748; padding: 6px 0;">{record_type_esc}</td>'
        '</tr>'
    )
    if source_url:
        source_esc = html.escape(source_url)
        html_parts.append(
            '<tr>'
            '<td style="font-weight: 700; color: #4a5568; padding: 6px 0;">Source Record:</td>'
            f'<td style="padding: 6px 0;"><a href="{source_esc}" target="_blank" style="color: #2b6cb0; text-decoration: none;">View Original Document ↗</a></td>'
            '</tr>'
        )
    html_parts.append('tbody></table></div>')

    # Parties & Court Rulings (Case Law)
    applicant = data.get("applicant_plaintiff")
    respondent = data.get("respondent_defendant")
    judges = data.get("judges")
    result = data.get("result")

    if applicant or respondent or judges or result:
        html_parts.append(
            '<div style="margin-bottom: 24px;">'
            '<h2 style="font-family: sans-serif; font-size: 16px; color: #2b6cb0; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-bottom: 12px;">⚖️ Parties & Court Bench</h2>'
            '<ul style="list-style: none; padding-left: 0; font-size: 14px;">'
        )
        if applicant:
            html_parts.append(f'<li style="margin-bottom: 6px;"><strong>Applicant / Plaintiff:</strong> {html.escape(str(applicant))}</li>')
        if respondent:
            resp_str = ", ".join(str(r) for r in respondent) if isinstance(respondent, list) else str(respondent)
            html_parts.append(f'<li style="margin-bottom: 6px;"><strong>Respondent / Defendant:</strong> {html.escape(resp_str)}</li>')
        if judges:
            judges_str = ", ".join(str(j) for j in judges) if isinstance(judges, list) else str(judges)
            html_parts.append(f'<li style="margin-bottom: 6px;"><strong>Presiding Judge(s):</strong> {html.escape(judges_str)}</li>')
        html_parts.append('</ul>')

        if result:
            html_parts.append(
                '<div style="background-color: #ebf8ff; border-left: 4px solid #3182ce; padding: 12px 16px; margin-top: 12px; border-radius: 0 4px 4px 0;">'
                '<strong style="font-family: sans-serif; font-size: 13px; color: #2b6cb0; text-transform: uppercase; display: block; margin-bottom: 4px;">Holding & Final Order:</strong>'
                f'<span style="font-size: 14px; color: #2d3748;">{html.escape(str(result))}</span>'
                '</div>'
            )
        html_parts.append('</div>')

    # Publication Details (Gazettes / Journals)
    publisher = data.get("publisher") or data.get("journal_name")
    authors = data.get("author") or data.get("authors")
    subjects = data.get("subjects") or data.get("keywords") or data.get("ai_keywords")

    if publisher or authors or subjects:
        html_parts.append(
            '<div style="margin-bottom: 24px;">'
            '<h2 style="font-family: sans-serif; font-size: 16px; color: #2b6cb0; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-bottom: 12px;">📚 Publication & Notice Info</h2>'
            '<ul style="list-style: none; padding-left: 0; font-size: 14px;">'
        )
        if publisher:
            html_parts.append(f'<li style="margin-bottom: 6px;"><strong>Publisher / Periodical:</strong> {html.escape(str(publisher))}</li>')
        if authors:
            auth_str = ", ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors)
            html_parts.append(f'<li style="margin-bottom: 6px;"><strong>Author(s):</strong> {html.escape(auth_str)}</li>')
        if subjects:
            subj_list = subjects if isinstance(subjects, list) else [str(subjects)]
            badges_html = " ".join(
                f'<span style="background: #edf2f7; color: #4a5568; font-family: sans-serif; font-size: 12px; padding: 3px 8px; border-radius: 12px; display: inline-block; margin-right: 4px;">{html.escape(str(s))}</span>'
                for s in subj_list
            )
            html_parts.append(f'<li style="margin-top: 8px;"><strong>Subjects & Indexing:</strong><br><div style="margin-top: 6px;">{badges_html}</div></li>')
        html_parts.append('</ul></div>')

    # Formal Headnotes & Case Summary Block
    html_parts.append(
        '<div style="margin-bottom: 28px;">'
        '<h2 style="font-family: sans-serif; font-size: 16px; color: #2b6cb0; border-bottom: 1px solid #e2e8f0; padding-bottom: 4px; margin-bottom: 12px;">📖 Headnotes & Summary</h2>'
        '<blockquote style="margin: 0; padding: 16px 20px; background-color: #fffaf0; border-left: 4px solid #dd6b20; font-style: italic; font-size: 14.5px; color: #2d3748; line-height: 1.7; border-radius: 0 4px 4px 0;">'
        f'{ai_summary_esc}'
        '</blockquote>'
        '</div>'
    )

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
