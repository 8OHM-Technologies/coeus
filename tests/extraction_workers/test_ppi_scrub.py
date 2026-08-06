"""Unit tests for the GLiNER-based PII Redaction module."""

import os
import sys
import json
import pytest

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from utils.pii_scrub import Scrub, DEFAULT_PII_LABELS


def test_gliner_compliance_example():
    """Validates the official GLiNER compliance and PII redaction example."""
    scrubber = Scrub()

    sample_text = """
Patient John Smith (DOB: 03/15/1982, SSN: 123-45-6789) was seen at
Mayo Clinic on January 10, 2024. Contact: john.smith@email.com,
+1-555-867-5309. Insurance ID: BC-9876543. His home address is
742 Evergreen Terrace, Springfield, IL 62704.
"""
    redacted = scrubber.scrub_text(sample_text)

    # Verify sensitive data was redacted
    assert "John Smith" not in redacted
    assert "john.smith@email.com" not in redacted
    assert "123-45-6789" not in redacted
    assert "+1-555-867-5309" not in redacted

    # Verify standard redaction tags are inserted
    assert "[PERSON]" in redacted or "[NAME]" in redacted
    assert "[EMAIL]" in redacted
    assert "[PHONE NUMBER]" in redacted


def test_scrub_dict_nested():
    """Validates recursive dictionary and list structure scrubbing."""
    scrubber = Scrub()

    data = {
        "title": "Patient intake record",
        "patient": "Jane Doe",
        "contact_info": {
            "email": "jane.doe@example.com",
            "cell": "555-012-3456",
        },
        "records": [
            "Follow-up scheduled for Jane Doe.",
            "Contact: jane.doe@example.com",
        ],
        "record_id": 1042,
        "is_active": True,
    }

    scrubbed = scrubber.scrub_dict(data)

    # Check non-string values are preserved as-is
    assert scrubbed["record_id"] == 1042
    assert scrubbed["is_active"] is True

    # Check nested string values are redacted
    assert "Jane Doe" not in scrubbed["patient"]
    assert "[PERSON]" in scrubbed["patient"]

    assert "jane.doe@example.com" not in scrubbed["contact_info"]["email"]
    assert "[EMAIL]" in scrubbed["contact_info"]["email"]

    assert "Jane Doe" not in scrubbed["records"][0]
    assert "[PERSON]" in scrubbed["records"][0]

    assert "jane.doe@example.com" not in scrubbed["records"][1]
    assert "[EMAIL]" in scrubbed["records"][1]


def test_custom_labels_and_threshold():
    """Validates overriding PII labels and threshold at call-time."""
    scrubber = Scrub()

    text = "Please reach out to Alice Johnson at alice@domain.org."
    # Only redact email, ignoring person
    redacted_email_only = scrubber.scrub_text(text, labels=["email"], threshold=0.3)

    assert "Alice Johnson" in redacted_email_only
    assert "alice@domain.org" not in redacted_email_only
    assert "[EMAIL]" in redacted_email_only


def test_scrub_json_string():
    """Validates the top-level scrub() entry point on JSON strings."""
    scrubber = Scrub()

    data_dict = {
        "user": "Alice Walker",
        "notes": "Direct email: alice.walker@corp.net",
    }
    json_str = json.dumps(data_dict)

    result = scrubber.scrub(json_str)
    assert isinstance(result, str)

    parsed = json.loads(result)
    assert "Alice Walker" not in parsed["user"]
    assert "alice.walker@corp.net" not in parsed["notes"]
    assert "[PERSON]" in parsed["user"]
    assert "[EMAIL]" in parsed["notes"]


def test_edge_cases():
    """Validates edge cases like empty strings, whitespace, and non-string types."""
    scrubber = Scrub()

    assert scrubber.scrub_text("") == ""
    assert scrubber.scrub_text("   ") == "   "
    assert scrubber.scrub_dict({}) == {}
    assert scrubber.scrub(12345) == 12345
    assert scrubber.scrub(None) is None
