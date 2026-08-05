import re
import json
import xml.etree.ElementTree as ET
from typing import Union
import spacy

# Load Spacy NLP model with fallback support
def _load_spacy_model():
    for model_name in ["en_core_web_trf", "en_core_web_sm", "en_core_web_md"]:
        try:
            return spacy.load(model_name)
        except Exception:
            continue
    import subprocess
    import sys
    try:
        subprocess.run([sys.executable, "-m", "spacy", "download", "en_core_web_sm"], check=True)
        return spacy.load("en_core_web_sm")
    except Exception as exc:
        raise OSError(
            "Could not load or download any spaCy model (en_core_web_trf, en_core_web_sm). "
            "Please run: python -m spacy download en_core_web_sm"
        ) from exc


nlp = _load_spacy_model()


class Scrub:
    def __init__(self):
        # We do NOT run generic regex pattern redactions for phone/ssn/ip/hostname/uuid
        # because case numbers (e.g. 1234/2023, (011) 555-1234), citations, and dates
        # were being indiscriminately redacted.
        self.patterns = {}

        # Judicial title keywords — used in regex pre-scan and NLP context checks
        self._judicial_titles = {
            "judge", "justice", "magistrate", "acting", "j", "aj", "dcj", "ja", "aja", "jp", "djp", "p", "cj", "eja",
            "judge of the high court", "judge of the supreme court", "judge president", "deputy judge president",
            "chief justice", "coram", "before",
        }

    @staticmethod
    def _is_protected_key(key: str) -> bool:
        """Returns True if the dictionary key represents a field that should NEVER be redacted."""
        k = key.lower().strip()
        if "employer" in k:
            return True

        protected_keys = {
            # Corporate / Org
            "company", "company_name", "organisation", "organisation_name",
            "organization", "organization_name", "firm", "firm_name", "employer_name",
            # Judicial
            "judge", "judges", "coram", "bench", "presiding_judge", "author_judge",
            "hearing_judge", "magistrate", "justice",
            # Case / Metadata / Structure
            "case_number", "case_no", "case_num", "case_id", "citation", "court",
            "court_name", "court_location", "division", "jurisdiction", "date",
            "hearing_date", "judgment_date", "url", "link", "id", "record_type",
            "type", "journal_name", "publisher", "volume", "issue", "heading",
        }
        return k in protected_keys

    @staticmethod
    def _is_employer_key(key: str) -> bool:
        """Backwards compatible alias for employer key check."""
        return Scrub._is_protected_key(key)

    def _extract_protected_names(self, data: Union[dict, list, str]) -> set[str]:
        """Scans structured data for values of protected fields (employers, judges, courts, case numbers) to protect them globally across text fields."""
        protected_names: set[str] = set()
        if isinstance(data, dict):
            for key, value in data.items():
                if self._is_protected_key(key):
                    if isinstance(value, str) and value.strip():
                        protected_names.update(self._tokenise_name(value.strip()))
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, str) and item.strip():
                                protected_names.update(self._tokenise_name(item.strip()))
                    elif isinstance(value, dict):
                        protected_names.update(self._extract_protected_names(value))
                else:
                    protected_names.update(self._extract_protected_names(value))
        elif isinstance(data, list):
            for item in data:
                protected_names.update(self._extract_protected_names(item))
        return protected_names

    def _extract_employer_names(self, data: Union[dict, list, str]) -> set[str]:
        """Backwards compatible alias for employer name extraction."""
        return self._extract_protected_names(data)

    # ------------------------------------------------------------------
    # Judge-name pre-identification
    # ------------------------------------------------------------------

    def _extract_judge_names(self, text: str) -> set[str]:
        """
        Scan *text* for judge names using regex patterns and spaCy NER.
        Handles suffixes (J, AJ, JA, AJA, JP, DJP, P, CJ, DCJ, EJA),
        prefixes (Judge, Justice, Magistrate, Chief Justice, etc.),
        and sign-off blocks.
        """
        judge_names: set[str] = set()

        # ── Strategy 1: regex-based patterns ──────────────────────────

        # Pattern A: Suffixes — e.g. "VAN NIEKERK J", "Smith J", "Unterhalter AJ", "Sutherland DJP", "Zondo CJ", "Mlambo JP", "Van der Merwe JA"
        judicial_suffix = r"(?:AJ|DCJ|JA|AJA|JP|DJP|CJ|EJA|P|J)\b"

        # Match Title Case or UPPER CASE surnames preceding suffix
        pattern_suffix = re.compile(
            r"\b([A-Z][a-zA-Z']*(?:\s+(?:van|der|den|de|du|la|le|von)\s+[A-Z][a-zA-Z']*|\s+[A-Z][a-zA-Z']*)*|\b[A-Z]{1,3}(?:\s+[A-Z]{2,})+|[A-Z]{2,})\s+" + judicial_suffix,
            re.MULTILINE,
        )
        for m in pattern_suffix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(0).strip()))

        # Pattern B: Titled prefix — "Judge Smith", "Magistrate Nkosi",
        #            "Acting Justice Molefe", "Justice van der Merwe", "Chief Justice Zondo"
        title_prefix = (
            r"(?:Acting\s+)?(?:Judge|Justice|Magistrate|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President)"
        )
        pattern_prefix = re.compile(
            r"\b" + title_prefix + r"\s+([A-Z][a-z]+(?:\s+(?:van|der|den|de|du|la|le|von)\s+[A-Z][a-z]+)?(?:\s+[A-Z][a-z]+)*)\b"
        )
        for m in pattern_prefix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(1).strip()))

        # Pattern C: Formal sign-off / header block —
        #   "BC WANLESS\nJUDGE OF THE HIGH COURT" or "BEFORE THE HONOURABLE JUSTICE XYZ"
        pattern_signoff = re.compile(
            r"^([A-Z][A-Z\s\.]{2,})\s*\n\s*(?:JUDGE|ACTING JUDGE|JUSTICE|MAGISTRATE|DEPUTY JUDGE)",
            re.MULTILINE,
        )
        for m in pattern_signoff.finditer(text):
            judge_names.update(self._tokenise_name(m.group(1).strip()))

        pattern_before = re.compile(
            r"(?:BEFORE\s+THE\s+HONOURABLE\s+)?(?:MR\s+|MS\s+|MRS\s+)?(?:JUSTICE|JUDGE|ACTING\s+JUDGE|MAGISTRATE)\s+([A-Z][a-zA-Z'\s\.]+)",
            re.IGNORECASE,
        )
        for m in pattern_before.finditer(text):
            val = m.group(1).strip()
            if len(val) < 60:
                judge_names.update(self._tokenise_name(val))

        # ── Strategy 2: spaCy NER on the tail of the document ─────────
        tail_start = max(0, int(len(text) * 0.70))
        tail_text = text[tail_start:]

        tail_doc = nlp(tail_text)
        judicial_kw = {
            "judge", "judges", "justice", "justices", "magistrate",
            "magistrates", "acting", "j", "aj", "dcj", "ja", "aja", "jp", "djp", "p", "cj",
        }
        for ent in tail_doc.ents:
            if ent.label_ != "PERSON":
                continue
            window_before = tail_doc[max(0, ent.start - 5) : ent.start]
            window_after = tail_doc[ent.end : min(len(tail_doc), ent.end + 5)]
            near_tokens = {t.text.lower() for t in list(window_before) + list(window_after)}
            if near_tokens & judicial_kw:
                judge_names.update(self._tokenise_name(ent.text))

        return judge_names

    @staticmethod
    def _tokenise_name(name: str) -> set[str]:
        """
        Extract the full name, name without title prefixes/suffixes,
        and significant surname tokens for protection.
        """
        clean_name = name.strip()
        parts: set[str] = {clean_name}

        # Strip judicial titles
        without_title = re.sub(
            r"^(?:Judge|Justice|Magistrate|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President|Acting\s+Judge|Acting\s+Justice)\s+",
            "", clean_name, flags=re.IGNORECASE
        )
        without_suffix = re.sub(
            r"\s+(?:AJ|DCJ|JA|AJA|JP|DJP|CJ|EJA|P|J)$",
            "", without_title, flags=re.IGNORECASE
        ).strip()

        if without_suffix:
            parts.add(without_suffix)

        # Add the surname (last token) if it is > 2 characters
        tokens = [t.strip(".,;:()") for t in without_suffix.split() if len(t.strip(".,;:()")) > 2]
        if tokens:
            surname = tokens[-1]
            parts.add(surname)

        return parts

    # ------------------------------------------------------------------
    # Public scrub interface
    # ------------------------------------------------------------------

    def scrub_text(self, text: str, judge_names: set[str] | None = None) -> str:
        if judge_names is None:
            judge_names = self._extract_judge_names(text)
        scrubbed_text = text
        for category, pattern in self.patterns.items():
            scrubbed_text = re.sub(pattern, "[REDACTED]", scrubbed_text)

        scrubbed_text = self.scrub_pii_with_nlp(scrubbed_text, judge_names=judge_names)
        return scrubbed_text

    def scrub_pii_with_nlp(self, text: str, judge_names: set[str] | None = None) -> str:
        nlp_doc = nlp(text)
        final_text = text

        org_keywords = [
            # Courts & Locations
            "court", "courts", "division", "high court", "supreme court", "labour court", "land claims court",
            "magistrate", "magistrates", "tribunal", "tribunals", "bar", "chambers", "jurisdiction",
            "johannesburg", "pretoria", "cape town", "durban", "bloemfontein", "gqeberha", "port elizabeth",
            "polokwane", "nelspruit", "mbombela", "mahikeng", "mmabatho", "kimberley", "pietermaritzburg",
            "thohoyandou", "bisho", "grahamstown", "makhanda", "gauteng", "western cape", "eastern cape",
            "kwazulu-natal", "free state", "limpopo", "mpumalanga", "north west", "northern cape",
            "south africa", "saflii", "case", "matter", "appeal", "review", "index", "citation",
            # Corporate / Unions / Govt
            "ltd", "limited", "pty", "proprietary", "inc", "incorporated", "corp", "corporation",
            "gmbh", "plc", "llc", "llp", "holdings", "group", "industries", "services", "enterprises",
            "ventures", "partners", "trust", "bank", "co", "company", "companies",
            "union", "unions", "association", "associations", "federation", "society", "council",
            "committee", "chamber", "coalition", "alliance", "congress", "amcu", "numsa", "num",
            "cosatu", "satawu", "popcru", "nehawu", "denosa", "solidarity", "solidariteit",
            "minister", "mec", "department", "dept", "registrar", "commissioner", "director",
            "judge", "judges", "justice", "justices",
            "state", "government", "president", "governor", "premier", "mayor", "municipality",
            "municipal", "city of", "province", "provincial", "national", "board", "agency",
            "authority", "commission", "office", "bureau", "administration", "protector", "police",
            "sheriff", "school", "university", "college", "clinic", "hospital", "church", "foundation",
            "charity", "club", "institute", "center", "centre", "vs", "versus", "cc", "close corporation",
            "soc", "npc", "npo", "sars", "ccma"
        ]

        protected_names: set[str] = judge_names or set()

        for name in nlp_doc.ents:
            # We ONLY redact natural persons
            if name.label_ != "PERSON":
                continue

            entity_text = name.text.strip()

            # Guard 1: Do NOT redact if it contains numbers, slashes, or special characters (e.g. case numbers "1234/2023", "JR 123/21")
            if re.search(r"[\d/\\]", entity_text):
                continue

            # Guard 2: Skip if entity matches court/location/organization keywords
            normalized = entity_text.lower()
            is_org = any(
                re.search(r"\b" + re.escape(kw) + r"\b", normalized)
                for kw in org_keywords
            )
            if is_org:
                continue

            # Guard 3: Check pre-identified judge or employer/protected names
            is_protected = False
            entity_words = {t.lower().strip(".,;:()") for t in entity_text.split()}
            for pn in protected_names:
                pn_norm = pn.lower().strip()
                if pn_norm == normalized:
                    is_protected = True
                    break
                if " " in pn_norm and pn_norm in normalized:
                    is_protected = True
                    break
                if pn_norm in entity_words:
                    is_protected = True
                    break

            if is_protected:
                continue

            # Guard 4: Fallback surrounding context tokens for judicial title keywords
            preceding_tokens = nlp_doc[max(0, name.start - 3) : name.start]
            following_tokens = nlp_doc[name.end : min(len(nlp_doc), name.end + 3)]
            context_tokens = {t.text.lower() for t in list(preceding_tokens) + list(following_tokens)}
            if context_tokens & self._judicial_titles:
                continue

            final_text = re.sub(r"\b" + re.escape(entity_text) + r"\b", "[REDACTED]", final_text)

        return final_text

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    def scrub(
        self, input_data: Union[str, dict], original_format: str = "txt"
    ) -> Union[str, dict, list[dict]]:
        raw_text = input_data if isinstance(input_data, str) else json.dumps(input_data)
        judge_names = self._extract_judge_names(raw_text)

        if isinstance(input_data, dict):
            protected_names = self._extract_protected_names(input_data) | judge_names
            return self.scrub_dict(input_data, judge_names=judge_names, employer_names=protected_names)

        if original_format == "json":
            scrubbed_data = json.loads(input_data)
            protected_names = self._extract_protected_names(scrubbed_data) | judge_names
            scrubbed_data = self.scrub_dict(scrubbed_data, judge_names=judge_names, employer_names=protected_names)
        elif original_format == "ndjson":
            scrubbed_data = []
            for line in input_data.splitlines():
                parsed = json.loads(line)
                protected_names = self._extract_protected_names(parsed) | judge_names
                scrubbed_data.append(self.scrub_dict(parsed, judge_names=judge_names, employer_names=protected_names))
        elif original_format == "xml":
            root = ET.fromstring(input_data)
            self.scrub_xml(root, judge_names=judge_names)
            scrubbed_data = ET.tostring(root, encoding="unicode")
        else:
            scrubbed_data = self.scrub_text(input_data, judge_names=judge_names)

        return scrubbed_data

    def scrub_dict(
        self,
        data: dict,
        judge_names: set[str] | None = None,
        employer_names: set[str] | None = None,
    ) -> dict:
        if employer_names is None:
            employer_names = self._extract_protected_names(data)

        protected = (judge_names or set()) | (employer_names or set())

        for key, value in data.items():
            if self._is_protected_key(key):
                # Never redact protected metadata / corporate / judicial / case number fields
                continue
            if isinstance(value, dict):
                data[key] = self.scrub_dict(
                    value, judge_names=judge_names, employer_names=employer_names
                )
            elif isinstance(value, list):
                data[key] = [
                    self.scrub_dict(
                        item, judge_names=judge_names, employer_names=employer_names
                    )
                    if isinstance(item, dict)
                    else (
                        self.scrub_text(item, judge_names=protected)
                        if isinstance(item, str)
                        else item
                    )
                    for item in value
                ]
            elif isinstance(value, str):
                data[key] = self.scrub_text(value, judge_names=protected)
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