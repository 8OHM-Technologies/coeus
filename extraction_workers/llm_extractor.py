"""
Coeus LLM Extractor
===================
Reads detailed scraped documents from the PostgreSQL `extracted_records` table,
sends the text content (e.g., `center_content`) to an OpenAI-compatible API
(e.g., local Ollama instance) to extract structured fields using a Pydantic schema,
scrubs/pseudonymizes PII according to POPIA compliance guidelines, and writes the
validated structured results into the `scrubbed_records` table.

Expected database records (output by the scraper):
    `extracted_records` table with status = 'detailed'

Environment variables:
    PIPELINE_NAME           – pipeline identifier (corresponds to record_type)
    AI_MODEL                – Ollama model string, e.g., "Qwen/Qwen2.5-1.5B-Instruct"
    EXTRACTION_INSTRUCTIONS – free-text LLM instructions
    POSTGRES_HOST / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_PORT
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import sys
import uuid
import time
from datetime import date, datetime
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ValidationError

try:
    from .db import get_db_connection
    from .utils.utils import fetch_pipeline_config
except ImportError:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from db import get_db_connection
    from utils.utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# Compile regex patterns for post-processing PII scrubbing
# RSA ID: 13-digit number
RSA_ID_REGEX = re.compile(r"\b\d{13}\b")
# Passport: letter followed by 8 digits (standard South African passport)
PASSPORT_REGEX = re.compile(r"\b[A-Za-z]\d{8}\b")
# Bank / Tax: sequence of 10 to 16 digits (excludes dates with hyphens, case numbers with slashes)
BANK_TAX_REGEX = re.compile(r"\b\d{10,16}\b")


def get_llm_client() -> OpenAI:
    return OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")


def resolve_schema(schema_name: str) -> type[BaseModel] | None:
    """
    Dynamically imports ``schemas.py`` and returns the class matching *schema_name*.
    Returns ``None`` when not found so the caller can fall back to the generic schema.
    """
    if not schema_name:
        return None
    try:
        if __package__:
            schemas_module = importlib.import_module(".schemas", package=__package__)
        else:
            schemas_module = importlib.import_module("schemas")
        schema_cls = getattr(schemas_module, schema_name, None)
        if schema_cls is None:
            logger.warning(
                "Schema class '%s' not found in schemas.py. "
                "Falling back to GenericDocumentExtraction.",
                schema_name,
            )
        return schema_cls
    except ImportError as exc:
        logger.error("Could not import schemas module: %s", exc)
        return None


def format_journal_text(raw_text: str) -> str:
    """
    Format raw journal/gazette text by finding the "Summary" or "Abstract" marker,
    stripping everything before it, and reconstructs paragraph structures and headings.
    Also inlines standalone footnote numbers back to their preceding sentence.
    """
    # 1. Normalize carriage returns and spacing
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    
    # 2. Find the word "Summary" (case-insensitive) as a whole word/line
    match = re.search(r"\b(summary|abstract)\b", raw_text, re.IGNORECASE)
    if match:
        text_to_process = raw_text[match.start():]
    else:
        text_to_process = raw_text
        
    lines = text_to_process.split("\n")
    cleaned_lines = []
    for line in lines:
        cleaned_lines.append(line.strip())
        
    # Reconstruct paragraphs and headings
    formatted_blocks = []
    current_paragraph = []
    
    heading_regex = re.compile(r"^(\d+(\.\d+)*\s+[A-Za-z0-9]|[A-Z\s]{4,50}$)")
    footnote_regex = re.compile(r"^\d+$") # standalone number
    
    i = 0
    while i < len(cleaned_lines):
        line = cleaned_lines[i]
        if not line:
            if current_paragraph:
                formatted_blocks.append(" ".join(current_paragraph))
                current_paragraph = []
            i += 1
            continue
            
        # If it's a footnote marker on its own line
        if footnote_regex.match(line):
            if current_paragraph:
                # Add it as a footnote superscript to the last word of the paragraph
                current_paragraph[-1] = f"{current_paragraph[-1]}[{line}]"
            else:
                formatted_blocks.append(f"[{line}]")
            i += 1
            continue
            
        # Check if this line is a heading
        if heading_regex.match(line) and len(line) < 100:
            if current_paragraph:
                formatted_blocks.append(" ".join(current_paragraph))
                current_paragraph = []
            
            # Formatting heading
            parts = line.split()
            if parts and re.match(r"^\d+", parts[0]):
                dots = parts[0].count('.')
                prefix = "#" * (dots + 1)
                formatted_blocks.append(f"{prefix} {line}")
            else:
                formatted_blocks.append(f"## {line}")
            i += 1
            continue
            
        current_paragraph.append(line)
        i += 1
        
    if current_paragraph:
        formatted_blocks.append(" ".join(current_paragraph))
        
    result_text = "\n\n".join(formatted_blocks)
    result_text = re.sub(r" +", " ", result_text)
    result_text = re.sub(r"\n{3,}", "\n\n", result_text)
    return result_text.strip()


def parse_court_roll(text: str) -> list[dict[str, Any]]:
    """
    Programmatically parse SAFLII Court Roll text into column rows.
    """
    rows = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    
    # Matches case numbers like 265/2016 (no/year) or 2012/14934 (year/no)
    case_num_pattern = re.compile(r"\b(\d{1,5}/\d{4}|\d{4}/\d{1,5})\b")
    
    i = 0
    while i < len(lines):
        line = lines[i]
        match = case_num_pattern.search(line)
        if match:
            case_no = match.group(1)
            
            # Smart party/details detection
            parties = ""
            parts_after = line.split(case_no, 1)[1].strip()
            parts_before = line.split(case_no, 1)[0].strip()
            
            # Check if current line contains parties
            if " vs " in line.lower() or " v " in line.lower() or " vs. " in line.lower() or " vs" in line.lower():
                parties = parts_after if len(parts_after) > len(parts_before) else parts_before
            
            # Otherwise, check surrounding lines
            if not parties:
                if i > 0 and (" vs " in lines[i-1].lower() or " v " in lines[i-1].lower() or " vs. " in lines[i-1].lower() or " vs" in lines[i-1].lower()):
                    parties = lines[i-1]
                elif i + 1 < len(lines) and (" vs " in lines[i+1].lower() or " v " in lines[i+1].lower() or " vs. " in lines[i+1].lower() or " vs" in lines[i+1].lower()):
                    parties = lines[i+1]
                    
            # Fallback to the next line or current line parts
            if not parties:
                parties = parts_after or parts_before or (lines[i+1] if i + 1 < len(lines) else "")
            
            # Clean up and extract sequence prefixes
            seq = ""
            if parties:
                seq_match = re.match(r"^([A-Za-z0-9\.\-\/]+)\.?\s+", parties)
                if seq_match and len(seq_match.group(1)) < 8:
                    seq = seq_match.group(1).rstrip(".")
                    parties = parties[seq_match.end():].strip()
            
            # Determine status if next line is OK or short status string
            status = ""
            if i + 1 < len(lines) and (lines[i+1].upper() in ("OK", "NOT OK", "FILING ROOM", "STAMPED") or len(lines[i+1]) < 10):
                # Ensure it's not the next seq/case number
                if not re.match(r"^\d+$", lines[i+1]) and not case_num_pattern.search(lines[i+1]):
                    # If we used next line as parties, don't consume it as status
                    if parties != lines[i+1]:
                        status = lines[i+1]
                
            rows.append({
                "column_1": case_no,
                "column_2": parties or "Unknown Parties",
                "column_3": seq or None,
                "column_4": status or None
            })
            i += 1
            continue
            
        if "BEFORE THE" in line.upper():
            court_room = ""
            judge = line
            prev_short = ""
            if i > 0 and len(lines[i-1]) < 15:
                prev_short = lines[i-1]
            next_court = ""
            if i + 1 < len(lines) and "COURT" in lines[i+1].upper():
                i += 1
                next_court = lines[i]
                
            rows.append({
                "column_1": prev_short or "Allocation",
                "column_2": judge,
                "column_3": next_court or None,
                "column_4": None
            })
            
        i += 1
        
    return rows


def smart_truncate_court_judgment(text: str) -> str:
    """
    Keeps the first 8,000 characters of the judgment (metadata + intro).
    If the final order is at the bottom (i.e. not found at the top),
    scans and appends the final order/conclusion block from the end of the document
    (up to 3,000 characters) if it's a long document.
    This prevents hallucination and context window overflow in local LLMs.
    """
    if not text or len(text) <= 10000:
        return text

    head = text[:8000]
    
    # Check if the order is already in the head (typically for Constitutional Court cases)
    # Look for 'ORDER' or 'COURT ORDER' as standalone lines/headers
    has_order_at_top = bool(re.search(r"\b(?:court\s+)?order\b", head, re.IGNORECASE))
    
    if has_order_at_top:
        # If the order is at the top, we don't need to search the tail for an order block
        return head

    # Otherwise, the order is likely at the bottom. Scan the last 12,000 characters.
    tail_text = text[-12000:]
    order_patterns = [
        r"\b(?:court\s+)?order\b",
        r"\bconclusion\b",
        r"\bthe\s+following\s+order\s+is\s+made\b"
    ]
    
    best_idx = -1
    for pattern in order_patterns:
        matches = list(re.finditer(pattern, tail_text, re.IGNORECASE))
        if matches:
            idx = matches[-1].start()
            if idx > best_idx:
                best_idx = idx
                
    if best_idx != -1:
        order_section = tail_text[best_idx:best_idx + 3000]
        # Clean up by removing standard footnotes at the very end
        footnote_start = order_section.find("\n[1]")
        if footnote_start == -1:
            footnote_start = order_section.find("\n[50]")
        if footnote_start != -1:
            order_section = order_section[:footnote_start]
            
        return f"{head}\n\n... [TRUNCATED FOR BREVITY] ...\n\n=== ORDER / CONCLUSION SECTION ===\n{order_section}"
    else:
        return f"{head}\n\n... [TRUNCATED FOR BREVITY] ...\n\n=== END OF DOCUMENT ===\n{text[-2000:]}"


def build_system_prompt(
    schema_cls: type[BaseModel],
    extraction_instructions: str,
) -> str:
    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
    field_names = list(schema_cls.model_fields.keys())
    
    base = (
        f"Nonce: {time.time()}\n"
        "You are a precise data extraction engine. \n"
        "Extract legal information into valid JSON.\n\n"
        "STRICT CRITICAL RULES:\n"
        "- Do NOT introduce generic section titles like 'Introduction', 'Background', or 'Body' as root keys.\n"
        "- Do not include markdown code blocks or explanatory text.\n"
        "- If a field value is missing or unknown, set it to null or an empty array.\n"
        f"- Your JSON object MUST contain the following root keys: {', '.join(field_names)}.\n"
        "- Respond ONLY with a valid JSON object strictly adhering to the schema keys above and the JSON schema below.\n\n"
        f"JSON SCHEMA:\n{schema_json}"
    )
    
    # Add specialized extraction instructions for court case extractions to avoid hallucinations
    if schema_cls.__name__ == "SafliiExtractedData":
        base += (
            "\n\nSAFLII EXTRACTION GUIDELINES:\n"
            "- 'applicant_plaintiff': Extract the exact names of the applicants/plaintiffs (e.g. Cishahayo Saidi or Saidi and Others).\n"
            "- 'respondent_defendant': Extract the exact names of the respondents/defendants (e.g. Minister of Home Affairs).\n"
            "- 'hearing_date': This is the date the judgment was DECIDED or DELIVERED (look for 'Decided on', 'Date of Judgment', or 'Judgment Delivered'). Always use YYYY-MM-DD format.\n"
            "- 'court': Identify the exact court from the document header (e.g., 'Constitutional Court of South Africa', 'Supreme Court of Appeal', or 'Western Cape Division, Cape Town'). Note the citation prefix (ZACC = Constitutional Court, ZASCA = Supreme Court of Appeal).\n"
            "- 'judges': Look under 'Coram:' or 'Judges:' or check the authors of the judgments listed at the top. Extract the actual names of the judges presiding over the case. Do NOT hallucinate names not present in the text.\n"
            "- 'court_location': The location/city of the court (e.g. Johannesburg, Cape Town, Bloemfontein).\n"
            "- 'result': A concise summary of the court's final order.\n"
            "- 'summary': Summarize the legal issue, arguments, and majority reasoning. Do not focus on dissenting opinions unless specifically relevant, and represent the majority decision as the holding of the court. Note that if the court ruled that an action is obligatory/mandatory, make sure the summary reflects that it is an obligation, not a discretion.\n"
        )

    if extraction_instructions:
        base += f"\n\nADDITIONAL INSTRUCTIONS:\n{extraction_instructions}"
    return base


def call_ollama(
    client: OpenAI,
    model: str,
    system_prompt: str,
    document_text: str,
    schema_cls: type[BaseModel] | None = None,
) -> dict[str, Any] | None:
    """
    Call the Ollama OpenAI-compatible endpoint and return the parsed JSON dict,
    or ``None`` on failure.
    """
    ollama_model = model.removeprefix("ollama/")

    response_format: dict[str, Any] = {"type": "json_object"}
    if schema_cls:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_cls.__name__,
                "strict": True,
                "schema": schema_cls.model_json_schema(),
            },
        }

    try:
        response = client.chat.completions.create(
            model=ollama_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Extract structured data from the following document:\n\n"
                        f"{document_text}"
                    ),
                },
            ],
            response_format=response_format,
            temperature=0.1,
            top_p=0.05,
            max_completion_tokens=4096,
            extra_body={
                "options": {
                    "num_ctx": 8192,
                },
                "keep_alive": 0
            }
        )
        raw = response.choices[0].message.content
        return json.loads(str(raw))
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return None


def regex_scrub_text(text: str) -> str:
    """Apply regex-based search-and-replace to scrub sensitive IDs and account numbers."""
    if not text:
        return text
    text = RSA_ID_REGEX.sub("[RSA ID]", text)
    text = PASSPORT_REGEX.sub("[PASSPORT]", text)
    text = BANK_TAX_REGEX.sub("[BANK/TAX NUMBER]", text)
    return text


def scrub_pii_data(data: Any) -> Any:
    """Recursively scrub string values inside dictionaries and lists."""
    if isinstance(data, str):
        return regex_scrub_text(data)
    elif isinstance(data, dict):
        return {k: scrub_pii_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [scrub_pii_data(item) for item in data]
    return data


async def process_records(
    conn,
    records,
    schema_cls,
    pipeline_name,
    client,
    ai_model,
    content_field,
    extraction_instructions,
) -> tuple[int, int, list[uuid.UUID]]:
    success_count = 0
    failure_count = 0
    failed_ids = []
    
    for idx, record in enumerate(records, start=1):
        record_id = record["id"]
        source_url = record["source_url"] or ""
        logger.info("[%d/%d] Extracting record ID: %s (URL: %s)", idx, len(records), record_id, source_url)

        # 1. Parse JSON data from database
        raw_data = record["data"]
        if isinstance(raw_data, str):
            try:
                record_data = json.loads(raw_data)
            except Exception as exc:
                logger.error("  [!] Failed to parse record %s data as JSON: %s", record_id, exc)
                failure_count += 1
                failed_ids.append(record_id)
                continue
        else:
            record_data = dict(raw_data) if raw_data else {}

        # 2. Determine schema class dynamically
        current_schema_cls = schema_cls
        category = None
        if "saflii" in pipeline_name.lower():
            category = record_data.get("category")
            if not category and source_url:
                import urllib.parse
                parsed_path = [p for p in urllib.parse.urlparse(source_url).path.split("/") if p]
                for i, part in enumerate(parsed_path):
                    if part == "za" and i + 1 < len(parsed_path):
                        if parsed_path[i+1] in ("cases", "gaz", "journals", "other"):
                            category = parsed_path[i+1]
                            break
            
            if category == "cases":
                current_schema_cls = resolve_schema("SafliiExtractedData") or schema_cls
            elif category in ("gaz", "journals"):
                current_schema_cls = resolve_schema("SafliiJournalGazetteExtraction") or schema_cls
            elif category == "other":
                current_schema_cls = resolve_schema("SafliiCourtRollExtraction") or schema_cls

        logger.info("  Using schema for extraction: %s (Category: %s)", current_schema_cls.__name__, category if "saflii" in pipeline_name.lower() else "N/A")

        # 3. Extract content for LLM
        if content_field and content_field in record_data:
            doc_text = str(record_data[content_field])
        else:
            doc_text = record_data.get("full_text") or record_data.get("center_content") or ""

        if not doc_text.strip():
            logger.warning("  [!] Empty content for record %s, skipping.", record_id)
            failure_count += 1
            failed_ids.append(record_id)
            continue

        # Apply smart truncation for SAFLII case extraction to avoid context window overflow & hallucinations
        if current_schema_cls.__name__ == "SafliiExtractedData":
            doc_text = smart_truncate_court_judgment(doc_text)

        # Check if this record should be processed programmatically (bypassing AI model)
        is_programmatic = False
        schema_instance = None
        
        if "saflii" in pipeline_name.lower():
            if category in ("gaz", "journals"):
                is_programmatic = True
                try:
                    from schemas import SafliiJournalGazetteExtraction, BaseExtractedRecord, DataQualityFlags
                    
                    formatted = format_journal_text(doc_text)
                    
                    doc_date = None
                    year = record_data.get("year")
                    if year:
                        try:
                            doc_date = date(int(year), 1, 1)
                        except Exception:
                            pass
                    if not doc_date:
                        doc_date = date.today()
                        
                    metadata = BaseExtractedRecord(
                        entity_name=str(record_data.get("dataset") or "SAFLII"),
                        target_name=str(record_data.get("citation") or record_data.get("case_number") or "SAFLII Document"),
                        document_date=doc_date,
                        record_type=str(record_data.get("document_type") or record_data.get("category") or "journals"),
                    )
                            
                    schema_instance = SafliiJournalGazetteExtraction(
                        metadata=metadata,
                        title=str(record_data.get("title") or record_data.get("case_name") or "SAFLII Document"),
                        formatted_text=formatted,
                        data_quality_flags=DataQualityFlags(
                            requires_human_review=bool(record_data.get("requires_human_review") or False)
                        )
                    )
                except Exception as exc:
                    logger.error("  [!] Programmatic extraction failed for journal/gazette %s: %s", record_id, exc)
                    failure_count += 1
                    failed_ids.append(record_id)
                    continue
                    
            elif category == "other":
                is_programmatic = True
                try:
                    from schemas import SafliiCourtRollExtraction, SafliiCourtRollRow, BaseExtractedRecord, DataQualityFlags
                    
                    parsed_rows_data = parse_court_roll(doc_text)
                    rows_objs = []
                    for r in parsed_rows_data:
                        rows_objs.append(SafliiCourtRollRow(**r))
                        
                    doc_date = None
                    year = record_data.get("year")
                    if year:
                        try:
                            doc_date = date(int(year), 1, 1)
                        except Exception:
                            pass
                    if not doc_date:
                        doc_date = date.today()
                        
                    metadata = BaseExtractedRecord(
                        entity_name=str(record_data.get("dataset") or "SAFLII"),
                        target_name=str(record_data.get("citation") or record_data.get("case_number") or "SAFLII Court Roll"),
                        document_date=doc_date,
                        record_type=str(record_data.get("document_type") or record_data.get("category") or "other"),
                    )
                            
                    schema_instance = SafliiCourtRollExtraction(
                        metadata=metadata,
                        rows=rows_objs,
                        data_quality_flags=DataQualityFlags(
                            requires_human_review=bool(record_data.get("requires_human_review") or False)
                        )
                    )
                except Exception as exc:
                    logger.error("  [!] Programmatic extraction failed for court roll %s: %s", record_id, exc)
                    failure_count += 1
                    failed_ids.append(record_id)
                    continue

        if not is_programmatic:
            # 4. Build system prompt dynamically for the selected schema
            current_system_prompt = build_system_prompt(current_schema_cls, extraction_instructions)

            # 5. Call LLM
            raw_result = call_ollama(client, ai_model, current_system_prompt, doc_text, current_schema_cls)
            if raw_result is None:
                logger.error("  [!] LLM returned no result for record: %s", record_id)
                failure_count += 1
                failed_ids.append(record_id)
                continue

            # 6. Validate against Pydantic schema
            try:
                schema_instance = current_schema_cls.model_validate(raw_result)
            except ValidationError as exc:
                logger.error("  [!] Schema validation failed for record %s:\n%s", record_id, exc)
                failure_count += 1
                failed_ids.append(record_id)
                continue

            # Wrap SafliiExtractedData in SafliiCaseExtraction outer schema
            if current_schema_cls.__name__ == "SafliiExtractedData":
                try:
                    from schemas import SafliiCaseExtraction, BaseExtractedRecord, DataQualityFlags
                    
                    doc_date = schema_instance.hearing_date
                    metadata = BaseExtractedRecord(
                        entity_name=str(record_data.get("dataset") or "SAFLII"),
                        target_name=str(record_data.get("citation") or record_data.get("case_number") or "SAFLII Court Case"),
                        document_date=doc_date,
                        record_type=str(record_data.get("document_type") or record_data.get("category") or "cases"),
                    )
                    
                    outer_instance = SafliiCaseExtraction(
                        metadata=metadata,
                        title=str(record_data.get("title") or record_data.get("case_name") or "SAFLII Court Case"),
                        extracted_data=schema_instance,
                        data_quality_flags=DataQualityFlags(
                            requires_human_review=bool(record_data.get("requires_human_review") or False)
                        )
                    )
                    schema_instance = outer_instance
                except Exception as exc:
                    logger.error("  [!] Failed to construct SafliiCaseExtraction wrapper for record %s: %s", record_id, exc)
                    failure_count += 1
                    failed_ids.append(record_id)
                    continue

        # 5. Apply regex post-processing PII scrubbing on the output dictionary
        validated_dict = schema_instance.model_dump(mode="json")
        scrubbed_dict = scrub_pii_data(validated_dict)

        # 6. Populate scrubbed_records table and mark extracted_record as cleaned
        try:
            # Insert scrubbed fields directly into scrubbed_records.data
            await conn.execute(
                """
                INSERT INTO scrubbed_records (id, extracted_record_id, data, created_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (extracted_record_id)
                DO UPDATE SET data = EXCLUDED.data
                """,
                uuid.uuid4(),
                record_id,
                json.dumps(scrubbed_dict, ensure_ascii=False),
            )

            # Mark extracted_record as cleaned
            await conn.execute(
                "UPDATE extracted_records SET cleaned_at = NOW() WHERE id = $1",
                record_id,
            )

            success_count += 1
            logger.info("  [+] Successfully extracted and saved record: %s", record_id)

        except Exception as db_exc:
            logger.error("  [!] Database write failed for record %s: %s", record_id, db_exc)
            failure_count += 1
            failed_ids.append(record_id)
            
    return success_count, failure_count, failed_ids


async def run_extraction(
    pipeline_name: str,
    schema_name: str,
    record_id_filter: str | None = None,
) -> None:
    config = await fetch_pipeline_config(pipeline_name)

    extraction_instructions: str = os.getenv("EXTRACTION_INSTRUCTIONS") or config.get(
        "extraction_instructions", ""
    )
    ai_model: str = os.getenv("AI_MODEL") or config.get("engine", "ollama/phi4-mini")
    content_field: str = (config.get("extraction_params") or {}).get("content_field", "")

    logger.info("==================================================")
    logger.info("🚀 COEUS LLM EXTRACTOR INITIALIZED (PIPELINE: %s)", pipeline_name)
    logger.info("   Schema       : %s", schema_name or "GenericDocumentExtraction")
    logger.info("   AI model     : %s", ai_model)
    logger.info("==================================================")

    # -- Resolve Pydantic schema -----------------------------------------------
    if __package__:
        schemas_module = importlib.import_module(".schemas", package=__package__)
    else:
        schemas_module = importlib.import_module("schemas")
    schema_cls: type[BaseModel] = (
        resolve_schema(schema_name)
        or getattr(schemas_module, "GenericDocumentExtraction")
    )
    logger.info("Using schema: %s", schema_cls.__name__)

    # -- Initialise LLM client --------------------------------------------------
    client = get_llm_client()

    # -- Connect to DB ---------------------------------------------------------
    try:
        conn = await get_db_connection()
        logger.info("Connected to PostgreSQL.")
    except Exception as exc:
        logger.error("Could not connect to database: %s. Aborting extraction.", exc)
        sys.exit(1)

    try:
        if record_id_filter:
            # MANUAL TEST MODE: fetch a single record by UUID, ignoring status/scrubbed state.
            logger.info("🔬 MANUAL TEST MODE — targeting single record: %s", record_id_filter)
            try:
                target_uuid = uuid.UUID(record_id_filter)
            except ValueError:
                logger.error("Invalid UUID supplied for --record-id: '%s'", record_id_filter)
                sys.exit(1)

            records = await conn.fetch(
                """
                SELECT e.id, e.data, e.source_url
                FROM extracted_records e
                WHERE e.id = $1
                """,
                target_uuid,
            )

            if not records:
                logger.error(
                    "No extracted_record found with id '%s'. "
                    "Verify the UUID is correct and the record exists in the database.",
                    record_id_filter,
                )
                sys.exit(1)

            success_count, failure_count, _ = await process_records(
                conn, records, schema_cls, pipeline_name, client, ai_model, content_field, extraction_instructions
            )

            logger.info("==================================================")
            logger.info(
                "✅ Extraction complete — %d succeeded, %d failed.",
                success_count,
                failure_count,
            )
            logger.info("==================================================")

            if failure_count > 0 and success_count == 0:
                sys.exit(1)

        else:
            # BATCH MODE: process in batches of 100
            batch_size = 100
            failed_ids = []
            total_success = 0
            total_failure = 0

            logger.info("Starting batch extraction mode (batch size: %d)...", batch_size)

            while True:
                if failed_ids:
                    records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND s.extracted_record_id IS NULL
                          AND NOT (e.id = ANY($2))
                        ORDER BY e.scraped_at ASC
                        LIMIT $3
                        """,
                        pipeline_name,
                        failed_ids,
                        batch_size,
                    )
                else:
                    records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND s.extracted_record_id IS NULL
                        ORDER BY e.scraped_at ASC
                        LIMIT $2
                        """,
                        pipeline_name,
                        batch_size,
                    )

                if not records:
                    logger.info("No more detailed records found needing LLM extraction for pipeline '%s'.", pipeline_name)
                    break

                logger.info("Fetched batch of %d record(s) to extract. Already failed: %d", len(records), len(failed_ids))

                success_count, failure_count, current_failed_ids = await process_records(
                    conn, records, schema_cls, pipeline_name, client, ai_model, content_field, extraction_instructions
                )

                total_success += success_count
                total_failure += failure_count
                failed_ids.extend(current_failed_ids)

            logger.info("==================================================")
            logger.info(
                "✅ Batch extraction complete — %d total succeeded, %d total failed.",
                total_success,
                total_failure,
            )
            logger.info("==================================================")

            if total_failure > 0 and total_success == 0:
                sys.exit(1)

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus LLM Extractor")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The pipeline identifier (used to locate config).",
    )
    parser.add_argument(
        "--schema",
        default="",
        help=(
            "The Pydantic schema class name from schemas.py to validate against. "
            "Defaults to GenericDocumentExtraction."
        ),
    )
    parser.add_argument(
        "--record-id",
        default=None,
        dest="record_id",
        metavar="UUID",
        help=(
            "UUID of a single extracted_record to process. "
            "Bypasses the batch query (status / scrubbed-record filters) so any record "
            "can be tested directly. Useful for manual end-to-end testing of the LLM "
            "extractor without touching DB state."
        ),
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name, args.schema, args.record_id))
