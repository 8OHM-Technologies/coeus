import sys
import os
from datetime import date
import pytest
from pydantic import ValidationError
from schemas import DataQualityFlags, BaseExtractedRecord, GenericDocumentExtraction

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))


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
