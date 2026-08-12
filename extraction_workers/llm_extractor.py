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


def build_system_prompt(
    schema_cls: type[BaseModel],
    extraction_instructions: str,
) -> str:
    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
    field_names = list(schema_cls.model_fields.keys())
    
    base = (
        "You are a precise data extraction assistant. "
        "Extract structured legal information from the document provided.\n\n"
        "STRICT CRITICAL RULE:\n"
        f"Your JSON object MUST contain the following root keys: {', '.join(field_names)}.\n"
        "Do NOT introduce generic section titles like 'Introduction', 'Background', or 'Body' as root keys.\n\n"
        "IMPORTANT:\n"
        "- Respond ONLY with a valid JSON object strictly adhering to the schema keys above.\n"
        "- Do not include markdown code blocks or explanatory text.\n"
        "- If a field value is missing or unknown, set it to null or an empty array.\n\n"
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
                        f"{document_text[:60000]}"
                    ),
                },
            ],
            response_format=response_format,
            temperature=0.2,
            top_p=0.05,
            max_completion_tokens=8192
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
        # -- Fetch records needing extraction ----------------------------------
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
        else:
            # BATCH MODE: select records where record_type matches pipeline_name, status is
            # 'detailed', and there is no entry in the scrubbed_records table yet.
            records = await conn.fetch(
                """
                SELECT e.id, e.data, e.source_url
                FROM extracted_records e
                LEFT JOIN scrubbed_records s ON e.id = s.extracted_record_id
                WHERE e.record_type = $1
                  AND e.status = 'detailed'
                  AND s.extracted_record_id IS NULL
                ORDER BY e.scraped_at ASC
                """,
                pipeline_name,
            )

        if not records:
            logger.info("No detailed records found needing LLM extraction for pipeline '%s'.", pipeline_name)
            return

        logger.info("Found %d record(s) to extract.", len(records))

        success_count = 0
        failure_count = 0

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
                continue

            # 4. Build system prompt dynamically for the selected schema
            current_system_prompt = build_system_prompt(current_schema_cls, extraction_instructions)

            # 5. Call LLM
            raw_result = call_ollama(client, ai_model, current_system_prompt, doc_text, current_schema_cls)
            if raw_result is None:
                logger.error("  [!] LLM returned no result for record: %s", record_id)
                failure_count += 1
                continue

            # 6. Validate against Pydantic schema
            try:
                schema_instance = current_schema_cls.model_validate(raw_result)
            except ValidationError as exc:
                logger.error("  [!] Schema validation failed for record %s:\n%s", record_id, exc)
                failure_count += 1
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

    finally:
        await conn.close()

    logger.info("==================================================")
    logger.info(
        "✅ Extraction complete — %d succeeded, %d failed.",
        success_count,
        failure_count,
    )
    logger.info("==================================================")

    if failure_count > 0 and success_count == 0:
        # All records failed — signal failure
        sys.exit(1)


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
