"""
Unit tests for BookStack integration client and management command.
"""

from unittest.mock import MagicMock, patch
import pytest
from django.core.management import call_command

from extracted_data.models import BookStackImport, Entity, ExtractedRecord, ScrubbedRecord, Target
from pipelines.bookstack_client import BookStackClient, BookStackClientError
from pipelines.management.commands.import_to_bookstack import (
    extract_court_name,
    extract_page_title,
    format_html_content,
)


@pytest.mark.django_db
class TestBookStackImport:

    def test_extract_court_name(self):
        # Explicit court in data
        record_data = {"court": "Constitutional Court"}
        scrubbed = MagicMock()
        assert extract_court_name(scrubbed, record_data) == "Constitutional Court"

        # Fallback to target name
        record_data_empty = {}
        entity = Entity.objects.create(name="South African Judiciary")
        target = Target.objects.create(entity=entity, target_name="Supreme Court of Appeal")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="2026-01-01",
            record_type="saflii_courts",
            data={},
        )
        scrubbed_obj = ScrubbedRecord.objects.create(
            extracted_record=extracted,
            data=record_data_empty,
        )
        assert extract_court_name(scrubbed_obj, record_data_empty) == "Supreme Court of Appeal"

    def test_extract_page_title(self):
        data = {
            "case_number": "CCT 123/25",
            "applicant_plaintiff": "Alpha Corp",
            "respondent_defendant": ["Beta Ltd"],
        }
        scrubbed = MagicMock()
        title = extract_page_title(scrubbed, data)
        assert "[CCT 123/25]" in title
        assert "Alpha Corp v Beta Ltd" in title

    def test_format_html_content(self):
        entity = Entity.objects.create(name="High Court Test")
        target = Target.objects.create(entity=entity, target_name="Gauteng Local Division")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="2026-05-10",
            record_type="saflii_courts",
            source_url="https://saflii.org/cases/123",
            data={},
        )
        scrubbed = ScrubbedRecord.objects.create(
            extracted_record=extracted,
            data={
                "case_number": "1234/2026",
                "applicant_plaintiff": "John Doe",
                "respondent_defendant": ["Minister of Justice"],
                "ai_summary": "Test legal judgment summary.",
                "result": "Application granted with costs.",
            },
        )

        html_out = format_html_content(scrubbed, "Gauteng Local Division", scrubbed.data)
        assert "1234/2026" in html_out
        assert "Gauteng Local Division" in html_out
        assert "John Doe" in html_out
        assert "Application granted with costs." in html_out
        assert "detail_url" not in html_out
        assert "https://saflii.org/cases/123" not in html_out

    def test_format_html_gazettes_and_journals(self):
        entity = Entity.objects.create(name="Gazette Source")
        target = Target.objects.create(entity=entity, target_name="Government Gazette")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="2026-04-01",
            record_type="sabinet_gazettes",
            source_url="https://sabinet.co.za/gazette/50000",
            data={},
        )
        scrubbed = ScrubbedRecord.objects.create(
            extracted_record=extracted,
            data={
                "title": "National Environmental Management Act Notice",
                "gazette_number": "50000",
                "publisher": "Government Printer",
                "summary": "Notice regarding plastic waste management.",
                "keywords": ["Environment", "Regulations"],
                "detail_url": "https://example.com/detail/123",
            },
        )

        html_out = format_html_content(scrubbed, "Government Gazette", scrubbed.data)
        assert "Government Printer" in html_out
        assert "Notice regarding plastic waste management." in html_out
        assert "Environment" in html_out
        assert "detail_url" not in html_out

    def test_format_html_content_court_forum_sorting_and_type_removal(self):
        entity = Entity.objects.create(name="CCMA Source")
        target = Target.objects.create(entity=entity, target_name="CCMA")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="1996-11-01",
            record_type="sabinet_ccma",
            data={},
        )
        data = {
            "court": "CCMA",
            "forum": "CCMA",
            "employee": "Melikhaya [REDACTED]",
            "employer": "Quattro Protection Services (Pty) Ltd",
            "award_date": "1996-11-01",
            "hearing_end": "1996-11-27",
            "award_number": "WE54",
            "date_modified": "2019-10-28",
            "document_type": "CCMA Bargaining Council Awards",
            "hearing_start": "1996-11-27",
            "court_location": "Western Cape [Cape Town]"
        }
        scrubbed = ScrubbedRecord.objects.create(extracted_record=extracted, data=data)

        html_out = format_html_content(scrubbed, "CCMA", data)

        # 1. Check "Type:" is removed from top callout
        assert "<strong>Type:</strong>" not in html_out

        # 2. Check title styling is present
        assert ".page-title, h1.page-title, h1" in html_out

        # 3. Check Court/Forum consolidation
        assert "<strong>Court/Forum:</strong> CCMA" in html_out
        assert "<strong>Court:</strong>" not in html_out
        assert "<strong>Forum:</strong>" not in html_out

        # 4. Check alphabetical sorting of keys
        pos_award_date = html_out.find("<strong>Award Date:</strong>")
        pos_award_num = html_out.find("<strong>Award Number:</strong>")
        pos_court_loc = html_out.find("<strong>Court Location:</strong>")
        pos_court_forum = html_out.find("<strong>Court/Forum:</strong>")
        pos_date_mod = html_out.find("<strong>Date Modified:</strong>")
        pos_doc_type = html_out.find("<strong>Document Type:</strong>")
        pos_employee = html_out.find("<strong>Employee:</strong>")
        pos_employer = html_out.find("<strong>Employer:</strong>")

        assert pos_award_date < pos_award_num < pos_court_loc < pos_court_forum < pos_date_mod < pos_doc_type < pos_employee < pos_employer

    @patch("pipelines.bookstack_client.requests.Session.request")
    def test_bookstack_client_shelf_and_book(self, mock_request):
        # Mock shelf search (found)
        mock_resp_shelf_list = MagicMock()
        mock_resp_shelf_list.json.return_value = {"data": [{"id": 42, "name": "South African Legal Data"}]}
        mock_resp_shelf_get = MagicMock()
        mock_resp_shelf_get.json.return_value = {"id": 42, "name": "South African Legal Data", "books": []}

        # Mock book search & creation
        mock_resp_book_list = MagicMock()
        mock_resp_book_list.json.return_value = {"data": []}
        mock_resp_book_create = MagicMock()
        mock_resp_book_create.json.return_value = {"id": 101, "name": "Constitutional Court"}

        # Mock shelf put (attach book)
        mock_resp_shelf_put = MagicMock()
        mock_resp_shelf_put.json.return_value = {"id": 42, "name": "South African Legal Data", "books": [{"id": 101}]}

        mock_request.side_effect = [
            mock_resp_shelf_list,
            mock_resp_shelf_get,
            mock_resp_book_list,
            mock_resp_book_create,
            mock_resp_shelf_get,
            mock_resp_shelf_put,
        ]

        client = BookStackClient(base_url="http://mock-ohmbase", token_id="id", token_secret="sec")
        shelf = client.get_or_create_shelf("South African Legal Data")
        assert shelf["id"] == 42

        book = client.get_or_create_book("Constitutional Court", shelf_id=42)
        assert book["id"] == 101

    def test_import_command_dry_run(self):
        entity = Entity.objects.create(name="Test Entity")
        target = Target.objects.create(entity=entity, target_name="Test Target")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="2026-06-01",
            record_type="saflii_courts",
            data={},
        )
        scrubbed = ScrubbedRecord.objects.create(
            extracted_record=extracted,
            data={"court": "Labour Court", "case_number": "J100/26"},
        )

        call_command("import_to_bookstack", "--dry-run")
        assert BookStackImport.objects.filter(scrubbed_record=scrubbed).count() == 0

    def test_import_command_clear_first(self):
        entity = Entity.objects.create(name="Clear Test Entity")
        target = Target.objects.create(entity=entity, target_name="Clear Test Target")
        extracted = ExtractedRecord.objects.create(
            target=target,
            document_date="2026-06-01",
            record_type="saflii_courts",
            data={},
        )
        scrubbed = ScrubbedRecord.objects.create(
            extracted_record=extracted,
            data={"court": "Labour Court", "case_number": "J200/26"},
        )
        BookStackImport.objects.create(
            scrubbed_record=scrubbed,
            bookstack_page_id=99,
            bookstack_book_id=88,
            court_name="Labour Court",
        )
        assert BookStackImport.objects.count() == 1

        call_command("import_to_bookstack", "--clear-first", "--dry-run")
        assert BookStackImport.objects.count() == 0
