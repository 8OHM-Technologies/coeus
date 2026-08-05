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
        assert "[1234/2026] John Doe v Minister of Justice" in html_out
        assert "Gauteng Local Division" in html_out
        assert "John Doe" in html_out
        assert "Application granted with costs." in html_out

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
            },
        )

        html_out = format_html_content(scrubbed, "Government Gazette", scrubbed.data)
        assert "National Environmental Management Act Notice" in html_out
        assert "Government Printer" in html_out
        assert "Notice regarding plastic waste management." in html_out
        assert "Environment" in html_out

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
