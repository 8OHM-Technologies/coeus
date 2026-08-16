"""
SAFLII Document Section Splitter
================================
Utility to split raw SAFLII court judgment text into structured sections:
- `header`: Court name, case number, neutral citation, parties, dates, reportability.
- `judgment`: Main body containing facts, evidence, legal analysis, and reasoning.
- `order`: Final court order, verdict, or appeal disposition.
- `citations`: Case references, statutory links, and footnote citations extracted from HTML & text.
"""

import re
from typing import Dict, Any, Optional
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False


def split_saflii_document(text: str, center_content: Optional[str] = None) -> Dict[str, Any]:
    """
    Splits a SAFLII court judgment string into header, judgment, order, and citations.

    Args:
        text (str): Full text of the SAFLII document (e.g. data['full_text']).
        center_content (Optional[str]): HTML content of the document (e.g. data['center_content'])
            used to extract citation target URLs and footnote structure.

    Returns:
        Dict[str, Any]: Dictionary containing:
            - 'header': Introductory section text
            - 'judgment': Main judgment body text
            - 'order': Court order / verdict text
            - 'citations': Dict with 'raw_text' (footnote text) and 'targets' (link URL targets)
    """
    if not text or not isinstance(text, str):
        return {
            "null_values": ["header", "judgment", "order"],
            "header": "",
            "judgment": "",
            "order": "",
            "appearances": "",
            "citations": {"raw_text": "", "targets": []}
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

    # Strip website navigation breadcrumbs ("LawCite" and everything preceding it)
    match_lawcite = re.search(r'\bLawCite\b', header, re.IGNORECASE)
    if match_lawcite:
        header = header[match_lawcite.end():].strip()

    # Strip SAFLII website UI noise lines individually
    strip_noise_patterns = [
        r'(?m)^[ \t]*Download\s+original\s+files[ \t]*$',
        r'(?m)^[ \t]*PDF\s+format[ \t]*$',
        r'(?m)^[ \t]*RTF\s+format[ \t]*$',
        r'(?m)^[ \t]*Links\s+to\s+summary[ \t]*$',
        r'(?m)^[ \t]*Heads\s+of\s+arguments?[ \t]*$',
    ]
    for pattern in strip_noise_patterns:
        header = re.sub(pattern, '', header)

    # Clean up blank line runs
    header = re.sub(r'\n\s*\n+', '\n', header).strip()

    # =========================================================================
    # 2. APPEARANCES EXTRACTION
    # Check for representation / counsel section near the bottom.
    # =========================================================================
    appearances = ""
    app_pattern = re.compile(
        r'(?m)^[ \t]*(?:A\s*P\s*P\s*E\s*A\s*R\s*A\s*N\s*C\s*E\s*S|Appearances|Representation|Counsel\s*:)[ \t\:\-]*$',
        re.IGNORECASE
    )
    match_app = app_pattern.search(body_and_tail)
    if match_app and match_app.start() > len(body_and_tail) * 0.4:
        appearances = body_and_tail[match_app.start():].strip()
        body_and_tail = body_and_tail[:match_app.start()].strip()

    # =========================================================================
    # 3. CITATIONS & FOOTNOTES EXTRACTION
    # Extract link targets from HTML center_content and strip bottom footnotes.
    # =========================================================================
    citations_raw_text = ""
    targets = []
    seen_urls = set()

    if center_content and BS4_AVAILABLE:
        try:
            soup = BeautifulSoup(center_content, 'html.parser')

            # A. Extract footnote divs (<div id="ftn1">, <div id="fn1">, etc.)
            ftn_divs = soup.find_all('div', id=lambda x: x and (x.startswith('ftn') or x.startswith('fn')))
            if ftn_divs:
                first_ftn = ftn_divs[0].get_text(strip=True)
                m_tag = re.search(r'\[\d+\]', first_ftn)
                if m_tag:
                    tag_num = re.search(r'\d+', m_tag.group(0)).group(0)
                    # Search for line-start footnote tag near end (> 0.7) of body_and_tail, followed by word/uppercase letter
                    search_start = int(len(body_and_tail) * 0.7)
                    tail_text = body_and_tail[search_start:]
                    ftn_line_m = re.search(fr'(?m)^[ \t]*\[{tag_num}\][ \t\n]+[A-Z0-9]', tail_text)
                    if ftn_line_m:
                        pos = search_start + ftn_line_m.start()
                        citations_raw_text = body_and_tail[pos:].strip()
                        body_and_tail = body_and_tail[:pos].strip()

            # B. Extract citation link targets from HTML <a> tags
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

    # Fallback for plain-text footnotes if HTML parsing did not extract footnotes
    if not citations_raw_text:
        # Search for footnote block near tail after judge signature / representation
        m_ftn_tail = re.search(r'(?m)^[ \t]*(\[1\]|\(1\))\s+[A-Z0-9]', body_and_tail[int(len(body_and_tail)*0.7):])
        if m_ftn_tail:
            actual_pos = int(len(body_and_tail) * 0.7) + m_ftn_tail.start()
            citations_raw_text = body_and_tail[actual_pos:].strip()
            body_and_tail = body_and_tail[:actual_pos].strip()

    # =========================================================================
    # 4. ORDER / VERDICT SPLIT
    # Search for order header or judge signature boundary near end of body.
    # =========================================================================
    judgment = body_and_tail
    order = ""

    # Primary Explicit Order Header Regex
    order_pattern = re.compile(
        r'(?i)(?:'
        r'IN\s+THE\s+RESULT,?\s*(?:THE\s+FOLLOWING\s+ORDER\s+IS\s+MADE|IT\s+IS\s+ORDERED|THE\s+APPEAL\s+IS|THE\s+ORDER|ORDER|THE\s+CONDONATION)?|'
        r'IN\s+THE\s+RESULT\s+[^.\n]+ORDER[^.\n]*|'
        r'THE\s+APPEAL\s+(?:AGAINST\s+[^.\n]+)?IS\s+(?:DISMISSED|UPHELD)|'
        r'I\s+(?:ACCORDINGLY\s+|THEREFORE\s+)?(?:MAKE|GRANT|GRANTED)\s+AN?\s+ORDER\s+IN\s+THE\s+FOLLOWING\s+TERMS|'
        r'I\s+(?:THEREFORE\s+|ACCORDINGLY\s+)?(?:MAKE|GRANT|GRANTED)\s+(?:AN?|THE)\s+FOLLOWING\s+ORDER|'
        r'ACCORDINGLY,?\s+(?:THE\s+ACCUSED\s+IS\s+SENTENCED|THE\s+FOLLOWING\s+ORDER\s+(?:SHALL|WILL)\s+ISSUE)|'
        r'THE\s+FOLLOWING\s+ORDER\s+(?:IS\s+MADE|SHALL\s+ISSUE|WILL\s+ISSUE)|'
        r'MY\s+ORDER\s+IS\s+(?:THEREFORE\s+|ACCORDINGLY\s+)?AS\s+FOLLOWS|'
        r'IT\s+IS\s+ORDERED\s+THAT|'
        r'IT\s+IS\s+HEREBY\s+ORDERED|'
        r'IN\s+THE\s+PREMISES,?\s+THE\s+FOLLOWING\s+ORDER|'
        r'DIE\s+VOLGENDE\s+BEVEL\s+WORD\s+GEMAAK|'
        r'BYGEVOLG\s+WORD|'
        r'DIE\s+(?:AANSOEK|APPÈL)\s+WORD\s+(?:VAN\s+DIE\s+ROL\s+GESKRAP|VAN\s+DIE\s+HAND\s+GEWYS|TOEGESTAAN)|'
        r'^\s*(?:[A-Z0-9\.\-\[\]\(\)]+[ \t]+)?(?:ORDER|THE ORDER|THE RESULT|RULING|CONCLUSION|VERDICT|BEVEL|DIE RESULTAAT|VONNIS|COSTS|KOSTE|SUMMARY OF ORDER|FINAL ORDER|VARIATION ORDER|INTERLOCUTORY ORDER)\s*:?\s*$'
        r')',
        re.MULTILINE
    )

    matches = list(order_pattern.finditer(body_and_tail))
    if matches:
        last_m = matches[-1]
        judgment = body_and_tail[:last_m.start()].strip()
        order = body_and_tail[last_m.start():].strip()
    else:
        # Fallback 1: Search for Inline Ruling Sentences in the final portion of the text
        inline_order_pattern = re.compile(
            r'(?i)(?:'
            r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:In\s+the\s+result|By\s+virtue\s+of\s+the\s+aforegoing|In\s+the\s+premises|Accordingly),?\s+(?:I\s+order\s+that|the\s+accused\s+is\s+sentenced|the\s+application|the\s+condonation|the\s+appeal|the\s+claim|the\s+review|the\s+matter|it\s+is\s+ordered)|'
            r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:The\s+application|The\s+condonation\s+application|The\s+appeal|The\s+claim|The\s+action|The\s+matter|Application\s+for\s+leave\s+to\s+appeal|The\s+notice\s+of\s+motion)\s+(?:for\s+[^.\n]+|brought\s+by\s+[^.\n]+)?is\s+(?:therefore\s+|accordingly\s+)?(?:dismissed|granted|upheld|refused|struck|varied)(?:\s+with\s+costs)?|'
            r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:Die\s+aansoek|Die\s+appèl|Aansoek\s+om\s+verlof\s+tot\s+appèl|Die\s+saak|Die\s+respondent)\s+word\s+(?:afgewys|van\s+die\s+rol\s+geskrap|toegestaan|bekragtig|gelas)|'
            r'(?:[0-9]+\.|\([0-9]+\)|\[[0-9]+\])?\s*(?:There\s+is\s+no|no)\s+order\s+as\s+to\s+costs|Costs\s+must\s+follow|Costs\s+to\s+stand\s+over|application\s+was\s+dismissed|appeal\s+to\s+this\s+Court\s+is\s+dismissed|relief\s+contained\s+in\s+the\s+order|application\s+must\s+succeed|we\s+accordingly\s+(?:refuse|decline|grant)|(?:leave\s+to\s+appeal|direct\s+access|application)\s+(?:directly\s+to\s+this\s+Court\s+)?is\s+(?:accordingly\s+)?(?:refused|declined)'
            r')'
        )
        inline_matches = list(inline_order_pattern.finditer(body_and_tail))
        if inline_matches and inline_matches[-1].start() > len(body_and_tail) * 0.3:
            last_im = inline_matches[-1]
            judgment = body_and_tail[:last_im.start()].strip()
            order = body_and_tail[last_im.start():].strip()
        else:
            # Fallback 2: Check for Judge Signature Block near the end (e.g. "JUDGE OF THE HIGH COURT")
            judge_sig_pattern = re.compile(
                r'(?m)^[ \t]*(?:_+|–{3,}|—{3,})\s*\n.*(?:JUDGE|ACTING JUDGE|JUSTICE|R\s*/|AJ\b|J\b).*$',
                re.IGNORECASE
            )
            match_sig = judge_sig_pattern.search(body_and_tail)
            if match_sig and match_sig.start() > len(body_and_tail) * 0.4:
                judgment = body_and_tail[:match_sig.start()].strip()
                order = body_and_tail[match_sig.start():].strip()

    # Final Fallback: If no explicit order was found, search for "struck from/off the roll" variations
    if not order or not order.strip():
        struck_pattern = re.compile(
            r'(?i)\b(?:struck\s+(?:off|from)|striking\s+(?:off|from))\s+(?:the\s+roll|the\s+court\s+roll)\b'
        )
        if struck_pattern.search(body_and_tail):
            order = "Struck from the roll"

    # Determine null / empty core section fields (citations excluded as citations can naturally be null)
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
        "citations": {
            "raw_text": citations_raw_text,
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
        print(f"CITATIONS RAW ({len(parsed['citations']['raw_text'])} chars):\n{parsed['citations']['raw_text'][:200]}...\n")
        print(f"CITATIONS TARGETS ({len(parsed['citations']['targets'])} links)")
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
                print(f"CITATIONS RAW ({len(parsed['citations']['raw_text'])} chars):\n{parsed['citations']['raw_text'][:200]}...\n")
                print(f"CITATIONS TARGETS ({len(parsed['citations']['targets'])} links):")
                for tgt in parsed['citations']['targets'][:5]:
                    print(f"  - [{tgt['text']}] -> {tgt['url']}")
                if len(parsed['citations']['targets']) > 5:
                    print(f"  ... (+{len(parsed['citations']['targets']) - 5} more links)")
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
