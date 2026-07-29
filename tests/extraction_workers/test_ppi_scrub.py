import os
import sys
import pytest

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from utils.pii_scrub import Scrub


def test_scrub_names():
    scrubber = Scrub()
    
    # Individuals should be redacted
    text_with_names = "The package was delivered by John Doe and Jane Smith."
    scrubbed = scrubber.scrub_text(text_with_names)
    assert "[REDACTED]" in scrubbed
    assert "John Doe" not in scrubbed
    assert "Jane Smith" not in scrubbed


def test_no_scrub_orgs_and_judges():
    scrubber = Scrub()
    
    # Organizations, unions, government entities, and judges should NOT be redacted
    text_with_orgs_and_judges = (
        "The applicant was NUMSA, represented by the Minister of Police. "
        "The case was heard before Judge John Doe and Justice Jane Smith. "
        "The respondent was Acme Mining Pty Ltd."
    )
    scrubbed = scrubber.scrub_text(text_with_orgs_and_judges)
    
    # Ensure they are not redacted
    assert "NUMSA" in scrubbed
    assert "Minister of Police" in scrubbed
    assert "Judge John Doe" in scrubbed
    assert "Justice Jane Smith" in scrubbed
    assert "Acme Mining Pty Ltd" in scrubbed


def test_scrub_dict():
    scrubber = Scrub()
    
    data = {
        "applicant": "Association of Mineworkers and Construction Union (AMCU)",
        "respondent": "Acme Pty Ltd",
        "judges": ["Judge John Smith", "Judge Mary Cooper"],
        "summary": "John Doe was an employee who was dismissed by Acme Pty Ltd."
    }
    
    scrubbed_data = scrubber.scrub_dict(data)
    
    # Check that individual names are redacted but organizations are NOT
    assert "AMCU" in scrubbed_data["applicant"]
    assert "Acme Pty Ltd" in scrubbed_data["respondent"]
    
    # Judges should NOT be redacted
    assert "Judge John Smith" in scrubbed_data["judges"][0]
    assert "Judge Mary Cooper" in scrubbed_data["judges"][1]
    
    # Inside summary, John Doe is redacted, but Acme Pty Ltd is not
    assert "John Doe" not in scrubbed_data["summary"]
    assert "[REDACTED]" in scrubbed_data["summary"]
    assert "Acme Pty Ltd" in scrubbed_data["summary"]
