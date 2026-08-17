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

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from extraction_workers.db import get_db_connection
from extraction_workers.utils.utils import fetch_pipeline_config
from extraction_workers.schemas.generic import BaseExtractedRecord, DataQualityFlags
from extraction_workers.schemas.saflii import (
    SafliiCaseExtraction,
    SafliiExtractedData,
    SafliiHeaderData,
    SafliiPrecedentsData,
    SafliiBodyData,
    SafliiJournalGazetteExtraction,
    SafliiCourtRollExtraction,
)

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
    Dynamically imports ``extraction_workers.schemas`` package and returns the class matching *schema_name*.
    Returns ``None`` when not found so the caller can fall back to the generic schema.
    """
    if not schema_name:
        return None
    try:
        try:
            schemas_module = importlib.import_module("extraction_workers.schemas")
        except ImportError:
            schemas_module = importlib.import_module("schemas")

        schema_cls = getattr(schemas_module, schema_name, None)
        if schema_cls is None:
            logger.warning(
                "Schema class '%s' not found in schemas package. "
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
    footnote_regex = re.compile(r"^\d+$")
    
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


def create_llm_json_schema(schema_cls: type[BaseModel]) -> dict[str, Any]:
    """
    Returns the JSON schema for LLM structured output, filtering out system-only
    BaseExtractedRecord / metadata fields so the AI model never sees or attempts
    to extract system metadata.
    """
    import copy
    schema = copy.deepcopy(schema_cls.model_json_schema())
    
    if "properties" in schema and "metadata" in schema["properties"]:
        del schema["properties"]["metadata"]
    if "required" in schema and "metadata" in schema["required"]:
        schema["required"].remove("metadata")
        
    if "$defs" in schema:
        schema["$defs"].pop("BaseExtractedRecord", None)
        
    return schema


def build_system_prompt(
    schema_cls: type[BaseModel],
    extraction_instructions: str,
) -> str:
    schema_dict = create_llm_json_schema(schema_cls)
    schema_json = json.dumps(schema_dict, indent=2)
    
    base = (
        f"Nonce: {time.time()}\n"
        "You are a precise legal data extraction engine. \n"
        "Extract legal information from the provided document into valid JSON.\n\n"
        "STRICT CRITICAL RULES:\n"
        "- Do NOT introduce generic section titles like 'Introduction', 'Background', or 'Body' as root keys.\n"
        "- Do not include markdown code blocks or explanatory text.\n"
        "- If a field value is missing or unknown, set it to null or an empty array.\n"
        "- Respond ONLY with a valid JSON object strictly adhering to the JSON schema below.\n\n"
        f"JSON SCHEMA:\n{schema_json}"
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
        schema_dict = create_llm_json_schema(schema_cls)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_cls.__name__,
                "strict": True,
                "schema": schema_dict,
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
            max_completion_tokens=8192,
            extra_body={
                "options": {
                    "num_ctx": 16384,
                }
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


def resolve_parser(parser_name: str):
    """
    Dynamically imports and resolves a document section parser function based on parser_name.
    Supported aliases:
      - 'saflii', 'saflii_document_parser', 'saflii_document_parser.py'
      - module paths like 'extraction_workers.utils.saflii_document_parser'
    """
    if not parser_name:
        return None

    clean_name = parser_name.strip().lower()
    if clean_name.endswith(".py"):
        clean_name = clean_name[:-3]

    if clean_name in ("saflii", "saflii_document_parser", "utils.saflii_document_parser"):
        try:
            from .utils.saflii_document_parser import split_saflii_document
            return split_saflii_document
        except ImportError:
            try:
                from utils.saflii_document_parser import split_saflii_document
                return split_saflii_document
            except ImportError as exc:
                logger.error("Could not import split_saflii_document parser: %s", exc)
                return None

    try:
        mod = importlib.import_module(parser_name)
        for fn_name in ("split_saflii_document", "parse_document", "split_document", "parse"):
            if hasattr(mod, fn_name):
                return getattr(mod, fn_name)
    except Exception as exc:
        logger.warning("Could not dynamically load parser module '%s': %s", parser_name, exc)
    return None


def get_record_category(record_data: dict, source_url: str | None = None) -> str | None:
    """Resolves document category (e.g. cases, gaz, journals, other) from record data or source_url."""
    category = record_data.get("category")
    if not category and source_url:
        import urllib.parse
        parsed_path = [p for p in urllib.parse.urlparse(source_url).path.split("/") if p]
        for i, part in enumerate(parsed_path):
            if part == "za" and i + 1 < len(parsed_path):
                if parsed_path[i + 1] in ("cases", "gaz", "journals", "other"):
                    category = parsed_path[i + 1]
                    break
    return category


async def run_parser_phase(
    conn,
    pipeline_name: str,
    parser_func,
    record_id_filter: str | None = None,
    batch_size: int = 250,
) -> int:
    """
    Fetches records from extracted_records that do not have a corresponding parsed_records entry yet,
    runs parser_func on each record in batches (filtering strictly for 'cases' category on SAFLII pipelines),
    populates parsed_records, and flags failed records for human review.
    """
    is_saflii = "saflii" in pipeline_name.lower()

    if record_id_filter:
        records = await conn.fetch(
            """
            SELECT e.id, e.data, e.source_url
            FROM extracted_records e
            WHERE e.id = $1
            """,
            uuid.UUID(record_id_filter),
        )
        if not records:
            return 0
        total_unparsed = len(records)
    else:
        if is_saflii:
            total_unparsed = await conn.fetchval(
                """
                SELECT count(*)
                FROM extracted_records e
                LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                WHERE e.record_type = $1
                  AND e.status = 'detailed'
                  AND (e.requires_human_review IS NOT TRUE)
                  AND (e.source_url LIKE '%/cases/%' OR (e.source_url IS NULL AND e.data->>'category' = 'cases'))
                  AND p.extracted_record_id IS NULL
                """,
                pipeline_name,
            ) or 0
        else:
            total_unparsed = await conn.fetchval(
                """
                SELECT count(*)
                FROM extracted_records e
                LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                WHERE e.record_type = $1
                  AND e.status = 'detailed'
                  AND (e.requires_human_review IS NOT TRUE)
                  AND p.extracted_record_id IS NULL
                """,
                pipeline_name,
            ) or 0

        if total_unparsed == 0:
            logger.info("📄 SECTION PARSER PHASE — All detailed cases are already parsed for pipeline '%s'.", pipeline_name)
            return 0

        logger.info(
            "📄 SECTION PARSER PHASE — Found %d unparsed case record(s) for pipeline '%s'. Processing in batches of %d...",
            total_unparsed,
            pipeline_name,
            batch_size,
        )

    parsed_count = 0
    failed_parse_ids = []

    while True:
        if record_id_filter:
            fetch_records = records
        else:
            if is_saflii:
                if failed_parse_ids:
                    fetch_records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
                          AND (e.source_url LIKE '%/cases/%' OR (e.source_url IS NULL AND e.data->>'category' = 'cases'))
                          AND p.extracted_record_id IS NULL
                          AND NOT (e.id = ANY($2))
                        ORDER BY e.scraped_at ASC
                        LIMIT $3
                        """,
                        pipeline_name,
                        failed_parse_ids,
                        batch_size,
                    )
                else:
                    fetch_records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
                          AND (e.source_url LIKE '%/cases/%' OR (e.source_url IS NULL AND e.data->>'category' = 'cases'))
                          AND p.extracted_record_id IS NULL
                        ORDER BY e.scraped_at ASC
                        LIMIT $2
                        """,
                        pipeline_name,
                        batch_size,
                    )
            else:
                if failed_parse_ids:
                    fetch_records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
                          AND p.extracted_record_id IS NULL
                          AND NOT (e.id = ANY($2))
                        ORDER BY e.scraped_at ASC
                        LIMIT $3
                        """,
                        pipeline_name,
                        failed_parse_ids,
                        batch_size,
                    )
                else:
                    fetch_records = await conn.fetch(
                        """
                        SELECT e.id, e.data, e.source_url
                        FROM extracted_records e
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
                          AND p.extracted_record_id IS NULL
                        ORDER BY e.scraped_at ASC
                        LIMIT $2
                        """,
                        pipeline_name,
                        batch_size,
                    )

        if not fetch_records:
            break

        for record in fetch_records:
            rec_id = record["id"]
            raw_data = record["data"]
            source_url = record.get("source_url") or ""
            if isinstance(raw_data, str):
                try:
                    rec_dict = json.loads(raw_data)
                except Exception:
                    rec_dict = {}
            else:
                rec_dict = dict(raw_data) if raw_data else {}

            category = get_record_category(rec_dict, source_url)
            if is_saflii and category != "cases":
                continue

            full_text = rec_dict.get("full_text") or ""
            center_content = rec_dict.get("center_content") or ""

            if not full_text.strip() and not center_content.strip():
                logger.warning("  [!] Record %s has empty text for parsing.", rec_id)
                parsed_payload = {"null_values": ["header", "judgment", "order"], "error": "Empty text for parsing"}
            else:
                try:
                    parsed_payload = parser_func(full_text, center_content)
                except Exception as exc:
                    logger.error("  [!] Parser function failed on record %s: %s", rec_id, exc)
                    parsed_payload = {"null_values": ["header", "judgment", "order"], "error": str(exc)}

            try:
                await conn.execute(
                    """
                    INSERT INTO parsed_records (id, extracted_record_id, data, created_at, updated_at)
                    VALUES ($1, $2, $3, NOW(), NOW())
                    ON CONFLICT (extracted_record_id)
                    DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                    """,
                    uuid.uuid4(),
                    rec_id,
                    json.dumps(parsed_payload, ensure_ascii=False),
                )

                if parsed_payload.get("null_values"):
                    await conn.execute(
                        """
                        UPDATE extracted_records
                        SET requires_human_review = TRUE,
                            review_reason = 'Document parsing failed',
                            parsed_at = NOW(),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        rec_id,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE extracted_records
                        SET parsed_at = NOW(),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        rec_id,
                    )

                parsed_count += 1
            except Exception as exc:
                logger.error("  [!] Failed to save parsed_record for record %s: %s", rec_id, exc)
                failed_parse_ids.append(rec_id)

        if record_id_filter:
            break

        logger.info(
            "  [+] Parsed %d / %d case record(s) (%.1f%%) in section parser phase...",
            parsed_count,
            total_unparsed,
            (parsed_count / total_unparsed * 100) if total_unparsed > 0 else 100.0,
        )

    logger.info("  [+] Completed section parser phase for %d case record(s).", parsed_count)
    return parsed_count


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

        # 0. Skip if already marked for human review
        if record.get("requires_human_review") or (isinstance(record.get("data"), dict) and record["data"].get("requires_human_review")):
            logger.info("  [i] Record %s is already marked for human review, skipping extraction.", record_id)
            failure_count += 1
            failed_ids.append(record_id)
            continue

        # 1. Parse JSON data and DB metadata from database
        db_entity_name = record.get("db_entity_name")
        db_target_name = record.get("db_target_name")
        db_doc_date = record.get("document_date")
        db_record_type = record.get("record_type") or pipeline_name

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

        if record_data.get("requires_human_review"):
            logger.info("  [i] Record %s is marked for human review in data payload, skipping extraction.", record_id)
            failure_count += 1
            failed_ids.append(record_id)
            continue

        case_number = record_data.get("case_number") or record_data.get("case_no") or None

        # 2. Determine schema class and process non-cases directly
        current_schema_cls = schema_cls
        category = None
        is_programmatic = False
        schema_instance = None

        if "saflii" in pipeline_name.lower():
            category = get_record_category(record_data, source_url)
            
            if category in ("gaz", "journals"):
                formatted_text = str(record_data.get("full_text") or record_data.get("center_content") or "").strip()
                if not formatted_text:
                    logger.warning("  [!] Empty content for journal/gazette %s, skipping.", record_id)
                    failure_count += 1
                    failed_ids.append(record_id)
                    continue

                schema_instance = SafliiJournalGazetteExtraction(
                    title=str(record_data.get("title") or record_data.get("case_name") or "Untitled Document"),
                    formatted_text=formatted_text,
                    data_quality_flags=DataQualityFlags(
                        requires_human_review=bool(record_data.get("requires_human_review") or False)
                    ),
                )
                is_programmatic = True
                logger.info("  [Programmatic Mode] Extracted journal/gazette record: %s (direct full_text, no LLM)", record_id)

            elif category == "other":
                schema_instance = SafliiCourtRollExtraction(
                    title=str(record_data.get("title") or record_data.get("case_name") or "Court Roll"),
                    roll_type=str(record_data.get("roll_type") or "Court Roll"),
                    rows=[],
                    data_quality_flags=DataQualityFlags(
                        requires_human_review=bool(record_data.get("requires_human_review") or False)
                    ),
                )
                is_programmatic = True
                logger.info("  [Programmatic Mode] Extracted court roll record: %s (no LLM)", record_id)

            else:
                current_schema_cls = resolve_schema("SafliiExtractedData") or schema_cls

        if not is_programmatic:
            logger.info("  Using schema for extraction: %s (Category: %s)", current_schema_cls.__name__, category if "saflii" in pipeline_name.lower() else "N/A")

            # 3. Extract content for LLM from parsed_records
            parsed_data = {}
            if "parsed_data" in record:
                raw_pdata = record["parsed_data"]
                if isinstance(raw_pdata, str):
                    try:
                        parsed_data = json.loads(raw_pdata)
                    except Exception:
                        parsed_data = {}
                elif isinstance(raw_pdata, dict):
                    parsed_data = raw_pdata

            if content_field and content_field in parsed_data:
                doc_text = str(parsed_data[content_field])
            else:
                header = parsed_data.get("header") or ""
                judgment = parsed_data.get("judgment") or ""
                order = parsed_data.get("order") or ""
                if judgment or order or header:
                    doc_text = f"{header}\n\n=== JUDGMENT ===\n{judgment}\n\n=== ORDER ===\n{order}".strip()
                else:
                    doc_text = record_data.get("full_text") or record_data.get("center_content") or ""

            if not doc_text.strip():
                logger.warning("  [!] Empty content for record %s, skipping.", record_id)
                failure_count += 1
                failed_ids.append(record_id)
                continue
        
        if not is_programmatic:
            # Multi-Pass Section-Targeted LLM Extraction for SAFLII Cases
            if current_schema_cls.__name__ == "SafliiExtractedData":
                logger.info("  [Section-Targeted LLM Mode] Extracting SafliiExtractedData via section-specific context...")
                
                header_text = (parsed_data.get("header") or "").strip()
                judgment_text = (parsed_data.get("judgment") or "").strip()
                order_text = (parsed_data.get("order") or "").strip()
                appearances_text = (parsed_data.get("appearances") or "").strip()
                raw_citations = parsed_data.get("citations")
                targets_list = []
                if isinstance(raw_citations, dict):
                    targets = raw_citations.get("targets") or []
                    targets_list = [t for t in targets if isinstance(t, dict)]
                    targets_formatted = []
                    for t in targets_list:
                        url = t.get("url") or ""
                        text = t.get("text") or ""
                        targets_formatted.append(f"- Text: {text} | URL: {url}")
                    
                    raw_text = raw_citations.get("raw_text") or ""
                    citations_text = "CITATION TARGETS:\n" + ("\n".join(targets_formatted) if targets_formatted else "None") + "\n\nRAW CITATION TEXT:\n" + str(raw_text)
                elif isinstance(raw_citations, list):
                    citations_text = "\n".join(str(c) for c in raw_citations if c).strip()
                elif isinstance(raw_citations, str):
                    citations_text = raw_citations.strip()
                else:
                    citations_text = ""

                entire_doc_context = f"{header_text}\n\n=== JUDGMENT ===\n{judgment_text}\n\n=== ORDER ===\n{order_text}\n\n=== CITATIONS ===\n{citations_text}".strip()
                if not entire_doc_context:
                    entire_doc_context = doc_text

                # Pass 1: First 8 fields (applicant_plaintiff to court_location) using Header + Judgment Intro (coram/bench) + Appearances context
                header_parts = []
                if header_text:
                    header_parts.append(f"=== HEADER & PARTIES ===\n{header_text}")
                if judgment_text:
                    # Provide first 3000 chars of judgment where authoring judge, coram, and panel concurrences reside
                    header_parts.append(f"=== JUDGMENT INTRO & BENCH (CORAM) ===\n{judgment_text[:3000]}")
                if appearances_text:
                    header_parts.append(f"=== APPEARANCES & COUNSEL ===\n{appearances_text}")

                header_context = "\n\n".join(header_parts).strip()
                if not header_context:
                    header_context = entire_doc_context

                header_instructions = extraction_instructions + (
                    "\nJUDICIAL BENCH EXTRACTION INSTRUCTIONS:\n"
                    "- Extract ALL presiding judges and justices from the Header, Coram, or Judgment Intro (e.g., 'Davis JP', 'Cameron J', 'Chaskalson P', 'Langa DP', 'Moseneke DCJ', 'Rogers AJA', 'Froneman J', 'Madlanga J', 'Jafta J', 'Khampepe J', 'Mogoeng CJ', 'Zondo J').\n"
                    "- Include the authoring judge/justices as well as all concurring members of the court.\n"
                    "- Do NOT extract names of litigants (applicants/respondents), attorneys, advocates, or registrars as judges."
                )
                header_prompt = build_system_prompt(SafliiHeaderData, header_instructions)
                header_res = call_ollama(client, ai_model, header_prompt, header_context, SafliiHeaderData) or {}

                # If LLM completely failed on the header or returned empty result with no essential identifying data
                if not header_res or (not header_res.get("court") and not header_res.get("applicant_plaintiff") and not header_res.get("judgment_date")):
                    logger.warning(
                        "  [!] Failed to extract valid Header fields for record %s. Flagging for human review and skipping.",
                        record_id,
                    )
                    await conn.execute(
                        """
                        UPDATE extracted_records
                        SET requires_human_review = TRUE,
                            review_reason = 'Failed to extract essential Header fields (court/parties/dates missing)',
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        record_id,
                    )
                    failure_count += 1
                    failed_ids.append(record_id)
                    continue

                # Pass 2: precedents_cited using ONLY Citations section context
                citations_context = citations_text if citations_text else entire_doc_context
                precedents_instructions = extraction_instructions + "\nExtract EVERY citation target provided in the CITATION TARGETS list into precedents_cited, matching each target's exact URL and text."
                precedents_prompt = build_system_prompt(SafliiPrecedentsData, precedents_instructions)
                precedents_res = call_ollama(client, ai_model, precedents_prompt, citations_context, SafliiPrecedentsData) or {}

                extracted_precedents = precedents_res.get("precedents_cited") or []
                if not isinstance(extracted_precedents, list):
                    extracted_precedents = []

                cleaned_precedents = []
                for p in extracted_precedents:
                    if isinstance(p, dict):
                        name = p.get("case_name_citation") or p.get("precedent_name") or ""
                        treatment = p.get("treatment") or "Referred"
                        reasoning = p.get("reasoning") or p.get("relevance_summary") or f"Cited in judgment ({name})."
                        url = (p.get("url") or "").strip()

                        if not url and targets_list:
                            for t in targets_list:
                                t_text = (t.get("text") or "").lower()
                                t_url = (t.get("url") or "").strip()
                                if t_text and (t_text in name.lower() or name.lower() in t_text):
                                    url = t_url
                                    break

                        cleaned_precedents.append({
                            "case_name_citation": name if name else "Unspecified Citation",
                            "treatment": treatment,
                            "reasoning": reasoning,
                            "url": url,
                        })

                existing_urls = { (p["url"] or "").strip().lower() for p in cleaned_precedents if p.get("url") }
                existing_names = { (p["case_name_citation"] or "").strip().lower() for p in cleaned_precedents if p.get("case_name_citation") }

                for t in targets_list:
                    t_url = (t.get("url") or "").strip()
                    t_text = (t.get("text") or "").strip()
                    if not t_text and not t_url:
                        continue

                    is_covered = (t_url and t_url.lower() in existing_urls) or (t_text and any(t_text.lower() in name for name in existing_names))
                    if not is_covered:
                        cleaned_precedents.append({
                            "case_name_citation": t_text if t_text else t_url,
                            "treatment": "Referred",
                            "reasoning": f"Cited in judgment ({t_text or t_url}).",
                            "url": t_url,
                        })
                        if t_url:
                            existing_urls.add(t_url.lower())
                        if t_text:
                            existing_names.add(t_text.lower())

                precedents_res["precedents_cited"] = cleaned_precedents

                # Pass 3: Body fields (ratio_decidendi, obiter_dicta, order, summary, keywords) using Judgment & Order text context
                body_parts = []
                if judgment_text:
                    body_parts.append(f"=== JUDGMENT ===\n{judgment_text}")
                if order_text:
                    body_parts.append(f"=== ORDER ===\n{order_text}")
                body_context = "\n\n".join(body_parts).strip()
                if not body_context:
                    body_context = entire_doc_context

                body_instructions = extraction_instructions + (
                    "\nKEYWORDS INSTRUCTIONS:\n"
                    "- Extract 5 to 10 key South African legal concepts, doctrine names, and statutory provisions into 'keywords' (e.g. 'Constitutional Law', 'Section 27 Rights', 'Abuse of Dominance', 'Margin Squeeze', 'Administrative Action', 'Interdict'). Do NOT leave 'keywords' empty."
                )
                body_prompt = build_system_prompt(SafliiBodyData, body_instructions)
                body_res = call_ollama(client, ai_model, body_prompt, body_context, SafliiBodyData) or {}

                raw_result = {**header_res, **precedents_res, **body_res}
                if not raw_result.get("applicant_plaintiff"):
                    raw_result["applicant_plaintiff"] = str(record_data.get("title") or "State")
                if not raw_result.get("respondent_defendant"):
                    raw_result["respondent_defendant"] = []
                if not raw_result.get("hearing_date"):
                    raw_result["hearing_date"] = raw_result.get("judgment_date") or date.today().isoformat()
                if not raw_result.get("judgment_date"):
                    raw_result["judgment_date"] = raw_result.get("hearing_date") or date.today().isoformat()
                if raw_result.get("reportable") is None:
                    raw_result["reportable"] = False
                if not raw_result.get("court"):
                    raw_result["court"] = "High Court"
                
                # Sanitize and clean extracted judges list
                raw_judges = raw_result.get("judges") or []
                if not isinstance(raw_judges, list):
                    raw_judges = [str(raw_judges)]
                cleaned_judges = []
                invalid_judge_indicators = [
                    "[not", "not explicitly", "not stated", "not specified", "unspecified",
                    "unknown", "n/a", "none", "cct", "case no", "applicant", "respondent"
                ]
                for j in raw_judges:
                    if not j or not isinstance(j, str):
                        continue
                    j_str = j.strip().strip("\"'").strip()
                    j_lower = j_str.lower()
                    if any(ind in j_lower for ind in invalid_judge_indicators):
                        continue
                    # Strip leading professional/honorific prefixes
                    j_str = re.sub(r'^(?:Advocate|Adv\.|Mr|Ms|Mrs|Dr|Justice)\s+', '', j_str, flags=re.IGNORECASE).strip()
                    if len(j_str) >= 2 and j_str not in cleaned_judges:
                        cleaned_judges.append(j_str)
                raw_result["judges"] = cleaned_judges

                if not raw_result.get("court_location"):
                    raw_result["court_location"] = "South Africa"
                if raw_result.get("precedents_cited") is None:
                    raw_result["precedents_cited"] = []
                if raw_result.get("keywords") is None:
                    raw_result["keywords"] = []
                if not raw_result.get("obiter_dicta"):
                    raw_result["obiter_dicta"] = "No notable obiter dicta identified in this judgment."
                if not raw_result.get("ratio_decidendi"):
                    raw_result["ratio_decidendi"] = "No explicit ratio decidendi identified."
                if not raw_result.get("order"):
                    raw_result["order"] = "Order not explicitly specified."
                if not raw_result.get("summary"):
                    raw_result["summary"] = "Summary not generated."
            else:
                # 4. Standard single-pass extraction
                current_system_prompt = build_system_prompt(current_schema_cls, extraction_instructions)
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
                    doc_date = schema_instance.hearing_date or db_doc_date
                    case_num = case_number or (schema_instance.dict().get("case_number") if hasattr(schema_instance, "dict") else None)
                    metadata = BaseExtractedRecord(
                        entity_name=str(db_entity_name),
                        target_name=str(db_target_name),
                        document_date=doc_date if isinstance(doc_date, date) else date.today(),
                        record_type=str(db_record_type),
                        case_number=str(case_num) if case_num else None,
                    )
                    
                    extracted_data_obj = (
                        schema_instance
                        if isinstance(schema_instance, SafliiExtractedData)
                        else SafliiExtractedData.model_validate(schema_instance.model_dump())
                    )
                    
                    outer_instance = SafliiCaseExtraction(
                        metadata=metadata,
                        title=str(record_data.get("title")),
                        extracted_data=extracted_data_obj,
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

        if hasattr(schema_instance, "metadata") and getattr(schema_instance, "metadata") is None:
            try:
                schema_instance.metadata = BaseExtractedRecord(
                    entity_name=str(db_entity_name),
                    target_name=str(db_target_name),
                    document_date=db_doc_date if isinstance(db_doc_date, date) else date.today(),
                    record_type=str(db_record_type),
                    case_number=str(case_number) if case_number else None,
                )
            except Exception:
                pass

        # 5. Apply regex post-processing PII scrubbing on the output dictionary
        validated_dict = schema_instance.model_dump(mode="json")
        scrubbed_dict = scrub_pii_data(validated_dict)

        # 6. Populate scrubbed_records table and mark extracted_record as scrubbed
        try:
            # Insert scrubbed fields directly into scrubbed_records.data
            await conn.execute(
                """
                INSERT INTO scrubbed_records (id, extracted_record_id, data, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
                ON CONFLICT (extracted_record_id)
                DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                """,
                uuid.uuid4(),
                record_id,
                json.dumps(scrubbed_dict, ensure_ascii=False),
            )

            # Mark extracted_record as scrubbed
            await conn.execute(
                "UPDATE extracted_records SET scrubbed_at = NOW(), updated_at = NOW() WHERE id = $1",
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
    extraction_params = config.get("extraction_params") or {}
    content_field: str = extraction_params.get("content_field", "")
    parser_name: str = extraction_params.get("parser", "")
    parser_func = resolve_parser(parser_name) if parser_name else None

    if not parser_func:
        logger.error(
            "❌ [ERROR] A valid document section parser is required in extraction_params for pipeline '%s' (e.g. 'parser': 'saflii_document_parser'). Aborting extraction.",
            pipeline_name,
        )
        sys.exit(1)

    logger.info("==================================================")
    logger.info("🚀 COEUS LLM EXTRACTOR INITIALIZED (PIPELINE: %s)", pipeline_name)
    logger.info("   Schema       : %s", schema_name or "GenericDocumentExtraction")
    logger.info("   AI model     : %s", ai_model)
    logger.info("   Parser       : %s", parser_name)
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
        # Execute the document section parsing phase first
        await run_parser_phase(conn, pipeline_name, parser_func, record_id_filter)

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
                SELECT e.id, e.data, e.source_url, e.requires_human_review, p.data AS parsed_data,
                       t.target_name AS db_target_name, ent.name AS db_entity_name
                FROM extracted_records e
                LEFT JOIN targets t ON e.target_id = t.id
                LEFT JOIN entities ent ON t.entity_id = ent.id
                LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
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
                        SELECT e.id, e.data, e.source_url, e.requires_human_review, p.data AS parsed_data,
                               t.target_name AS db_target_name, ent.name AS db_entity_name
                        FROM extracted_records e
                        LEFT JOIN targets t ON e.target_id = t.id
                        LEFT JOIN entities ent ON t.entity_id = ent.id
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
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
                        SELECT e.id, e.data, e.source_url, e.requires_human_review, p.data AS parsed_data,
                               t.target_name AS db_target_name, ent.name AS db_entity_name
                        FROM extracted_records e
                        LEFT JOIN targets t ON e.target_id = t.id
                        LEFT JOIN entities ent ON t.entity_id = ent.id
                        LEFT JOIN parsed_records p ON e.id = p.extracted_record_id
                        LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
                        WHERE e.record_type = $1
                          AND e.status = 'detailed'
                          AND (e.requires_human_review IS NOT TRUE)
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
            "The Pydantic schema class name from schemas package to validate against. "
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
