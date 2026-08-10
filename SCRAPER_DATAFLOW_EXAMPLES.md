# 1. SAFLII Pipeline Data Flow

### Step A: Indexed State (`status = 'indexed'`)
Discovered directly from the SAFLII index databases. Stored via `db_storage.upsert_scraped_records_batch`.

```json
{
  "detail_url": "https://www.saflii.org/za/cases/ZACC/2026/1.html",
  "dataset": "ZACC",
  "year": "2026",
  "entry_id": "1",
  "dataset_number": "[2026] ZACC 1"
}
```

### Step B: Detailed State (`status = 'detailed'`)
Workers extract the full text and append category-specific metadata parsed via `extract_metadata_by_category`. Stored via `db_storage.upsert_scraped_record`.

```json
{
  "dataset": "ZACC",
  "year": "2026",
  "entry_id": "1",
  "title": "State v Zuma and Others",
  "url": "https://www.saflii.org/za/cases/ZACC/2026/1.html",
  "document_type": "html",
  "category": "cases",
  "center_content": "<div id=\"center\"><!-- sino index -->...<!-- sino noindex --></div>",
  "full_text": "Constitutional Court of South Africa...\nCase no: CCT 12/25...\nDecided on: 12 June 2026...",
  "scraped_at": "2026-08-09T18:57:25.000Z",
  "worker_id": 1,
  "requires_human_review": false,
  "dataset_number": "CCT 12/25",
  "case_number": "CCT 12/25",
  "citation": "[2026] ZACC 1"
}
```

### Step C: LLM Extracted & Validated Output
The LLM extracts structured fields based on the `SafliiCaseExtraction` schema in [schemas.py](file:///home/tiaanf/Dev/coeus/extraction_workers/schemas.py#L49-L103).

```json
{
  "metadata": {
    "entity_name": "Saflii",
    "target_name": "ZACC",
    "document_date": "2026-06-18",
    "record_type": "saflii_courts"
  },
  "applicant_plaintiff": "State",
  "respondent_defendant": [
    "Zuma",
    "Ministers of Justice"
  ],
  "hearing_date": "2026-06-12",
  "dataset_number": "CCT 12/25",
  "reportable": false,
  "subjects": [
    "Criminal Law",
    "Constitutional Law"
  ],
  "court": "Constitutional Court",
  "judges": [
    "Zondo CJ",
    "Goliath AJ"
  ],
  "court_location": "Johannesburg",
  "result": "Application for leave to appeal dismissed.",
  "summary": "The applicant, ID number 8501015000083, sought urgent relief regarding...",
  "keywords": [
    "leave to appeal",
    "constitutional challenge"
  ],
  "formatted_text": "Constitutional Court of South Africa...\nCase no: CCT 12/25...",
  "data_quality_flags": {
    "requires_human_review": false,
    "review_reason": null
  }
}
```

### Step D: POPIA Scrubbed State (`scrubbed_records`)
The validated JSON passes through regex-based PII scrubbing where identifiers matching patterns like ID numbers are replaced.

```json
{
  "metadata": {
    "entity_name": "Saflii",
    "target_name": "ZACC",
    "document_date": "2026-06-18",
    "record_type": "saflii_courts"
  },
  "applicant_plaintiff": "State",
  "respondent_defendant": [
    "Zuma",
    "Ministers of Justice"
  ],
  "hearing_date": "2026-06-12",
  "dataset_number": "CCT 12/25",
  "reportable": false,
  "subjects": [
    "Criminal Law",
    "Constitutional Law"
  ],
  "court": "Constitutional Court",
  "judges": [
    "Zondo CJ",
    "Goliath AJ"
  ],
  "court_location": "Johannesburg",
  "result": "Application for leave to appeal dismissed.",
  "summary": "The applicant, ID number [RSA ID], sought urgent relief regarding...",
  "keywords": [
    "leave to appeal",
    "constitutional challenge"
  ],
  "formatted_text": "Constitutional Court of South Africa...\nCase no: CCT 12/25...",
  "data_quality_flags": {
    "requires_human_review": false,
    "review_reason": null
  }
}
```

---

# 2. SABINET Pipeline Data Flow

### Step A: Indexed State (`status = 'indexed'`)
Extracted from search listing cards via `_EXTRACT_ITEMS_JS` injection. The tags present on the results cards are parsed and structured dynamically.

```json
{
  "title": "Gumede v Mastercraft",
  "award_date": "2026-06-18",
  "award_number": "KN39790",
  "court": "CCMA",
  "detail_url": "https://discover.sabinet.co.za/document/65a3f12b8cd18b",
  "index_scraped_at": "2026-08-09T18:57:25.000Z"
}
```

### Step B: Detailed State (`status = 'detailed'`)
The detailed extractor navigates to the document page and merges the detailed metadata table parsed via `_EXTRACT_DETAIL_JS`.

```json
{
  "title": "Gumede v Mastercraft",
  "award_date": "2026-06-18",
  "award_number": "KN39790",
  "court": "CCMA",
  "detail_url": "https://discover.sabinet.co.za/document/65a3f12b8cd18b",
  "index_scraped_at": "2026-08-09T18:57:25.000Z",
  "details_scraped_at": "2026-08-09T18:58:30.000Z",
  "auth_ok": true,
  "content_loaded": true,
  "detail_title": "Gumede v Mastercraft, KN39790",
  "arbitrator": "Mnguni [AJ]",
  "employer": "Mastercraft Retail",
  "employee": "Gumede",
  "court_location": "KwaZulu-Natal [Durban]",
  "industry": "Retail Sector",
  "preview_image_url": "https://discover.sabinet.co.za/preview/65a3f12b8cd18b.png",
  "full_text": "Commission for Conciliation, Mediation and Arbitration...\nAward number: KN39790...\nArbitrator: Mnguni [AJ]..."
}
```

### Step C: LLM Extracted & Validated Output
Because Sabinet does not have a custom case schema, it matches the generic schema structure `GenericDocumentExtraction` defined in [schemas.py](file:///home/tiaanf/Dev/coeus/extraction_workers/schemas.py#L39-L48).

```json
{
  "metadata": {
    "entity_name": "CCMA",
    "target_name": "Durban Regional Office",
    "document_date": "2026-06-18",
    "record_type": "Arbitration Award"
  },
  "extracted_data": {
    "employer": "Mastercraft Retail",
    "employee": "Gumede",
    "arbitrator": "Mnguni [AJ]",
    "award_number": "KN39790",
    "court_location": "KwaZulu-Natal [Durban]",
    "reason_for_dismissal": "Misconduct: Employee accused of unauthorized absence",
    "outcome": "Dismissed",
    "costs_order": "No order as to costs"
  },
  "data_quality_flags": {
    "requires_human_review": false,
    "review_reason": null
  }
}
```

### Step D: POPIA Scrubbed State (`scrubbed_records`)
The clean data payload is scrubbed for sensitive IDs/accounts and saved in `scrubbed_records`.

```json
{
  "metadata": {
    "entity_name": "CCMA",
    "target_name": "Durban Regional Office",
    "document_date": "2026-06-18",
    "record_type": "Arbitration Award"
  },
  "extracted_data": {
    "employer": "Mastercraft Retail",
    "employee": "Gumede",
    "arbitrator": "Mnguni [AJ]",
    "award_number": "KN39790",
    "court_location": "KwaZulu-Natal [Durban]",
    "reason_for_dismissal": "Misconduct: Employee accused of unauthorized absence",
    "outcome": "Dismissed",
    "costs_order": "No order as to costs"
  },
  "data_quality_flags": {
    "requires_human_review": false,
    "review_reason": null
  }
}
```