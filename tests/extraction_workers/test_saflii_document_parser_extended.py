import os
import sys
import pytest

# Ensure repo root is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.utils.saflii_document_parser import split_saflii_document
from extraction_workers.new_saflii_scraper import check_page_state


def test_statutory_subclause_does_not_truncate_order():
    """Ensure statutory subclause (1) or citation (1) in tail text does not truncate order."""
    text = """
IN THE HIGH COURT OF SOUTH AFRICA
CASE NO: 123/2023

In the matter between:
ABC (PTY) LTD and XYZ (PTY) LTD

JUDGMENT

[1] This is an application for eviction.
[2] The respondent relies on section 14(1) of the Act.
Section 14(1) of the Act provides:
(1) The tenant shall not be evicted without court authorization.
In 2006 (1) SA 718 the court held that proper notice must be given.

[3] Having considered the papers:
The following orders are made:
1. The respondent is ordered to vacate the premises within 30 days.
2. The respondent is to pay the costs.

MAMOSEBO J
JUDGE OF THE HIGH COURT
"""
    result = split_saflii_document(text)
    assert "order" not in result["null_values"], f"Order was null! Result: {result['null_values']}"
    assert "vacate the premises" in result["order"]
    assert "MAMOSEBO J" in result["order"] or "MAMOSEBO J" in result["appearances"]
    assert len(result["footnotes"]["raw_text"]) == 0


def test_plural_orders_with_semicolon():
    """Test plural order header ending with semicolon."""
    text = """
HIGH COURT OF SOUTH AFRICA
CASE NO: 456/2024

JUDGMENT

[1] The applicants seek an order declaring the suspension invalid.
[2] For reasons discussed above, the application must succeed.

The following orders are therefore made;
a) The first respondent's decision is declared unlawful and invalid.
b) The first respondent is ordered to pay the costs.

CC WILLIAMS
JUDGE
"""
    result = split_saflii_document(text)
    assert "order" not in result["null_values"]
    assert "declared unlawful and invalid" in result["order"]
    assert "CC WILLIAMS" in result["order"] or "CC WILLIAMS" in result["appearances"]


def test_appellate_proposal_order():
    """Test standard appellate proposal order format."""
    text = """
HIGH COURT OF SOUTH AFRICA
CASE NO: A123/2024

JUDGMENT

[1] This is an appeal against sentence.
[2] In my view, the magistrate misdirected herself.

[3] I would accordingly propose the following order:
1. The appeal is upheld.
2. The sentence is set aside and replaced with a fine of R10 000.

MOSSOP J
I agree
VAHED J
"""
    result = split_saflii_document(text)
    assert "order" not in result["null_values"]
    assert "The appeal is upheld" in result["order"]
    assert "fine of R10 000" in result["order"]


def test_inline_ruling_order():
    """Test inline dismissal and strike from the roll rulings."""
    text = """
IN THE HIGH COURT
CASE NO: 999/2025

JUDGMENT

[1] The applicant did not appear.
[2] For the foregoing reasons the appeal was dismissed with costs.

RADEBE J
"""
    result = split_saflii_document(text)
    assert "order" not in result["null_values"]
    assert "appeal was dismissed with costs" in result["order"]


def test_afrikaans_judgment_order():
    """Test Afrikaans judgment order extraction."""
    text = """
IN DIE HOË HOF VAN SUID-AFRIKA
SAAK NR: 777/2022

UITSPRAAK

[1] Die respondent betwis die geldigheid van die ooreenkoms.
[2] Die aansoek het geen meriete nie.

Die aansoek word met koste van die hand gewys.

JC FRONEMAN ARP
Ek stem saam.
JF MYBURGH RP
VERSKYNINGS
Vir Applikant: Adv Smith
"""
    result = split_saflii_document(text)
    assert "order" not in result["null_values"]
    assert "van die hand gewys" in result["order"]
    assert "Vir Applikant" in result["appearances"]


def test_cloudflare_5xx_detection_in_check_page_state():
    """Ensure Cloudflare Error 525, What happened?, and 5xx errors are caught as BLOCKED."""
    assert check_page_state("What happened?", "", "Cloudflare is unable to establish an SSL connection") == "BLOCKED"
    assert check_page_state("", "", "SSL handshake failed Error code 525") == "BLOCKED"
    assert check_page_state("502 Bad Gateway", "", "") == "BLOCKED"
    assert check_page_state("504 Gateway Time-out", "", "") == "BLOCKED"
    assert check_page_state("", "", "Cloudflare Ray ID: abc123def456") == "BLOCKED"
