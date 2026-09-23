import os
import sys
import pytest

# Ensure scripts and extraction_workers are importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../scripts")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from fix_saflii_case_numbers import extract_case_number_from_title, extract_case_number_from_header


def test_extract_case_number_from_standard_title():
    # Standard single case number in title
    title = "Damise v Minister of Police (EL354/2018) [2019] ZAECELLC 34 (15 October 2019)"
    assert extract_case_number_from_title(title) == "EL354/2018"

    # Title with company parenthesis before case number
    title_corp = "Computicket (Pty) Ltd v Competition Commission (118/CAC/APR12) [2012] ZACAC 7"
    assert extract_case_number_from_title(title_corp) == "118/CAC/APR12"

    # Joint case numbers separated by semicolon
    title_joint = "Continental Tyres SA v Commission (150/CAC/JUN17; 151/CAC/JUN17) [2018] ZACAC 6"
    assert extract_case_number_from_title(title_joint) == "150/CAC/JUN17; 151/CAC/JUN17"

    # Numerical case number with slash
    title_num = "Smith v Jones (2556/09) [2010] ZAECGHC 12"
    assert extract_case_number_from_title(title_num) == "2556/09"


def test_extract_case_number_exclusions():
    # Title without case number (only date or court indicator)
    assert extract_case_number_from_title("State v Khumalo [2021] ZACC 12 (12 May 2021)") is None

    # Title with non-case keyword inside parentheses
    assert extract_case_number_from_title("Minister v Citizen (Unreported Judgment) [2020] ZASCA 1") is None
    assert extract_case_number_from_title("Minister v Citizen (Appeal Judgment 2) [2020] ZASCA 1") is None

    # Neutral citation mistakenly in parentheses
    assert extract_case_number_from_title("Matter X ([2020] ZACC 4) [2020] ZACC 4") is None

    # Empty or None title
    assert extract_case_number_from_title("") is None
    assert extract_case_number_from_title(None) is None


def test_extract_case_number_from_header():
    # Standard header snippet
    header = "IN THE HIGH COURT OF SOUTH AFRICA\nCase No: 12345/2021\nIn the matter between:"
    assert extract_case_number_from_header(header) == "12345/2021"

    # Header with CCT format
    header_cct = "CONSTITUTIONAL COURT OF SOUTH AFRICA\nCase CCT 37/11\nHeard on: 11 August 2011"
    assert extract_case_number_from_header(header_cct) == "CCT 37/11"

    # Header with CAC format
    header_cac = "COMPETITION APPEAL COURT\nCase number: 11/CAC/AUG01\nBefore: Davis JP"
    assert extract_case_number_from_header(header_cac) == "11/CAC/AUG01"

    # Empty or snippet with no case number
    assert extract_case_number_from_header("Just plain text with no case tag") is None
    assert extract_case_number_from_header(None) is None
