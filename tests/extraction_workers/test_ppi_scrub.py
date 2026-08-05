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


def test_no_scrub_case_numbers_and_court_locations():
    scrubber = Scrub()

    text = (
        "In the High Court of South Africa, Gauteng Local Division, Johannesburg. "
        "Case No: 1234/2023. Citation: [2023] ZAGPPHC 45. "
        "Heard on 15 March 2023 at the Johannesburg Labour Court."
    )
    scrubbed = scrubber.scrub_text(text)

    assert "1234/2023" in scrubbed
    assert "ZAGPPHC 45" in scrubbed
    assert "Gauteng Local Division" in scrubbed
    assert "Johannesburg" in scrubbed
    assert "Labour Court" in scrubbed
    assert "[REDACTED]" not in scrubbed


def test_no_scrub_various_judge_title_styles():
    scrubber = Scrub()

    text = (
        "Judgment delivered by Van Niekerk J with Unterhalter AJ concurring. "
        "Before Sutherland DJP and Zondo CJ in the Constitutional Court. "
        "Molahlehi J presided."
    )
    scrubbed = scrubber.scrub_text(text)

    assert "Van Niekerk J" in scrubbed
    assert "Unterhalter AJ" in scrubbed
    assert "Sutherland DJP" in scrubbed
    assert "Zondo CJ" in scrubbed
    assert "Molahlehi J" in scrubbed


def test_scrub_dict():
    scrubber = Scrub()

    data = {
        "case_number": "JR 123/21",
        "court": "Labour Court, Johannesburg",
        "applicant": "Association of Mineworkers and Construction Union (AMCU)",
        "respondent": "Acme Pty Ltd",
        "judges": ["Judge John Smith", "Judge Mary Cooper"],
        "summary": "John Doe was an employee who was dismissed by Acme Pty Ltd."
    }

    scrubbed_data = scrubber.scrub_dict(data)

    # Protected keys must never be modified
    assert scrubbed_data["case_number"] == "JR 123/21"
    assert scrubbed_data["court"] == "Labour Court, Johannesburg"
    assert "AMCU" in scrubbed_data["applicant"]
    assert "Acme Pty Ltd" in scrubbed_data["respondent"]

    # Judges should NOT be redacted
    assert "Judge John Smith" in scrubbed_data["judges"][0]
    assert "Judge Mary Cooper" in scrubbed_data["judges"][1]

    # Inside summary, John Doe is redacted, but Acme Pty Ltd is not
    assert "John Doe" not in scrubbed_data["summary"]
    assert "[REDACTED]" in scrubbed_data["summary"]
    assert "Acme Pty Ltd" in scrubbed_data["summary"]


def test_no_scrub_employer_name_field():
    scrubber = Scrub()

    data = {
        "applicant_employee": "David Miller",
        "employer_name": "Bob Smith Contracting",
        "summary": "David Miller was employed at Bob Smith Contracting."
    }

    scrubbed_data = scrubber.scrub_dict(data)

    # Employer field value must never be redacted
    assert scrubbed_data["employer_name"] == "Bob Smith Contracting"

    # Employee name in summary must be redacted, but employer name must be protected
    assert "David Miller" not in scrubbed_data["summary"]
    assert "[REDACTED]" in scrubbed_data["summary"]
    assert "Bob Smith Contracting" in scrubbed_data["summary"]
