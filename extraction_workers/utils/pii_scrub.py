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


# Common legal dispute terms, case subjects, and non-person words that spaCy NER might misclassify
LEGAL_DISPUTE_TERMS = {
    "retrenchment", "demarcation", "dismissal", "severance", "misconduct", "incapacity",
    "operational", "requirements", "unfair", "labour", "practice", "dispute", "disputes",
    "arbitration", "bargaining", "council", "strike", "strikes", "lockout", "lockouts",
    "wage", "wages", "salary", "salaries", "transfer", "contract", "employment",
    "section", "schedule", "clause", "act", "bill", "regulation", "regulations",
    "rule", "rules", "substantive", "procedural", "fairness", "reinstatement",
    "compensation", "award", "order", "ruling", "application", "review", "rescission",
    "condonation", "jurisdiction", "jurisdictional", "promotion", "demotion",
    "suspension", "benefit", "benefits", "discrimination", "harassment", "retaliation",
}


class Scrub:
    def __init__(self):
        self.patterns = {}

        self._judicial_titles = {
            "judge", "justice", "magistrate", "acting", "j", "aj", "dcj", "ja", "aja", "jp", "djp", "p", "cj", "eja",
            "judge of the high court", "judge of the supreme court", "judge president", "deputy judge president",
            "chief justice", "coram", "before", "commissioner", "arbitrator",
        }

    # ------------------------------------------------------------------
    # Field Key Classifications
    # ------------------------------------------------------------------

    @staticmethod
    def _is_metadata_key(key: str) -> bool:
        """Structural metadata keys whose scalar values must NEVER be scrubbed."""
        k = key.lower().strip()
        metadata_keys = {
            "court", "forum", "court_location", "division", "jurisdiction",
            "award_number", "case_number", "case_no", "case_num", "case_id", "citation",
            "award_date", "judgment_date", "hearing_date", "hearing_start", "hearing_end",
            "date_modified", "details_scraped_at", "index_scraped_at",
            "detail_url", "preview_image_url", "document_type", "id", "record_type", "type",
            "journal_name", "publisher", "volume", "issue",
            "reason_for_dismissal", "nature_of_dispute", "issue_in_dispute", "subject",
            "category", "status",
        }
        return k in metadata_keys

    @staticmethod
    def _is_org_key(key: str) -> bool:
        """Keys representing companies, employers, unions, or organizations."""
        k = key.lower().strip()
        if "employer" in k:
            return True
        org_keys = {
            "company", "company_name", "organisation", "organisation_name",
            "organization", "organization_name", "firm", "firm_name", "employer_name",
            "union", "bargaining_council",
        }
        return k in org_keys

    @staticmethod
    def _is_judicial_key(key: str) -> bool:
        """Keys representing judges, justices, magistrates, or arbitrators."""
        k = key.lower().strip()
        judge_keys = {
            "judge", "judges", "coram", "bench", "presiding_judge", "author_judge",
            "hearing_judge", "magistrate", "justice", "arbitrator", "commissioner",
        }
        return k in judge_keys

    @staticmethod
    def _is_person_key(key: str) -> bool:
        """Keys representing individual natural persons."""
        k = key.lower().strip()
        person_keys = {
            "employee", "employee_name", "applicant_employee", "plaintiff_individual",
            "complainant", "person", "individual",
        }
        return k in person_keys

    @staticmethod
    def _is_protected_key(key: str) -> bool:
        """Backwards compatibility helper."""
        return Scrub._is_metadata_key(key) or Scrub._is_org_key(key) or Scrub._is_judicial_key(key)

    @staticmethod
    def _is_org_name(val: str) -> bool:
        """Returns True if the string looks like an organisation / corporate / union name."""
        v = val.lower()
        org_indicators = [
            "ltd", "limited", "pty", "proprietary", "inc", "corp", "cc", "gmbh", "plc", "llc",
            "union", "numsa", "amcu", "cosatu", "satawu", "popcru", "nehawu", "denosa", "solidarity",
            "association", "society", "council", "board", "bank", "trust", "fund", "dept", "department",
            "minister", "mec", "municipality", "city of", "government", "sars", "ccma",
        ]
        return any(re.search(r"\b" + re.escape(kw) + r"\b", v) for kw in org_indicators)

    # ------------------------------------------------------------------
    # Data Analysis: Extract Natural Persons, Orgs, Judges from Struct
    # ------------------------------------------------------------------

    def _extract_dict_entities(self, data: Union[dict, list, str]) -> tuple[set[str], set[str], set[str]]:
        """
        Walks structured data and extracts:
        - person_names: natural persons' names (e.g. employee: "Pretorius")
        - org_names: business/employer/union names
        - judge_names: judge/arbitrator names
        """
        person_names: set[str] = set()
        org_names: set[str] = set()
        judge_names: set[str] = set()

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value.strip():
                    val = value.strip()
                    if self._is_person_key(key):
                        if not self._is_org_name(val):
                            person_names.add(val)
                    elif self._is_org_key(key):
                        org_names.update(self._tokenise_name(val))
                    elif self._is_judicial_key(key):
                        judge_names.update(self._tokenise_name(val))
                    elif key.lower() in {"applicant", "applicant_plaintiff", "respondent", "respondent_defendant"}:
                        if self._is_org_name(val):
                            org_names.update(self._tokenise_name(val))
                        else:
                            person_names.add(val)
                elif isinstance(value, (dict, list)):
                    p, o, j = self._extract_dict_entities(value)
                    person_names.update(p)
                    org_names.update(o)
                    judge_names.update(j)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    p, o, j = self._extract_dict_entities(item)
                    person_names.update(p)
                    org_names.update(o)
                    judge_names.update(j)

        return person_names, org_names, judge_names

    # ------------------------------------------------------------------
    # Judge-name pre-identification from raw text
    # ------------------------------------------------------------------

    def _extract_judge_names(self, text: str) -> set[str]:
        judge_names: set[str] = set()

        judicial_suffix = r"(?:AJ|DCJ|JA|AJA|JP|DJP|CJ|EJA|P|J)\b"
        pattern_suffix = re.compile(
            r"\b([A-Z][a-zA-Z']*(?:\s+(?:van|der|den|de|du|la|le|von)\s+[A-Z][a-zA-Z']*|\s+[A-Z][a-zA-Z']*)*|\b[A-Z]{1,3}(?:\s+[A-Z]{2,})+|[A-Z]{2,})\s+" + judicial_suffix,
            re.MULTILINE,
        )
        for m in pattern_suffix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(0).strip()))

        title_prefix = r"(?:Acting\s+)?(?:Judge|Justice|Magistrate|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President)"
        pattern_prefix = re.compile(
            r"\b" + title_prefix + r"\s+([A-Z][a-z]+(?:\s+(?:van|der|den|de|du|la|le|von)\s+[A-Z][a-z]+)?(?:\s+[A-Z][a-z]+)*)\b"
        )
        for m in pattern_prefix.finditer(text):
            judge_names.update(self._tokenise_name(m.group(1).strip()))

        return judge_names

    @staticmethod
    def _tokenise_name(name: str) -> set[str]:
        clean_name = name.strip()
        parts: set[str] = {clean_name}

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

        tokens = [t.strip(".,;:()") for t in without_suffix.split() if len(t.strip(".,;:()")) > 2]
        if tokens:
            parts.add(tokens[-1])  # surname

        return parts

    # ------------------------------------------------------------------
    # Public scrub interface & NLP
    # ------------------------------------------------------------------

    def scrub_text(
        self,
        text: str,
        person_names: set[str] | None = None,
        judge_names: set[str] | None = None,
        org_names: set[str] | None = None,
    ) -> str:
        if judge_names is None:
            judge_names = self._extract_judge_names(text)

        scrubbed_text = text

        # Step 1: Explicitly redact any target person names known from structured fields
        if person_names:
            for name in person_names:
                if name and len(name.strip()) > 1:
                    scrubbed_text = re.sub(r"\b" + re.escape(name.strip()) + r"\b", "[REDACTED]", scrubbed_text)

        # Step 2: Run NLP for general text person entities
        scrubbed_text = self.scrub_pii_with_nlp(
            scrubbed_text,
            person_names=person_names,
            judge_names=judge_names,
            org_names=org_names,
        )
        return scrubbed_text

    def scrub_pii_with_nlp(
        self,
        text: str,
        person_names: set[str] | None = None,
        judge_names: set[str] | None = None,
        org_names: set[str] | None = None,
    ) -> str:
        nlp_doc = nlp(text)
        final_text = text

        org_keywords = [
            "court", "courts", "division", "high court", "supreme court", "labour court", "land claims court",
            "magistrate", "magistrates", "tribunal", "tribunals", "bar", "chambers", "jurisdiction",
            "johannesburg", "pretoria", "cape town", "durban", "bloemfontein", "gqeberha", "port elizabeth",
            "polokwane", "nelspruit", "mbombela", "mahikeng", "mmabatho", "kimberley", "pietermaritzburg",
            "thohoyandou", "bisho", "grahamstown", "makhanda", "gauteng", "western cape", "eastern cape",
            "kwazulu-natal", "free state", "limpopo", "mpumalanga", "north west", "northern cape",
            "south africa", "saflii", "case", "matter", "appeal", "review", "index", "citation",
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

        protected_names: set[str] = (judge_names or set()) | (org_names or set())

        for name in nlp_doc.ents:
            if name.label_ != "PERSON":
                continue

            entity_text = name.text.strip()

            # Guard 1: Do NOT redact if it contains numbers, slashes, or symbols
            if re.search(r"[\d/\\]", entity_text):
                continue

            normalized = entity_text.lower()

            # Guard 2: Skip legal dispute terms / case subjects
            if normalized in LEGAL_DISPUTE_TERMS:
                continue

            # Guard 3: Skip if entity matches court/location/organization keywords
            is_org = any(
                re.search(r"\b" + re.escape(kw) + r"\b", normalized)
                for kw in org_keywords
            )
            if is_org:
                continue

            # Guard 4: Check pre-identified judge or employer/protected names
            is_protected = False
            entity_words = {t.lower().strip(".,;:()") for t in entity_text.split()}
            for pn in protected_names:
                pn_norm = pn.lower().strip()
                if pn_norm == normalized or (" " in pn_norm and pn_norm in normalized) or pn_norm in entity_words:
                    is_protected = True
                    break

            if is_protected:
                continue

            # Guard 5: Context tokens for judicial titles
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
        if isinstance(input_data, dict):
            p, o, j = self._extract_dict_entities(input_data)
            raw_text = json.dumps(input_data)
            j.update(self._extract_judge_names(raw_text))
            return self.scrub_dict(input_data, person_names=p, judge_names=j, org_names=o)

        if original_format == "json":
            scrubbed_data = json.loads(input_data)
            if isinstance(scrubbed_data, dict):
                p, o, j = self._extract_dict_entities(scrubbed_data)
                j.update(self._extract_judge_names(input_data))
                return self.scrub_dict(scrubbed_data, person_names=p, judge_names=j, org_names=o)

        judge_names = self._extract_judge_names(input_data)
        return self.scrub_text(input_data, judge_names=judge_names)

    def scrub_dict(
        self,
        data: dict,
        person_names: set[str] | None = None,
        judge_names: set[str] | None = None,
        org_names: set[str] | None = None,
    ) -> dict:
        if person_names is None or org_names is None or judge_names is None:
            p, o, j = self._extract_dict_entities(data)
            person_names = (person_names or set()) | p
            org_names = (org_names or set()) | o
            judge_names = (judge_names or set()) | j

        for key, value in data.items():
            if self._is_metadata_key(key) or self._is_org_key(key) or self._is_judicial_key(key):
                # Structural metadata, org/employer names, and judge names are NEVER scrubbed
                continue

            if self._is_person_key(key):
                # Person name fields (e.g. employee: "Pretorius") are scrubbed directly
                if isinstance(value, str) and value.strip():
                    data[key] = "[REDACTED]"
                continue

            if isinstance(value, dict):
                data[key] = self.scrub_dict(
                    value, person_names=person_names, judge_names=judge_names, org_names=org_names
                )
            elif isinstance(value, list):
                data[key] = [
                    self.scrub_dict(
                        item, person_names=person_names, judge_names=judge_names, org_names=org_names
                    )
                    if isinstance(item, dict)
                    else (
                        self.scrub_text(item, person_names=person_names, judge_names=judge_names, org_names=org_names)
                        if isinstance(item, str)
                        else item
                    )
                    for item in value
                ]
            elif isinstance(value, str):
                data[key] = self.scrub_text(
                    value, person_names=person_names, judge_names=judge_names, org_names=org_names
                )
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