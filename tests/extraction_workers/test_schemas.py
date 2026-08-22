import sys
import os
from datetime import date
import pytest
from pydantic import ValidationError

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from schemas.generic import DataQualityFlags, BaseExtractedRecord, GenericDocumentExtraction
from schemas.saflii import (
    PrecedentCategory,
    SafliiHeaderData,
    SafliiPrecedentsData,
    SafliiBodyData,
    SafliiExtractedData,
    SafliiCaseExtraction,
    SafliiJournalGazetteExtraction,
    SafliiCourtRollRow,
    SafliiCourtRollExtraction,
)
import schemas


def test_package_exports():
    """Verify that extraction_workers.schemas re-exports all generic and SAFLII schemas."""
    assert hasattr(schemas, "DataQualityFlags")
    assert hasattr(schemas, "BaseExtractedRecord")
    assert hasattr(schemas, "GenericDocumentExtraction")
    assert hasattr(schemas, "SafliiCaseExtraction")
    assert hasattr(schemas, "SafliiJournalGazetteExtraction")
    assert hasattr(schemas, "SafliiCourtRollExtraction")


def test_data_quality_flags_validation():
    # Valid model
    flags = DataQualityFlags(requires_human_review=True, review_reason="Missing fields")
    assert flags.requires_human_review is True
    assert flags.review_reason == "Missing fields"

    # Default value for review_reason should be None
    flags_default = DataQualityFlags(requires_human_review=False)
    assert flags_default.requires_human_review is False
    assert flags_default.review_reason is None

    # Missing required fields should fail
    with pytest.raises(ValidationError):
        DataQualityFlags()  # type: ignore


def test_base_extracted_record_validation():
    # Valid record
    record = BaseExtractedRecord(
        entity_name="Test Entity",
        target_name="Test Target",
        document_date="2026-07-06",
        record_type="Financials",
    )
    assert record.entity_name == "Test Entity"
    assert record.target_name == "Test Target"
    assert record.document_date == date(2026, 7, 6)
    assert record.record_type == "Financials"

    # Invalid date should fail
    with pytest.raises(ValidationError):
        BaseExtractedRecord(
            entity_name="Test Entity",
            target_name="Test Target",
            document_date="invalid-date",
            record_type="Financials",
        )


def test_generic_document_extraction_validation():
    # Valid payload
    payload = {
        "metadata": {
            "entity_name": "Test Entity",
            "target_name": "Test Target",
            "document_date": "2026-07-06",
            "record_type": "Production Report",
        },
        "title": "Test Title",
        "extracted_data": {
            "gold_grade": "1.25 g/t",
            "tonnes_milled": 45000,
        },
        "data_quality_flags": {
            "requires_human_review": False,
        },
    }
    
    extraction = GenericDocumentExtraction(**payload)
    assert extraction.metadata.entity_name == "Test Entity"
    assert extraction.extracted_data["tonnes_milled"] == 45000
    assert extraction.data_quality_flags.requires_human_review is False


def test_saflii_case_extraction_validation():
    extracted = SafliiExtractedData(
        applicant_plaintiff="State",
        respondent_defendant=["Respondent A"],
        hearing_date=date(2026, 1, 15),
        judgment_date=date(2026, 2, 1),
        reportable=True,
        court="Constitutional Court",
        judges=["Judge X"],
        court_location="Johannesburg",
        ratio_decidendi="Core legal principle applied.",
        precedents_cited=[
            PrecedentCategory(
                raw_citation="State v Example (CCT 01/20) [2020] ZACC 1 (15 February 2020)",
                case_name="State v Example",
                case_number="CCT 01/20",
                neutral_citation="[2020] ZACC 1",
                commercial_citations=[],
                decision_date=date(2020, 2, 15),
                treatment="Applied/Followed",
                reasoning="Directly applicable precedent.",
                url="https://saflii.org/case/1",
            )
        ],
        obiter_dicta="No notable obiter dicta identified in this judgment.",
        order="Appeal dismissed with costs.",
        summary="Detailed case summary.",
        keywords=["constitutional law", "appeal"],
    )

    case_extraction = SafliiCaseExtraction(
        title="State v Respondent A",
        extracted_data=extracted,
        data_quality_flags=DataQualityFlags(requires_human_review=False),
    )

    assert case_extraction.title == "State v Respondent A"
    assert case_extraction.extracted_data.applicant_plaintiff == "State"
    assert len(case_extraction.extracted_data.precedents_cited) == 1
    assert case_extraction.extracted_data.precedents_cited[0].treatment == "Applied/Followed"


def test_saflii_journal_gazette_extraction_validation():
    gazette = SafliiJournalGazetteExtraction(
        title="Government Gazette 12345",
        formatted_text="Cleaned formatted text content of gazette...",
        data_quality_flags=DataQualityFlags(requires_human_review=False),
    )
    assert gazette.title == "Government Gazette 12345"
    assert "Cleaned formatted text" in gazette.formatted_text


def test_saflii_court_roll_extraction_validation():
    roll_row = SafliiCourtRollRow(
        column_1="2026-08-15",
        column_2="Party A v Party B",
        column_3="Courtroom 3",
    )
    roll = SafliiCourtRollExtraction(
        rows=[roll_row],
        data_quality_flags=DataQualityFlags(requires_human_review=False),
    )
    assert len(roll.rows) == 1
    assert roll.rows[0].column_2 == "Party A v Party B"
