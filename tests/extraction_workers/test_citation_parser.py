import pytest
from datetime import date
from extraction_workers.utils.citation_parser import parse_legal_citation_string, parse_date_safely


def test_parse_full_citation_row():
    raw = "VVC v JRM and Others (CCT202/24) [2026] ZACC 2; 2026 (3) BCLR 234 (CC); 2026 (3) SA 1 (CC) (21 January 2026)"
    res = parse_legal_citation_string(raw)
    assert res["raw_citation"] == raw
    assert res["case_name"] == "VVC v JRM and Others"
    assert res["case_number"] == "CCT202/24"
    assert res["neutral_citation"] == "[2026] ZACC 2"
    assert res["commercial_citations"] == ["2026 (3) BCLR 234 (CC)", "2026 (3) SA 1 (CC)"]
    assert res["decision_date"] == "2026-01-21"


def test_parse_consolidated_case_numbers():
    raw = "Minister of Police v Kunjana (CCT 25/24; CCT 27/24) [2026] ZACC 23 (15 July 2026)"
    res = parse_legal_citation_string(raw)
    assert res["case_name"] == "Minister of Police v Kunjana"
    assert res["case_number"] == "CCT 25/24; CCT 27/24"
    assert res["neutral_citation"] == "[2026] ZACC 23"
    assert res["commercial_citations"] == []
    assert res["decision_date"] == "2026-07-15"


def test_parse_no_case_number_with_commercial():
    raw = "S v Makwanyane and Another [1995] ZACC 3; 1995 (3) SA 391; 1995 (6) BCLR 665 (CC) (6 June 1995)"
    res = parse_legal_citation_string(raw)
    assert res["case_name"] == "S v Makwanyane and Another"
    assert res["case_number"] is None
    assert res["neutral_citation"] == "[1995] ZACC 3"
    assert res["commercial_citations"] == ["1995 (3) SA 391", "1995 (6) BCLR 665 (CC)"]
    assert res["decision_date"] == "1995-06-06"


def test_parse_standalone_commercial_citation():
    raw = "2011 (3) SA 164"
    res = parse_legal_citation_string(raw)
    assert res["raw_citation"] == raw
    assert res["case_name"] is None
    assert res["neutral_citation"] is None
    assert res["commercial_citations"] == ["2011 (3) SA 164"]


def test_parse_bracketed_commercial_citation():
    raw = "[2005] 2 CPLR 303"
    res = parse_legal_citation_string(raw)
    assert res["raw_citation"] == raw
    assert res["case_name"] is None
    assert res["neutral_citation"] is None
    assert res["commercial_citations"] == ["[2005] 2 CPLR 303"]


def test_parse_empty_and_none():
    assert parse_legal_citation_string("")["raw_citation"] == ""
    assert parse_legal_citation_string(None)["raw_citation"] == ""
