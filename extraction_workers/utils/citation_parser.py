"""
Legal Citation & Footnote Parser Utility
=========================================
Parses full South African legal titles, case citations, and footnote reference strings
into structured components:
  1. case_name: Style of cause / parties involved (e.g. 'VVC v JRM and Others')
  2. case_number: Court internal file/tracking number (e.g. 'CCT202/24' or 'CCT 25/24; CCT 27/24')
  3. neutral_citation: Electronic medium-neutral citation (e.g. '[2026] ZACC 2')
  4. commercial_citations: List of printed law report citations (e.g. ['2026 (3) BCLR 234 (CC)', '2026 (3) SA 1 (CC)'])
  5. decision_date: Officially delivered date (YYYY-MM-DD or None)
  6. raw_citation: Verbatim input string
"""

import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional

# Commercial Law Report Series Acronyms
LAW_REPORTS_PATTERN = (
    r'(?:^|[\s;,\(\[])(?:\d{4}|\[\d{4}\])'
    r'(?:\s*\(\d+\)|\s+\[\d+\]|\s+\d+)?'
    r'\s+(?:All\s+SA|ALL\s+SA|BCLR|BLLR|ILJ|SA|SACR|CPLR|JOL|CLD|SALLR|BPLR|LAC|LCC|SCA|CC)'
    r'\s+\d+'
    r'(?:\s*\([A-Z0-9\s]+\))?'
)

COMMON_DATE_FORMATS = (
    "%d %B %Y",
    "%d %b %Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%d",
    "%d %B %y",
    "%d %b %y",
)


def parse_date_safely(date_str: str) -> Optional[str]:
    """Parse common legal date formats to ISO YYYY-MM-DD string."""
    if not date_str:
        return None
    clean = re.sub(r'\s+', ' ', date_str.strip())
    try:
        return date.fromisoformat(clean).isoformat()
    except Exception:
        pass
    for fmt in COMMON_DATE_FORMATS:
        try:
            return datetime.strptime(clean, fmt).date().isoformat()
        except Exception:
            pass
    return None


def parse_legal_citation_string(raw_str: str) -> Dict[str, Any]:
    """
    Parse a full legal citation string into its structured components.

    Example input:
        'VVC v JRM and Others (CCT202/24) [2026] ZACC 2; 2026 (3) BCLR 234 (CC); 2026 (3) SA 1 (CC) (21 January 2026)'
    """
    if not raw_str or not isinstance(raw_str, str):
        return {
            "raw_citation": "",
            "case_name": None,
            "case_number": None,
            "neutral_citation": None,
            "commercial_citations": [],
            "decision_date": None,
        }

    raw_citation = raw_str.strip()
    working_text = raw_citation

    # 1. Extract Trailing Delivery Date: (21 January 2026)
    decision_date: Optional[str] = None
    date_match = re.search(r'\(([^()]+)\)[\s.;]*$', working_text)
    if date_match:
        candidate_date = date_match.group(1).strip()
        parsed_d = parse_date_safely(candidate_date)
        if parsed_d:
            decision_date = parsed_d
            working_text = working_text[:date_match.start()].strip()

    # 2. Extract Medium-Neutral Citation: [2026] ZACC 2 or [1995] ZASCA 12
    neutral_citation: Optional[str] = None
    nc_match = re.search(r'\[(\d{4})\]\s+([A-Z]+)\s+(\d+)', working_text)
    if nc_match:
        neutral_citation = nc_match.group(0).strip()
        # Remove neutral citation from working text for commercial & case name extraction
        nc_start, nc_end = nc_match.span()
        working_text_before_nc = working_text[:nc_start].strip()
        working_text_after_nc = working_text[nc_end:].strip()
    else:
        working_text_before_nc = working_text
        working_text_after_nc = ""

    # 3. Extract Commercial Law Report Citations from text
    commercial_citations: List[str] = []
    comm_matches = list(re.finditer(LAW_REPORTS_PATTERN, working_text, re.IGNORECASE))
    for m in comm_matches:
        c_str = m.group(0).strip()
        c_str = c_str.strip(";,. ")
        if c_str and c_str != neutral_citation and c_str not in commercial_citations:
            commercial_citations.append(c_str)

    # Clean out commercial citations from working_text_before_nc before case_number & case_name extraction
    for comm in commercial_citations:
        working_text_before_nc = working_text_before_nc.replace(comm, " ")

    # 4. Extract Case Number: e.g. (CCT202/24) or (CCT 25/24; CCT 27/24) or (16/FN/Mar04)
    case_number: Optional[str] = None
    cn_match = re.search(
        r'\(([A-Za-z0-9\s;/\-]*?\d+/\d{2,4}(?:[A-Za-z0-9\s;/\-]*?\d+/\d{2,4})*[A-Za-z0-9\s;/\-]*)\)',
        working_text_before_nc,
        re.IGNORECASE
    )
    if not cn_match:
        cn_match = re.search(
            r'\(((?:(?:Case|Application|Matter|Ref)\s*(?:No|Number)?\s*[:.]?\s*)[A-Za-z0-9\s/;\-]+)\)',
            working_text_before_nc,
            re.IGNORECASE
        )
    if cn_match:
        cand = cn_match.group(1).strip()
        if not any(k in cand.lower() for k in ["pty", "ltd", "others", "capacity", "acting", "executor", "trustee"]):
            if "/" in cand or re.search(r'(?:Case|Application|Matter|Ref)\s*(?:No|Number)?', cand, re.IGNORECASE):
                case_number = cand
                working_text_before_nc = (
                    working_text_before_nc[:cn_match.start()] +
                    working_text_before_nc[cn_match.end():]
                ).strip()

    # 5. Extract Case Name / Style of Cause
    case_name_raw = working_text_before_nc.strip(";,. \t\n")
    if not case_name_raw and working_text:
        parts = re.split(r'[;\[\(]', working_text)
        if parts:
            case_name_raw = parts[0].strip()

    case_name = case_name_raw if case_name_raw else None
    if case_name:
        case_name = re.sub(r'\s+', ' ', case_name).strip(";,. ")
        if case_name.isdigit() or case_name == neutral_citation or case_name in commercial_citations:
            case_name = None

    return {
        "raw_citation": raw_citation,
        "case_name": case_name,
        "case_number": case_number,
        "neutral_citation": neutral_citation,
        "commercial_citations": commercial_citations,
        "decision_date": decision_date,
    }
