import re
import json
import xml.etree.ElementTree as ET
from typing import Union

try:
    from gliner import GLiNER
except ImportError:
    GLiNER = None


def _load_gliner_model(model_name: str = "knowledgator/gliner-stream-pii-v1.0"):
    """Loads GLiNER model for PII entity detection."""
    if GLiNER is None:
        return None
    try:
        return GLiNER.from_pretrained(model_name)
    except Exception:
        try:
            return GLiNER.from_pretrained("urchade/gliner_base")
        except Exception:
            return None


class Scrub:
    _global_model = None

    def __init__(self, model_name: str = "knowledgator/gliner-stream-pii-v1.0"):
        self.model_name = model_name

        self._judicial_prefixes = re.compile(
            r"\b(?:Judge|Justice|Magistrate|Acting|Commissioner|Arbitrator|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President|Coram|Before)\s*$",
            re.IGNORECASE,
        )
        self._judicial_suffixes = re.compile(
            r"^\s*(?:AJ|DCJ|JA|AJA|JP|DJP|CJ|EJA|P|J)\b",
            re.IGNORECASE,
        )

        self._org_indicators = [
            "ltd", "limited", "pty", "proprietary", "inc", "incorporated", "corp", "corporation", "cc", "gmbh", "plc", "llc", "llp",
            "union", "unions", "numsa", "amcu", "cosatu", "satawu", "popcru", "nehawu", "denosa", "solidarity", "solidariteit",
            "association", "associations", "federation", "society", "council", "committee", "chamber", "coalition", "alliance", "congress",
            "minister", "mec", "department", "dept", "registrar", "commissioner", "director",
            "state", "government", "president", "governor", "premier", "mayor", "municipality", "municipal", "city of", "province",
            "board", "agency", "authority", "commission", "office", "bureau", "administration", "protector", "police", "sheriff",
            "school", "university", "college", "clinic", "hospital", "church", "foundation", "charity", "club", "institute", "center", "centre",
            "vs", "versus", "soc", "npc", "npo", "sars", "ccma", "court", "courts", "division", "high court", "supreme court", "labour court",
            "contracting", "hardware", "plumbing", "contractors", "services", "industries", "holdings", "group", "enterprises", "ventures", "partners", "trust", "bank"
        ]

    @property
    def model(self):
        if Scrub._global_model is None:
            Scrub._global_model = _load_gliner_model(self.model_name)
        return Scrub._global_model

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
            "category", "status", "url", "worker_id", "scraped_at", "year",
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

    def _is_org_name(self, val: str) -> bool:
        """Returns True if string matches corporate/union/org indicators or patterns."""
        v = val.lower().strip()
        return any(re.search(r"\b" + re.escape(kw) + r"\b", v) for kw in self._org_indicators)

    # ------------------------------------------------------------------
    # Data Analysis: Extract Natural Persons, Orgs, Judges from Struct
    # ------------------------------------------------------------------

    def _extract_dict_entities(self, data: Union[dict, list, str]) -> tuple[set[str], set[str], set[str]]:
        """Walks structured data and extracts person_names, org_names, judge_names."""
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

        title_prefix = r"(?:Acting\s+)?(?:Judge|Justice|Magistrate|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President|Commissioner|Arbitrator)"
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
            r"^(?:Judge|Justice|Magistrate|Chief\s+Justice|Judge\s+President|Deputy\s+Judge\s+President|Acting\s+Judge|Acting\s+Justice|Commissioner|Arbitrator)\s+",
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
    # Public scrub interface & GLiNER PII detection
    # ------------------------------------------------------------------

    def scrub_text(
        self,
        text: str,
        person_names: set[str] | None = None,
        judge_names: set[str] | None = None,
        org_names: set[str] | None = None,
        threshold: float = 0.4,
    ) -> str:
        if not text or not text.strip():
            return text

        person_names = person_names or set()
        judge_names = (judge_names or set()) | self._extract_judge_names(text)
        org_names = org_names or set()

        redacted_text = text

        if self.model is not None:
            pii_labels = ["name", "person", "organization", "company"]
            try:
                entities = self.model.predict_entities(text, pii_labels, threshold=threshold)
            except Exception:
                entities = []

            # Extract organization entity spans
            org_spans = [
                (e["start"], e["end"])
                for e in entities
                if e.get("label", "").lower() in ("organization", "company")
            ]

            entities_to_redact = []
            for entity in entities:
                lbl = entity.get("label", "").lower()
                # Rule 2: Company/Union/Organisation names should NEVER be redacted
                if lbl in ("organization", "company"):
                    continue

                if lbl in ("name", "person"):
                    start = entity["start"]
                    end = entity["end"]
                    ent_text = entity.get("text", text[start:end]).strip()

                    # Guard: skip single characters or numbers
                    if len(ent_text) <= 1 or re.search(r"^[\d/\\]+$", ent_text):
                        continue

                    # Rule 1: Judge/Arbitrator names should NEVER be redacted
                    preceding_text = text[max(0, start - 35) : start]
                    if self._judicial_prefixes.search(preceding_text):
                        continue

                    following_text = text[end : min(len(text), end + 20)]
                    if self._judicial_suffixes.search(following_text):
                        continue

                    ent_words = set(ent_text.split())
                    if ent_text in judge_names or any(jn in ent_text or jn in ent_words for jn in judge_names):
                        continue

                    # Rule 2: Company/Union/Organisation names should NEVER be redacted
                    if self._is_org_name(ent_text) or ent_text in org_names:
                        continue

                    # Rule 3: Natural person names redacted - EXCEPT inside org names (e.g., "John's Hardware")
                    inside_org = any(ob <= start and end <= oe for ob, oe in org_spans)
                    if inside_org:
                        continue

                    if re.match(r"^\s*'s\s+(?:Hardware|Plumbing|Contracting|Bakery|Garage|Services|Store|Shop|Market|Cafe|Consulting|Logistics|Engineering|Construction|Holdings|Group|Enterprise|Enterprises|Auto|Motors)\b", following_text, re.IGNORECASE):
                        continue

                    entities_to_redact.append(entity)

            # Sort entities by 'start' index in reverse order (right to left)
            sorted_entities = sorted(entities_to_redact, key=lambda x: x["start"], reverse=True)

            # Splice string to swap text fragments with [REDACTED]
            for entity in sorted_entities:
                start = entity["start"]
                end = entity["end"]
                redacted_text = redacted_text[:start] + "[REDACTED]" + redacted_text[end:]

        # Redact explicit natural person names from structured fields
        if person_names:
            for name in person_names:
                if (
                    name
                    and len(name.strip()) > 1
                    and name not in judge_names
                    and name not in org_names
                    and not self._is_org_name(name)
                ):
                    redacted_text = re.sub(r"\b" + re.escape(name.strip()) + r"\b", "[REDACTED]", redacted_text)

        return redacted_text

    def scrub_pii_with_nlp(
        self,
        text: str,
        person_names: set[str] | None = None,
        judge_names: set[str] | None = None,
        org_names: set[str] | None = None,
    ) -> str:
        return self.scrub_text(text, person_names=person_names, judge_names=judge_names, org_names=org_names)

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
                # Natural person name fields (e.g. employee: "Pretorius") are scrubbed directly
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
    sample_text = "My name is John Smith. Contact me at john.smith@email.com or call 555-123-4567."
    print("Original Text:", sample_text)
    print("Sanitized Output:", pii_scrubber.scrub_text(sample_text))