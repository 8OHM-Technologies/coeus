"""
SAFLII Document Section Splitter
================================
Utility to split raw SAFLII court judgment text into structured sections:
- `header`: Court name, case number, neutral citation, parties, dates, reportability.
- `judgment`: Main body containing facts, evidence, legal analysis, and reasoning.
- `order`: Final court order, verdict, or appeal disposition.
- `appearances`: Representation and counsel details.
- `footnotes`: Footnotes, case references, statutory links, and citations extracted from HTML & text.
"""

import re
from typing import Dict, Any, Optional
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


SAFLII_NOISE_PATTERNS = [
    r'(?m)^[ \t]*Download\s+original\s+files[ \t]*$',
    r'(?m)^[ \t]*PDF\s+format[ \t]*$',
    r'(?m)^[ \t]*RTF\s+format[ \t]*$',
    r'(?m)^[ \t]*Links\s+to\s+summary[ \t]*$',
    r'(?m)^[ \t]*Heads\s+of\s+arguments?[ \t]*$',
]


def clean_saflii_text(text: str, collapse_to_single_newline: bool = False) -> str:
    """
    Cleans raw SAFLII text (e.g. headers, journal articles, raw documents):
    1. Strips website navigation breadcrumbs ("LawCite" and everything preceding it).
    2. Strips SAFLII website UI noise lines (e.g. Download original files, PDF format, RTF format).
    3. Cleans up excessive blank lines (collapses to single newline for headers or double newline for body paragraphs).

    Args:
        text (str): Input text to clean.
        collapse_to_single_newline (bool): If True, collapses runs of blank lines to '\n'
            (ideal for headers). If False, normalizes multiple blank lines to '\n\n'
            (ideal for body documents/journals).

    Returns:
        str: Cleaned text.
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned = text.strip()

    # Strip website navigation breadcrumbs ("LawCite" and everything preceding it)
    match_lawcite = re.search(r'\bLawCite\b', cleaned, re.IGNORECASE)
    if match_lawcite:
        cleaned = cleaned[match_lawcite.end():].strip()

    # Strip SAFLII website UI noise lines individually
    for pattern in SAFLII_NOISE_PATTERNS:
        cleaned = re.sub(pattern, '', cleaned)

    # Clean up blank line runs
    if collapse_to_single_newline:
        cleaned = re.sub(r'\n\s*\n+', '\n', cleaned).strip()
    else:
        cleaned = re.sub(r'\n\s*\n+', '\n\n', cleaned).strip()

    return cleaned


def split_saflii_document(text: str, center_content: Optional[str] = None) -> Dict[str, Any]:
    """
    Splits a SAFLII court judgment string into header, judgment, order, appearances, and footnotes.

    Args:
        text (str): Full text of the SAFLII document (e.g. data['full_text']).
        center_content (Optional[str]): HTML content of the document (e.g. data['center_content'])
            used to extract footnote target URLs and structure.

    Returns:
        Dict[str, Any]: Dictionary containing:
            - 'header': Introductory section text
            - 'judgment': Main judgment body text
            - 'order': Court order / verdict text
            - 'appearances': Representation details
            - 'footnotes': Dict with 'raw_text' (footnote text) and 'targets' (link URL targets)
    """
    if not text or not isinstance(text, str):
        return {
            "null_values": ["header", "judgment", "order"],
            "header": "",
            "judgment": "",
            "order": "",
            "appearances": "",
            "footnotes": {"raw_text": "", "targets": []}
        }

    cleaned_text = text.strip()

    # =========================================================================
    # 1. HEADER SPLIT
    # Find boundary between intro header and judgment body.
    # =========================================================================
    header = ""
    body_and_tail = cleaned_text

    header_pattern = re.compile(
        r'(?m)^[ \t]*(?:J\s*U\s*D\s*G\s*M\s*E\s*N\s*T|R\s*U\s*L\s*I\s*N\s*G|REASONS\s+FOR\s+JUDGMENT|EX\s+TEMPORE\s+JUDGMENT|SENTENCE|SUMMARY|VARIATION\s+ORDER|INTERLOCUTORY\s+ORDER)[ \t\:\-]*$',
        re.IGNORECASE
    )

    match_header = header_pattern.search(cleaned_text)
    if match_header and match_header.start() < 4000:
        header = cleaned_text[:match_header.start()].strip()
        body_and_tail = cleaned_text[match_header.end():].strip()
    else:
        # Fallback: search for first paragraph marker [1] or 1. near top
        match_p1 = re.search(r'(?m)^\s*(?:\[1\]|1\.\s+[A-Z])', cleaned_text)
        if match_p1 and match_p1.start() < 4000:
            header = cleaned_text[:match_p1.start()].strip()
            body_and_tail = cleaned_text[match_p1.start():].strip()
        else:
            # Fallback: first 1500 characters
            header = cleaned_text[:1500].strip()
            body_and_tail = cleaned_text[1500:].strip()

    # Strip website navigation breadcrumbs, UI noise lines, and excess blank lines
    header = clean_saflii_text(header, collapse_to_single_newline=True)

    # =========================================================================
    # 2. PATTERNS: APPEARANCES, EXPLICIT ORDER, INLINE ORDER, JUDGE SIGNATURES
    # =========================================================================
    app_pattern = re.compile(
        r'(?m)^[ \t]*(?:'
        r'A\s*P\s*P\s*E\s*A\s*R\s*A\s*N\s*C\s*E\s*S\b|'
        r'Appearances?\b|'
        r'Representation\b|'
        r'Legal\s+Representation\b|'
        r'VERSKYNINGS\b|'
        r'Counsel\s*:|'
        r'COUNSEL\s*(?:\n|\s)+FOR\s+[A-Z\s]+:|'
        r'For\s+(?:the\s+)?(?:Applicants?|Respondents?|Appellants?|Plaintiffs?|Defendants?|State|Accused)\s*:|'
        r'ON\s*(?:\n|\s)+BEHALF\s+OF\s+[A-Z\s]+:|'
        r'REPRESENTATION\s*:'
        r')',
        re.IGNORECASE
    )

    order_pattern = re.compile(
        r'(?i)(?:'
        r'IN\s+THE\s+RESULT,?\s*(?:THE\s+FOLLOWING\s+ORDERS?\s+(?:IS|ARE)\s+MADE|IT\s+IS\s+ORDERED|THE\s+APPEAL\s+IS|THE\s+ORDER|ORDER|THE\s+CONDONATION)?|'
        r'IN\s+THE\s+PREMISES,?\s*(?:THE\s+FOLLOWING\s+ORDERS?\s+(?:IS|ARE)\s+MADE|IT\s+IS\s+ORDERED|I\s+MAKE\s+THE\s+FOLLOWING\s+ORDER)?|'
        r'IN\s+THE\s+RESULT\s+[^.\n]+ORDER[^.\n]*|'
        r'THE\s+APPEAL\s+(?:AGAINST\s+[^.\n]+)?(?:IS|WAS)\s+(?:DISMISSED|UPHELD)|'
        r'(?:ACCORDINGLY,?\s+)?(?:THE\s+FOLLOWING\s+ORDERS?\s+(?:IS|ARE)\s+(?:THEREFORE\s+|ACCORDINGLY\s+)?(?:MADE|GRANTED)[;:\.]?)|'
        r'(?:ACCORDINGLY,?\s+)?(?:THE\s+FOLLOWING\s+ORDERS?\s+(?:SHALL|WILL)\s+ISSUE[;:\.]?)|'
        r'(?:ACCORDINGLY,?\s+)?(?:I|WE)\s+(?:FIND|ORDER|MAKE|ISSUE)\s+THE\s+FOLLOWING[;:\.]?|'
        r'(?:I|WE)\s+(?:WOULD\s+)?(?:ACCORDINGLY\s+|THEREFORE\s+)?(?:PROPOSE|ISSUE|MAKE|GRANT)\s+THE\s+FOLLOWING\s+ORDER[;:\.]?|'
        r'THE\s+SENTENCES?\s+IMPOSED\s+(?:IS|ARE)\s+SET\s+ASIDE\s+AND\s+REPLACED\s+WITH\s+THE\s+FOLLOWING[;:\.]?|'
        r'FOR\s+THESE\s+REASONS\s+(?:WE|I)\s+(?:MAKE|MADE)\s+THE\s+ORDER|'
        r'(?:I|WE)\s+(?:ACCORDINGLY\s+|THEREFORE\s+)?(?:MAKE|GRANT|GRANTED|ISSUE)\s+AN?\s+ORDER\s+IN\s+THE\s+FOLLOWING\s+TERMS|'
        r'(?:I|WE)\s+(?:THEREFORE\s+|ACCORDINGLY\s+)?(?:MAKE|GRANT|GRANTED|ISSUE)\s+(?:AN?|THE)\s+FOLLOWING\s+ORDER|'
        r'ACCORDINGLY,?\s+(?:THE\s+ACCUSED\s+IS\s+SENTENCED|THE\s+FOLLOWING\s+ORDER\s+(?:SHALL|WILL)\s+ISSUE)|'
        r'DIE\s+(?:AANSOEK|APP[EÈ\u010d]L)\s+WORD\s+(?:MET\s+KOSTE\s+)?(?:VAN\s+DIE\s+ROL\s+GESKRAP|VAN\s+DIE\s+HAND\s+GEWYS|TOEGESTAAN)|'
        r'^\s*(?:[A-Z0-9\.\-\[\]\(\)]+[ \t]+)?(?:ORDER|THE ORDER|THE RESULT|RULING|CONCLUSION|VERDICT|BEVEL|DIE RESULTAAT|VONNIS|COSTS|KOSTE|SUMMARY OF ORDER|FINAL ORDER|VARIATION ORDER|INTERLOCUTORY ORDER)\s*:?\s*$'
        r')',
        re.MULTILINE
    )

    inline_order_pattern = re.compile(
        r'(?i)(?:'
        r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:In\s+the\s+result|By\s+virtue\s+of\s+the\s+aforegoing|In\s+the\s+premises|Accordingly),?\s+(?:I\s+(?:make|grant|issue|order)|the\s+accused\s+is\s+sentenced|the\s+application|the\s+condonation|the\s+appeal|the\s+claim|the\s+review|the\s+matter|it\s+is\s+ordered)|'
        r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:The\s+application|The\s+condonation\s+application|The\s+appeal|The\s+claim|The\s+action|The\s+matter|Application\s+for\s+leave\s+to\s+appeal|The\s+notice\s+of\s+motion)\s+(?:for\s+[^.\n]+|brought\s+by\s+[^.\n]+)?(?:is|was)\s+(?:therefore\s+|accordingly\s+)?(?:dismissed|granted|upheld|refused|struck|varied|adjourned|postponed|remitted|settled)(?:\s+with\s+costs|\s+sine\s+die)?|'
        r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:I|we)\s+(?:accordingly\s+|therefore\s+|consequently\s+)?(?:dismissed|granted|ordered|refused|upheld|issue)\b|'
        r'it\s+is\s+so\s+ordered|'
        r'(?:reflected\s+in\s+)?the\s+order\s+(?:that\s+)?(?:I|we)\s+(?:grant|make)(?:,\s*which\s+is\s+as\s+follows)?|'
        r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:Die\s+aansoek|Die\s+app[eè\u010d]l|Aansoek\s+om\s+verlof\s+tot\s+app[eè\u010d]l|Die\s+saak|Die\s+respondent)\s+word\s+(?:met\s+koste\s+)?(?:afgewys|van\s+die\s+rol\s+geskrap|toegestaan|bekragtig|gelas|van\s+die\s+hand\s+gewys)|'
        r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:There\s+is\s+no|no)\s+order\s+as\s+to\s+costs|Costs\s+must\s+follow|Costs\s+to\s+stand\s+over|application\s+was\s+dismissed|appeal\s+to\s+this\s+Court\s+is\s+dismissed|relief\s+contained\s+in\s+the\s+order|application\s+must\s+succeed|we\s+accordingly\s+(?:refuse|decline|grant)|(?:leave\s+to\s+appeal|direct\s+access|application)\s+(?:directly\s+to\s+this\s+Court\s+)?is\s+(?:accordingly\s+)?(?:refused|declined)'
        r')'
    )

    judge_sig_pattern = re.compile(
        r'(?m)^[ \t]*(?:'
        r'(?:_+|–{3,}|—{3,})\s*\n.*(?:JUDGE|ACTING JUDGE|JUSTICE|R\s*/|AJ\b|J\b).*$|'
        r'(?:(?:JUDGE|ACTING JUDGE|JUSTICE)\s+OF\s+THE\s+(?:HIGH\s+COURT|SUPREME\s+COURT)|JUDGE PRESIDENT|DEPUTY JUDGE PRESIDENT)\b.*$|'
        r'(?:JUDGE|ACTING JUDGE|JUSTICE|MAGISTRATE|REGIONAL MAGISTRATE|ASSESSOR)\b[ \t]*$|'
        r'[A-Z][A-Za-z\.\s\-\']{1,35}\s+(?:AJ|J|JP|DJP|DCJ|CJ|ARP|RP|AR)\b[ \t\:]*$'
        r')',
        re.MULTILINE
    )

    # =========================================================================
    # 3. FOOTNOTES EXTRACTION
    # Extract link targets from HTML center_content and strip bottom footnotes.
    # =========================================================================
    footnotes_raw_text = ""
    targets = []
    seen_urls = set()

    if center_content and BS4_AVAILABLE:
        try:
            soup = BeautifulSoup(center_content, 'html.parser')

            # A. Extract footnote containers (<div id="ftn1">, <div id="fn1">, <p class="footnote">, etc.)
            ftn_divs = soup.find_all('div', id=lambda x: x and (x.startswith('ftn') or x.startswith('fn') or 'footnote' in x.lower()))
            if not ftn_divs:
                ftn_divs = soup.find_all(lambda tag: tag.name in ('div', 'p') and tag.get('class') and any('footnote' in c.lower() or 'ftn' in c.lower() for c in tag.get('class')))
            if ftn_divs:
                first_ftn = ftn_divs[0].get_text(strip=True)
                m_tag = re.search(r'\[\d+\]', first_ftn)
                if m_tag:
                    tag_num = re.search(r'\d+', m_tag.group(0)).group(0)
                    search_start = int(len(body_and_tail) * 0.5)
                    tail_text = body_and_tail[search_start:]
                    ftn_line_m = re.search(fr'(?m)^[ \t]*\[{tag_num}\][ \t\n]+["\'‘“\w]', tail_text)
                    if ftn_line_m:
                        pos = search_start + ftn_line_m.start()
                        footnotes_raw_text = body_and_tail[pos:].strip()
                        body_and_tail = body_and_tail[:pos].strip()

            # B. Extract citation / footnote link targets from HTML <a> tags
            for a in soup.find_all('a'):
                href = a.get('href', '')
                if not href:
                    continue
                # Ignore download file links (.pdf, .rtf, .doc)
                if href.endswith('.pdf') or href.endswith('.rtf') or href.endswith('.doc'):
                    continue
                if 'LawCite' in href or '/za/cases/' in href or '/za/legis/' in href or 'autolink_findcases' in a.get('class', []):
                    full_url = f'https://www.saflii.org{href}' if href.startswith('/') else href
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        link_text = ' '.join(a.get_text(separator=' ', strip=True).split())
                        if link_text and link_text.lower() != 'lawcite':
                            targets.append({'text': link_text, 'url': full_url})
        except Exception:
            pass

    # Fallback for plain-text footnotes if HTML parsing did not extract footnotes.
    # Note: Paragraphs in SA judgments are numbered [1], [2]... A footnote block at the end
    # restarts numbering at [1] in the tail of the document, followed by [2].
    if not footnotes_raw_text:
        m_ftn_start = re.search(r'(?m)^[ \t]*\[1\][ \t\n]+[A-Z0-9"\'‘“]', body_and_tail[int(len(body_and_tail) * 0.5):])
        if m_ftn_start:
            rel_pos = int(len(body_and_tail) * 0.5) + m_ftn_start.start()
            after_start = body_and_tail[rel_pos + m_ftn_start.end():]
            if re.search(r'(?m)^[ \t]*\[2\][ \t\n]+', after_start[:5000]):
                footnotes_raw_text = body_and_tail[rel_pos:].strip()
                body_and_tail = body_and_tail[:rel_pos].strip()

    # Guard: Ensure footnotes did not swallow an Order that occurs after them
    if footnotes_raw_text:
        m_ord_in_fn = order_pattern.search(footnotes_raw_text) or inline_order_pattern.search(footnotes_raw_text)
        if m_ord_in_fn:
            trailing_order_part = footnotes_raw_text[m_ord_in_fn.start():]
            footnotes_raw_text = footnotes_raw_text[:m_ord_in_fn.start()].strip()
            body_and_tail = (body_and_tail + "\n\n" + trailing_order_part).strip()

    # =========================================================================
    # 4. ORDER / VERDICT & APPEARANCES EXTRACTION
    # =========================================================================
    judgment = body_and_tail
    order = ""
    appearances = ""

    # Primary: Explicit Order Header Match
    matches = list(order_pattern.finditer(body_and_tail))
    if matches and matches[-1].start() > len(body_and_tail) * 0.3:
        last_m = matches[-1]
        judgment = body_and_tail[:last_m.start()].strip()
        tail = body_and_tail[last_m.start():].strip()

        # Check if appearances exist inside the tail
        m_app = app_pattern.search(tail)
        if m_app and m_app.start() > len(tail) * 0.1:
            appearances = tail[m_app.start():].strip()
            order = tail[:m_app.start()].strip()
        else:
            order = tail
    else:
        # Fallback 1: Search for Inline Ruling Sentences in the final portion
        inline_matches = list(inline_order_pattern.finditer(body_and_tail))
        if inline_matches and inline_matches[-1].start() > len(body_and_tail) * 0.3:
            last_im = inline_matches[-1]
            judgment = body_and_tail[:last_im.start()].strip()
            tail = body_and_tail[last_im.start():].strip()

            m_app = app_pattern.search(tail)
            if m_app and m_app.start() > len(tail) * 0.1:
                appearances = tail[m_app.start():].strip()
                order = tail[:m_app.start()].strip()
            else:
                order = tail
        else:
            # Fallback 2: Look for judge signature or appearances near the tail
            m_app = app_pattern.search(body_and_tail)
            boundary_pos = None
            if m_app and m_app.start() > len(body_and_tail) * 0.5:
                boundary_pos = m_app.start()
                appearances = body_and_tail[m_app.start():].strip()
                pre_app = body_and_tail[:m_app.start()].strip()
            else:
                pre_app = body_and_tail

            search_start_sig = int(len(pre_app) * 0.5)
            sig_matches = list(judge_sig_pattern.finditer(pre_app[search_start_sig:]))
            if sig_matches:
                sig_pos = search_start_sig + sig_matches[0].start()
                pre_sig = pre_app[:sig_pos].rstrip()
                para_match = re.search(r'(?:\n\s*\n|\n(?=[0-9]+[\.\)]|\[[0-9]+\]|[a-z]\)))([^\n]+)$', pre_sig)
                if para_match:
                    order_start = para_match.start()
                    judgment = pre_app[:order_start].strip()
                    order = pre_app[order_start:].strip()
                else:
                    judgment = pre_app[:sig_pos].strip()
                    order = pre_app[sig_pos:].strip()
            elif boundary_pos:
                para_match = re.search(r'(?:\n\s*\n|\n(?=[0-9]+[\.\)]|\[[0-9]+\]|[a-z]\)))([^\n]+)$', pre_app)
                if para_match:
                    order_start = para_match.start()
                    judgment = pre_app[:order_start].strip()
                    order = pre_app[order_start:].strip()
                else:
                    order = pre_app[-500:].strip()
                    judgment = pre_app[:-500].strip()

    # Final Fallback: "struck from/off the roll"
    if not order or not order.strip():
        struck_pattern = re.compile(
            r'(?i)\b(?:struck\s+(?:off|from)|striking\s+(?:off|from))\s+(?:the\s+roll|the\s+court\s+roll)\b'
        )
        if struck_pattern.search(body_and_tail):
            order = "Struck from the roll"

    # Separate any trailing footnotes that might have been included in appearances
    if appearances:
        m_app_ftn = re.search(r'(?m)^[ \t]*\[\d+\][ \t\n]+', appearances)
        if m_app_ftn:
            ftn_part = appearances[m_app_ftn.start():].strip()
            appearances = appearances[:m_app_ftn.start()].strip()
            if not footnotes_raw_text:
                footnotes_raw_text = ftn_part

    # Determine null / empty core section fields (footnotes excluded as footnotes can naturally be null)
    null_values = []
    if not header or not header.strip():
        null_values.append("header")
    if not judgment or not judgment.strip():
        null_values.append("judgment")
    if not order or not order.strip():
        null_values.append("order")

    return {
        "null_values": null_values,
        "header": header,
        "judgment": judgment,
        "order": order,
        "appearances": appearances,
        "footnotes": {
            "raw_text": footnotes_raw_text,
            "targets": targets
        }
    }


if __name__ == "__main__":
    import argparse
    import json
    import os
    import sys
    import subprocess
    from pathlib import Path

    parser = argparse.ArgumentParser(description="SAFLII Document Section Parser CLI")
    parser.add_argument("--file", "-f", type=str, help="Path to text file containing SAFLII judgment")
    parser.add_argument("--record-id", "-r", type=str, help="Specific record UUID from extracted_records DB to fetch and parse")
    parser.add_argument("--limit", "-l", type=int, default=5, help="Number of database sample records to parse (default: 5)")
    parser.add_argument("--cases-only", action="store_true", help="Filter database query to court case judgments only (excluding law journals, gazettes, and rolls)")
    parser.add_argument("--export", "-e", type=str, help="Export parsed section results to JSON file")
    parser.add_argument("--mark-review", action="store_true", help="Mark records with null/failed section parsing as requiring human review in extracted_records DB table")

    args = parser.parse_args()
    results = []

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[ERROR] File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        text = file_path.read_text(encoding="utf-8")
        parsed = split_saflii_document(text)
        results.append({"source": str(file_path), "sections": parsed})

        print(f"=== PARSED FILE: {file_path} ===")
        print(f"HEADER ({len(parsed['header'])} chars):\n{parsed['header'][:300]}...\n")
        print(f"JUDGMENT ({len(parsed['judgment'])} chars):\n{parsed['judgment'][:300]}...\n")
        print(f"ORDER ({len(parsed['order'])} chars):\n{parsed['order'][:300]}...\n")
        print(f"FOOTNOTES RAW ({len(parsed['footnotes']['raw_text'])} chars):\n{parsed['footnotes']['raw_text'][:200]}...\n")
        print(f"FOOTNOTES TARGETS ({len(parsed['footnotes']['targets'])} links)")
    else:
        try:
            from dotenv import load_dotenv

            env_path = Path(__file__).resolve().parent.parent.parent / ".env"
            if env_path.exists():
                load_dotenv(env_path, override=True)
            else:
                load_dotenv(override=True)

            host = os.environ.get("POSTGRES_HOST", "100.71.253.103")
            user = os.environ.get("POSTGRES_USER", "postgres")
            password = (os.environ.get("POSTGRES_PASSWORD") or "").strip("'\"")
            db = os.environ.get("POSTGRES_DB", "coeus")
            port = os.environ.get("POSTGRES_PORT", "5432")

            if args.record_id:
                sql_where = f"er.id = '{args.record_id}'"
                limit_clause = ""
            else:
                cases_filter = "AND t.target_type = 'cases' AND t.target_name NOT LIKE '%Rolls%'" if args.cases_only else ""
                sql_where = f"er.record_type = 'saflii_courts' AND er.status = 'detailed' AND er.data->>'full_text' IS NOT NULL AND LENGTH(TRIM(er.data->>'full_text')) > 50 {cases_filter}"
                limit_clause = f"LIMIT {args.limit}"

            print(f"Fetching record(s) from database...")
            cmd = [
                "psql", "-h", host, "-p", port, "-U", user, "-d", db, "-t", "-A",
                "-c", f"SELECT json_build_object('id', er.id, 'target_name', t.target_name, 'text', er.data->>'full_text', 'center_content', er.data->>'center_content') FROM extracted_records er JOIN targets t ON er.target_id = t.id WHERE {sql_where} {limit_clause};"
            ]
            env = os.environ.copy()
            if password:
                env["PGPASSWORD"] = password

            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=True)
            raw_lines = [line for line in proc.stdout.strip().split("\n") if line.strip()]

            print(f"Loaded {len(raw_lines)} records from database.\n")

            null_record_ids = []

            for idx, raw_json in enumerate(raw_lines, 1):
                rec_data = json.loads(raw_json)
                rec_id = rec_data.get("id")
                target_name = rec_data.get("target_name")
                text = rec_data.get("text") or ""
                center_content = rec_data.get("center_content") or ""
                parsed = split_saflii_document(text, center_content)
                results.append({"record_id": str(rec_id), "target_name": target_name, "sections": parsed})

                if parsed.get("null_values"):
                    null_record_ids.append(str(rec_id))

                print(f"=== SAMPLE {idx} (Record ID: {rec_id} | Target: {target_name}) ===")
                print(f"NULL FIELDS: {parsed['null_values']}")
                print(f"HEADER ({len(parsed['header'])} chars):\n{parsed['header'][:250]}...\n")
                print(f"JUDGMENT ({len(parsed['judgment'])} chars):\n{parsed['judgment'][:250]}...\n")
                print(f"ORDER ({len(parsed['order'])} chars):\n{parsed['order'][:250]}...\n")
                print(f"FOOTNOTES RAW ({len(parsed['footnotes']['raw_text'])} chars):\n{parsed['footnotes']['raw_text'][:200]}...\n")
                print(f"FOOTNOTES TARGETS ({len(parsed['footnotes']['targets'])} links):")
                for tgt in parsed['footnotes']['targets'][:5]:
                    print(f"  - [{tgt['text']}] -> {tgt['url']}")
                if len(parsed['footnotes']['targets']) > 5:
                    print(f"  ... (+{len(parsed['footnotes']['targets']) - 5} more links)")
                print("-" * 60)

            print(f"\n=== RECORDS WITH NULL/EMPTY FIELDS ({len(null_record_ids)} / {len(results)}) ===")
            print(json.dumps(null_record_ids, indent=2))

            if args.mark_review and null_record_ids:
                print(f"\n[DB UPDATE] Marking {len(null_record_ids)} records as requiring human review...")
                id_list_str = ", ".join(f"'{rid}'" for rid in null_record_ids)
                update_sql = f"UPDATE extracted_records SET requires_human_review = true, review_reason = 'Document parsing failed', parsed_at = NOW(), updated_at = NOW() WHERE id IN ({id_list_str});"
                update_cmd = ["psql", "-h", host, "-p", port, "-U", user, "-d", db, "-c", update_sql]
                subprocess.run(update_cmd, capture_output=True, text=True, env=env, check=True)
                print("[DB UPDATE] Successfully updated database flags.")

        except Exception as err:
            print(f"[ERROR] Failed to query database: {err}", file=sys.stderr)
            sys.exit(1)

    if args.export:
        out_path = Path(args.export)
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Successfully saved parsed output to {out_path}")
