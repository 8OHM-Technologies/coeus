import re
import json
import xml.etree.ElementTree as ET
from typing import Union
import spacy

# Load Spacy NLP model
nlp = spacy.load("en_core_web_trf")


class Scrub:
    def __init__(self):
        self.patterns = {
            "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            "phone": r"\b\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b",
            "ssn": r"\b\d{3}[-]?\d{2}[-]?\d{4}\b",
            "ip_address_v4": r"\b(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\b",
            "ip_address_v6": r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b",
            "hostname": r"\b(?:(?:[a-zA-Z]|[a-zA-Z][a-zA-Z0-9\-]*[a-zA-Z0-9])\.)*(?:(?:[A-Za-z]|(?:[A-Za-z][A-Za-z0-9\-]*[A-Za-z0-9]))\.(?:[a-zA-Z]{2,})|(?:xn--[A-Za-z0-9]+))\b",
            "uuid": r"\b(?:[0-9a-fA-F]){8}-(?:[0-9a-fA-F]){4}-(?:[0-9a-fA-F]){4}-(?:[0-9a-fA-F]){4}-(?:[0-9a-fA-F]){12}\b",
        }

        # Judicial title keywords — used both in regex pre-scan and NLP context check
        self._judicial_titles = {
            "judge", "justice", "magistrate", "acting", "j", "aj", "dcj", "jа",
            "judge of the high court", "judge of the supreme court",
        }

    # ------------------------------------------------------------------
    # Judge-name pre-identification
    # ------------------------------------------------------------------

    def _extract_judge_names(self, text: str) -> set[str]:
        """
        Scan *text* for judge names using two complementary strategies:

        1. Regex patterns that look for judicial title markers near a name
           (e.g. "WANLESS J", "BC WANLESS\nJUDGE OF THE HIGH COURT",
           "Judge Smith", "Magistrate Dube", "Acting Justice Molefe").

        2. spaCy NER on the last ~30 % of the document, where the formal
           sign-off block typically lives, keeping only PERSON entities that
           appear within a few tokens of a judicial keyword.

        Returns a set of name strings (and their component parts) so that
        any occurrence of those strings anywhere in the document will be
        recognised as a judge name and not redacted.
        """
        judge_names: set[str] = set()

        # ── Strategy 1: regex-based patterns ──────────────────────────

        # Pattern A: "SURNAME J" / "SURNAME AJ" / "SURNAME DCJ" (all-caps surname)
        # Also handles "BC WANLESS J" style (initials + surname + suffix)
        judicial_suffix = r"(?:AJ|DCJ|JA|JP|J)\b"
        pattern_suffix = re.compile(
            r"\b([A-Z]{1,3}(?:\s+[A-Z]{2,})+|[A-Z]{2,})\s+" + judicial_suffix,
            re.MULTILINE,
        )
        for m in pattern_suffix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(0).strip()))

        # Pattern B: Titled prefix — "Judge Smith", "Magistrate Nkosi",
        #            "Acting Justice Molefe", "Justice van der Merwe"
        title_prefix = (
            r"(?:Acting\s+)?(?:Judge|Justice|Magistrate|Chief\s+Justice)"
        )
        pattern_prefix = re.compile(
            r"\b" + title_prefix + r"\s+([A-Z][a-z]+(?:\s+[a-z]{1,4}\s+[A-Z][a-z]+)?(?:\s+[A-Z][a-z]+)*)\b"
        )
        for m in pattern_prefix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(1).strip()))

        # Pattern C: Formal sign-off block —
        #   "BC WANLESS\nJUDGE OF THE HIGH COURT"  (name on its own line,
        #   immediately followed by a line containing JUDGE / JUSTICE / MAGISTRATE)
        pattern_signoff = re.compile(
            r"^([A-Z][A-Z\s\.]{2,})\s*\n\s*(?:JUDGE|ACTING JUDGE|JUSTICE|MAGISTRATE|DEPUTY JUDGE)",
            re.MULTILINE,
        )
        for m in pattern_signoff.finditer(text):
            judge_names.update(self._tokenise_name(m.group(1).strip()))

        # ── Strategy 2: spaCy NER on the tail of the document ─────────
        # The last 30 % (at most 4 000 chars) is where the sign-off lives.
        tail_start = max(0, int(len(text) * 0.70))
        tail_text = text[tail_start:]

        tail_doc = nlp(tail_text)
        judicial_kw = {
            "judge", "judges", "justice", "justices", "magistrate",
            "magistrates", "acting", "j", "aj", "dcj",
        }
        for ent in tail_doc.ents:
            if ent.label_ != "PERSON":
                continue
            # Look at the 5 tokens before and after the entity
            window_before = tail_doc[max(0, ent.start - 5) : ent.start]
            window_after = tail_doc[ent.end : min(len(tail_doc), ent.end + 5)]
            near_tokens = {t.text.lower() for t in list(window_before) + list(window_after)}
            if near_tokens & judicial_kw:
                judge_names.update(self._tokenise_name(ent.text))

        return judge_names

    @staticmethod
    def _tokenise_name(name: str) -> set[str]:
        """
        Return the full name string *and* its individual tokens (length ≥ 2)
        so that initials alone ("BC") are not added as protected tokens.
        """
        parts: set[str] = {name}
        for token in name.split():
            if len(token) > 2:  # skip single-letter initials
                parts.add(token)
        return parts

    # ------------------------------------------------------------------
    # Public scrub interface
    # ------------------------------------------------------------------

    def scrub_text(self, text: str, judge_names: set[str] | None = None) -> str:
        scrubbed_text = text
        for category, pattern in self.patterns.items():
            if category == "phone":
                matches = re.finditer(pattern, scrubbed_text)
                for match in matches:
                    matched_phone = match.group(0)
                    # Remove parentheses from matched phone numbers
                    matched_phone = re.sub(r"^\((\d{3})\)$", r"\1", matched_phone)
                    scrubbed_text = scrubbed_text.replace(matched_phone, "[REDACTED]")
            else:
                scrubbed_text = re.sub(pattern, "[REDACTED]", scrubbed_text)

        scrubbed_text = self.scrub_pii_with_nlp(scrubbed_text, judge_names=judge_names)
        return scrubbed_text

    def scrub_pii_with_nlp(self, text: str, judge_names: set[str] | None = None) -> str:
        nlp_doc = nlp(text)
        final_text = text

        org_keywords = [
            "ltd", "limited", "pty", "proprietary", "inc", "incorporated", "corp", "corporation",
            "gmbh", "plc", "llc", "llp", "holdings", "group", "industries", "services", "enterprises",
            "ventures", "partners", "trust", "bank", "co", "company", "companies",
            "union", "unions", "association", "associations", "federation", "society", "council",
            "committee", "chamber", "coalition", "alliance", "congress", "amcu", "numsa", "num",
            "cosatu", "satawu", "popcru", "nehawu", "denosa", "solidarity", "solidariteit",
            "minister", "mec", "department", "registrar", "commissioner", "director", "magistrate",
            "judge", "judges", "justice", "justices",
            "state", "government", "president", "governor", "premier", "mayor", "municipality",
            "municipal", "city of", "province", "provincial", "national", "board", "agency",
            "authority", "commission", "office", "bureau", "administration", "protector", "police",
            "sheriff", "school", "university", "college", "clinic", "hospital", "church", "foundation",
            "charity", "club", "institute", "center", "centre", "vs", "versus"
        ]

        protected_names: set[str] = judge_names or set()

        for name in nlp_doc.ents:
            if name.label_ != "PERSON":
                continue

            # Skip if it looks like an organisation / company name
            normalized = name.text.lower()
            is_org = any(
                re.search(r"\b" + re.escape(kw) + r"\b", normalized)
                for kw in org_keywords
            )
            if is_org:
                continue

            # ── Judge-name guard ──────────────────────────────────────
            # 1. Check pre-identified judge names (full name or any component)
            name_tokens = set(name.text.split())
            if any(
                pn.lower() in normalized or any(t.lower() == pn.lower() for t in name_tokens)
                for pn in protected_names
            ):
                continue

            # 2. Fallback: check surrounding context tokens for judicial keywords
            preceding_tokens = nlp_doc[max(0, name.start - 3) : name.start]
            if any(t.text.lower() in self._judicial_titles for t in preceding_tokens):
                continue

            final_text = re.sub(re.escape(name.text), "[REDACTED]", final_text)

        return final_text

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def scrub(
        self, input_data: Union[str, dict], original_format: str = "txt"
    ) -> Union[str, dict, list[dict]]:
        # Pre-identify judge names from the raw document before any scrubbing
        raw_text = input_data if isinstance(input_data, str) else json.dumps(input_data)
        judge_names = self._extract_judge_names(raw_text)

        if isinstance(input_data, dict):
            return self.scrub_dict(input_data, judge_names=judge_names)

        if original_format == "json":
            scrubbed_data = json.loads(input_data)
            scrubbed_data = self.scrub_dict(scrubbed_data, judge_names=judge_names)
        elif original_format == "ndjson":
            scrubbed_data = [
                self.scrub_dict(json.loads(line), judge_names=judge_names)
                for line in input_data.splitlines()
            ]
        elif original_format == "xml":
            root = ET.fromstring(input_data)
            self.scrub_xml(root, judge_names=judge_names)
            scrubbed_data = ET.tostring(root, encoding="unicode")
        else:
            scrubbed_data = self.scrub_text(input_data, judge_names=judge_names)

        return scrubbed_data

    def scrub_dict(self, data: dict, judge_names: set[str] | None = None) -> dict:
        for key, value in data.items():
            if isinstance(value, dict):
                data[key] = self.scrub_dict(value, judge_names=judge_names)
            elif isinstance(value, list):
                data[key] = [
                    self.scrub_dict(item, judge_names=judge_names)
                    if isinstance(item, dict)
                    else (self.scrub_text(item, judge_names=judge_names) if isinstance(item, str) else item)
                    for item in value
                ]
            elif isinstance(value, str):
                data[key] = self.scrub_text(value, judge_names=judge_names)
        return data

    def scrub_xml(self, node: ET.Element, judge_names: set[str] | None = None) -> None:
        if node.text is not None:
            node.text = self.scrub_text(node.text, judge_names=judge_names)
        for child in node:
            self.scrub_xml(child, judge_names=judge_names)


if __name__ == "__main__":
    pii_scrubber = Scrub()
    file = "<some file path>"
    format = "<one of 'json', 'ndjson', 'xml' or 'txt'>"

    with open(file, "r") as f:
        input_data = f.read()

    scrubbed_data = pii_scrubber.scrub(input_data, format)
    print("Original data:")
    print(input_data)
    print("Scrubbed data:")
    print(scrubbed_data)