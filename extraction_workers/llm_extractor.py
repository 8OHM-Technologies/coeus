"""
Coeus LLM Extractor
===================
Reads scraped documents (PDF or HTML/JSON) for a given pipeline, sends each
one to a local Ollama instance via the OpenAI-compatible API, validates the
response against a Pydantic schema, and upserts the structured result into the
PostgreSQL `extracted_records` table.

Expected directory layout (output by the scraper):
    /app/data/{pipeline_name}/pdf/     ← PDF files
    /app/data/{pipeline_name}/html/    ← JSON sidecar files from HTML scrapers

Environment variables (forwarded by the Airflow dynamic factory):
    PIPELINE_NAME           – pipeline identifier
    DOCUMENT_TYPE           – "pdf" or "html"
    AI_MODEL                – Ollama model string, e.g. "ollama/phi4-mini"
    EXTRACTION_INSTRUCTIONS – free-text LLM instructions
    POSTGRES_HOST / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
"""

import argparse
import asyncio
import importlib
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pdfplumber
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from .db import get_db_connection
from .utils.utils import fetch_pipeline_config, resolve_data_dir, save_json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ollama / OpenAI-compatible client
# The Ollama container exposes an OpenAI-compatible REST API on port 11434.
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")


def get_llm_client() -> OpenAI:
    return OpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")


# ---------------------------------------------------------------------------
# Schema resolution
# ---------------------------------------------------------------------------

def resolve_schema(schema_name: str) -> type[BaseModel] | None:
    """
    Dynamically imports ``schemas.py`` (in the same directory) and returns the
    class matching *schema_name*.  Returns ``None`` when not found so the caller
    can fall back to the generic schema.
    """
    if not schema_name:
        return None
    try:
        schemas_module = importlib.import_module(".schemas", package=__package__)
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


# ---------------------------------------------------------------------------
# Document reading
# ---------------------------------------------------------------------------

def read_pdf_text(file_path: Path) -> str:
    """Extract all text from a PDF using pdfplumber."""
    text_parts: list[str] = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
    except Exception as exc:
        logger.error("Failed to read PDF %s: %s", file_path, exc)
    return "\n\n".join(text_parts)


def read_json_text(file_path: Path) -> str:
    """Load a JSON sidecar file and return a pretty-printed string for the LLM."""
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.error("Failed to read JSON %s: %s", file_path, exc)
        return ""


def read_document(file_path: Path, document_type: str) -> str:
    """Dispatch to the correct reader based on document_type."""
    if document_type == "pdf":
        return read_pdf_text(file_path)
    return read_json_text(file_path)


def extract_content_for_llm(record: dict, content_field: str = "") -> str:
    """Extract the content to send to the LLM from a scraped record.

    If *content_field* is specified, use that field directly.  Otherwise try
    common field names produced by the various scrapers.
    Falls back to pretty-printing the entire record.
    """
    if content_field and content_field in record:
        return str(record[content_field])

    for field in ("center_content", "html_content", "content", "text"):
        if field in record:
            return str(record[field])

    # Fallback: dump the entire record as JSON text
    return json.dumps(record, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def build_system_prompt(
    schema_cls: type[BaseModel],
    extraction_instructions: str,
) -> str:
    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
    base = (
        "You are a precise data extraction assistant. "
        "Extract structured information from the document provided by the user.\n\n"
        "IMPORTANT:\n"
        "- Respond ONLY with a valid JSON object that exactly matches the schema below.\n"
        "- Do not include any markdown, explanations, or extra text.\n"
        "- If a required field cannot be determined, use null.\n\n"
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
) -> dict[str, Any] | None:
    """
    Call the Ollama OpenAI-compatible endpoint and return the parsed JSON dict,
    or ``None`` on failure.
    """
    # Strip "ollama/" prefix if present — the raw model name is what Ollama expects
    ollama_model = model.removeprefix("ollama/")
    try:
        response = client.chat.completions.create(
            model=ollama_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Extract structured data from the following document:\n\n"
                        f"{document_text[:12000]}"  # guard against context overrun
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content
        return json.loads(str(raw))
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Database upsert
# ---------------------------------------------------------------------------

async def upsert_record(
    conn,
    pipeline_name: str,
    source_file: str,
    raw_data: dict[str, Any],
    schema_instance: BaseModel,
) -> None:
    """
    Upserts one extraction result into the extracted_records table.

    The schema instance is expected to expose at minimum a ``metadata`` object
    (from BaseExtractedRecord) with entity_name, target_name, document_date,
    record_type and a data_quality_flags object.
    """
    try:
        meta = getattr(schema_instance, "metadata", None)
        quality = getattr(schema_instance, "data_quality_flags", None)

        # For schemas with a nested metadata model (GenericDocumentExtraction)
        # use its fields; otherwise fall back to common field names on the
        # schema itself (e.g. SafliiCaseExtraction) or pipeline defaults.
        entity_name: str = (
            (getattr(meta, "entity_name", None) if meta else None)
            or getattr(schema_instance, "applicant_plaintiff", None)
            or pipeline_name
        )
        target_name: str = (
            (getattr(meta, "target_name", None) if meta else None)
            or getattr(schema_instance, "case_number", None)
            or pipeline_name
        )
        doc_date: date = (
            (getattr(meta, "document_date", None) if meta else None)
            or getattr(schema_instance, "judgment_date", None)
            or date.today()
        )
        record_type: str = (
            (getattr(meta, "record_type", None) if meta else None)
            or pipeline_name
        )
        requires_review: bool = getattr(quality, "requires_human_review", False) if quality else False
        review_reason: str | None = getattr(quality, "review_reason", None) if quality else None

        # For flat schemas (no nested extracted_data), dump the full model
        extracted_payload = (
            getattr(schema_instance, "extracted_data", None)
            or schema_instance.model_dump(mode="json")
        )

        # 1. Upsert entity -------------------------------------------------------
        entity_id = await conn.fetchval(
            """
            INSERT INTO entities (id, name, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            uuid.uuid4(),
            entity_name,
        )

        # 2. Upsert target -------------------------------------------------------
        target_id = await conn.fetchval(
            """
            INSERT INTO targets (id, entity_id, target_name, created_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (entity_id, target_name) DO UPDATE SET target_name = EXCLUDED.target_name
            RETURNING id
            """,
            uuid.uuid4(),
            entity_id,
            target_name,
        )

        # 3. Upsert extracted_record ---------------------------------------------
        await conn.execute(
            """
            INSERT INTO extracted_records (
                id, target_id, document_date, record_type,
                data, requires_human_review, review_reason, source_url, scraped_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
            ON CONFLICT (target_id, document_date, record_type)
            DO UPDATE SET
                data                  = EXCLUDED.data,
                requires_human_review = EXCLUDED.requires_human_review,
                review_reason         = EXCLUDED.review_reason,
                source_url            = EXCLUDED.source_url
            """,
            uuid.uuid4(),
            target_id,
            doc_date,
            record_type,
            json.dumps(
                extracted_payload
                if isinstance(extracted_payload, dict)
                else extracted_payload.model_dump()
            ),
            requires_review,
            review_reason,
            source_file,
        )
        logger.info("  [+] Upserted record: %s / %s (%s)", entity_name, target_name, record_type)

    except Exception as exc:
        logger.error("  [!] DB upsert failed for %s: %s", source_file, exc)
        raise


# ---------------------------------------------------------------------------
# Main extraction flow
# ---------------------------------------------------------------------------

async def run_extraction(pipeline_name: str, schema_name: str) -> None:
    config = await fetch_pipeline_config(pipeline_name)

    document_type: str = config.get("document_type", "pdf").lower()
    extraction_instructions: str = os.getenv("EXTRACTION_INSTRUCTIONS") or config.get(
        "extraction_instructions", ""
    )
    ai_model: str = os.getenv("AI_MODEL") or config.get("engine", "ollama/phi4-mini")

    logger.info("==================================================")
    logger.info("🚀 COEUS LLM EXTRACTOR INITIALIZED (PIPELINE: %s)", pipeline_name)
    logger.info("   Schema       : %s", schema_name or "GenericDocumentExtraction")
    logger.info("   Document type: %s", document_type)
    logger.info("   AI model     : %s", ai_model)
    logger.info("==================================================")

    # -- Locate files ----------------------------------------------------------
    file_ext = ".pdf" if document_type == "pdf" else ".json"
    scan_dir = Path(resolve_data_dir(pipeline_name, document_type))
    if not scan_dir.exists():
        logger.error("Scan directory does not exist: %s", scan_dir)
        sys.exit(1)

    files = sorted(scan_dir.glob(f"*{file_ext}"))
    if not files:
        logger.warning("No %s files found in %s. Nothing to extract.", file_ext, scan_dir)
        sys.exit(0)

    logger.info("Found %d file(s) to process in %s", len(files), scan_dir)

    # -- Resolve Pydantic schema -----------------------------------------------
    schemas_module = importlib.import_module(".schemas", package=__package__)
    schema_cls: type[BaseModel] = (
        resolve_schema(schema_name)
        or getattr(schemas_module, "GenericDocumentExtraction")
    )
    logger.info("Using schema: %s", schema_cls.__name__)

    # -- Initialise LLM client --------------------------------------------------
    client = get_llm_client()
    system_prompt = build_system_prompt(schema_cls, extraction_instructions)

    # -- Detect processing mode ------------------------------------------------
    # Mode A: Individual files (one document per file)
    # Mode B: Single JSON array file (SAFLII/sabinet pattern — one record per
    #         array element, with a content field sent to the LLM).
    content_field: str = (config.get("extraction_params") or {}).get("content_field", "")
    array_mode = False
    records_data: list[dict] = []

    if file_ext == ".json":
        main_json = scan_dir / f"{pipeline_name}.json"
        if main_json.exists():
            try:
                with open(main_json, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    array_mode = True
                    records_data = data
                    logger.info(
                        "Detected JSON array with %d records in %s",
                        len(data),
                        main_json.name,
                    )
            except Exception as exc:
                logger.warning("Could not parse %s as array: %s", main_json, exc)

    # -- Open a single DB connection for the whole run -------------------------
    conn = None
    try:
        conn = await get_db_connection()
        logger.info("Connected to PostgreSQL.")
    except Exception as exc:
        logger.warning("Could not connect to database: %s. Continuing without DB.", exc)

    success_count = 0
    failure_count = 0

    try:
        if array_mode:
            # ---------------------------------------------------------------
            # MODE B: Iterate over records in the JSON array
            # ---------------------------------------------------------------
            output_file = scan_dir / f"{pipeline_name}_extracted.json"
            extracted_results: list[dict] = []
            existing_extractions: dict[str, dict] = {}

            # Load previously extracted records for deduplication
            if output_file.exists():
                try:
                    with open(output_file, "r", encoding="utf-8") as fh:
                        prev = json.load(fh)
                    for item in prev:
                        key = item.get("url") or item.get("case_id") or ""
                        if key:
                            existing_extractions[key] = item
                    extracted_results = prev
                    logger.info(
                        "Loaded %d existing extractions from %s.",
                        len(prev),
                        output_file.name,
                    )
                except Exception:
                    pass

            for idx, record in enumerate(records_data, start=1):
                record_id = (
                    record.get("case_id")
                    or record.get("title")
                    or f"record_{idx}"
                )
                dedup_key = record.get("url") or record.get("case_id") or ""

                if dedup_key and dedup_key in existing_extractions:
                    logger.info(
                        "[%d/%d] Skipping (already extracted): %s",
                        idx,
                        len(records_data),
                        record_id,
                    )
                    continue

                logger.info(
                    "[%d/%d] Extracting: %s", idx, len(records_data), record_id
                )

                # 1. Extract content to send to the LLM
                doc_text = extract_content_for_llm(record, content_field)
                if not doc_text.strip():
                    logger.warning(
                        "  [!] Empty content for record %s, skipping.", record_id
                    )
                    failure_count += 1
                    continue

                # 2. Call LLM
                raw_result = call_ollama(client, ai_model, system_prompt, doc_text)
                if raw_result is None:
                    logger.error(
                        "  [!] LLM returned no result for: %s", record_id
                    )
                    failure_count += 1
                    continue

                # 3. Validate against Pydantic schema
                try:
                    schema_instance = schema_cls.model_validate(raw_result)
                except ValidationError as exc:
                    logger.error(
                        "  [!] Schema validation failed for %s:\n%s",
                        record_id,
                        exc,
                    )
                    failure_count += 1
                    continue

                # 4. Build output record: scraper metadata + LLM extraction
                extracted_record = {
                    "url": record.get("url", ""),
                    "court": record.get("court", ""),
                    "year": record.get("year", ""),
                    "case_id": record.get("case_id", ""),
                    **schema_instance.model_dump(mode="json"),
                    "scraped_at": datetime.now().isoformat(),
                }
                extracted_results.append(extracted_record)
                if dedup_key:
                    existing_extractions[dedup_key] = extracted_record
                success_count += 1
                logger.info("  [+] Extracted: %s", record_id)

                # 5. Upsert to DB (non-fatal if DB is unavailable)
                if conn is not None:
                    try:
                        await upsert_record(
                            conn,
                            pipeline_name,
                            record.get("url", ""),
                            raw_result,
                            schema_instance,
                        )
                    except Exception:
                        pass  # DB failure should not block JSON output

                # Incremental save every 25 records
                if success_count % 25 == 0:
                    save_json(str(output_file), extracted_results)
                    logger.info(
                        "  💾 Incremental save: %d extracted records.",
                        len(extracted_results),
                    )

            # Final save
            save_json(str(output_file), extracted_results)
            logger.info(
                "✅ Saved %d extracted records to %s",
                len(extracted_results),
                output_file,
            )

        else:
            # ---------------------------------------------------------------
            # MODE A: Process individual files (original flow)
            # ---------------------------------------------------------------
            for file_path in files:
                logger.info("Processing: %s", file_path.name)

                # 1. Read document text
                doc_text = read_document(file_path, document_type)
                if not doc_text.strip():
                    logger.warning(
                        "  [!] Empty document, skipping: %s", file_path.name
                    )
                    failure_count += 1
                    continue

                # 2. Call LLM
                raw_result = call_ollama(client, ai_model, system_prompt, doc_text)
                if raw_result is None:
                    logger.error(
                        "  [!] LLM returned no result for: %s", file_path.name
                    )
                    failure_count += 1
                    continue

                # 3. Validate against Pydantic schema
                try:
                    schema_instance = schema_cls.model_validate(raw_result)
                except ValidationError as exc:
                    logger.error(
                        "  [!] Schema validation failed for %s:\n%s",
                        file_path.name,
                        exc,
                    )
                    failure_count += 1
                    continue

                # 4. Upsert to database
                if conn is not None:
                    try:
                        await upsert_record(
                            conn,
                            pipeline_name,
                            str(file_path),
                            raw_result,
                            schema_instance,
                        )
                        success_count += 1
                    except Exception:
                        failure_count += 1
                else:
                    success_count += 1

    finally:
        if conn is not None:
            await conn.close()

    logger.info("==================================================")
    logger.info(
        "✅ Extraction complete — %d succeeded, %d failed.",
        success_count,
        failure_count,
    )
    logger.info("==================================================")

    if failure_count > 0 and success_count == 0:
        # All files failed — signal a hard failure to Airflow
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus LLM Extractor")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The pipeline identifier (used to locate scraped files and config).",
    )
    parser.add_argument(
        "--schema",
        default="",
        help=(
            "The Pydantic schema class name from schemas.py to validate against. "
            "Defaults to GenericDocumentExtraction."
        ),
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name, args.schema))
