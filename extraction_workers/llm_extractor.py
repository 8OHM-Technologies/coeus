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

from db import get_db_connection
from utils import fetch_pipeline_config

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
        return json.loads(raw)
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
        extracted_payload = getattr(schema_instance, "extracted_data", raw_data)

        entity_name: str = getattr(meta, "entity_name", pipeline_name) if meta else pipeline_name
        target_name: str = getattr(meta, "target_name", pipeline_name) if meta else pipeline_name
        doc_date: date = getattr(meta, "document_date", date.today()) if meta else date.today()
        record_type: str = getattr(meta, "record_type", pipeline_name) if meta else pipeline_name
        requires_review: bool = getattr(quality, "requires_human_review", False) if quality else False
        review_reason: str | None = getattr(quality, "review_reason", None) if quality else None

        # 1. Upsert entity -------------------------------------------------------
        entity_id = await conn.fetchval(
            """
            INSERT INTO entities (id, name)
            VALUES ($1, $2)
            ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            uuid.uuid4(),
            entity_name,
        )

        # 2. Upsert target -------------------------------------------------------
        target_id = await conn.fetchval(
            """
            INSERT INTO targets (id, entity_id, target_name)
            VALUES ($1, $2, $3)
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
                data, requires_human_review, review_reason, source_url
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
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
    scan_dir = Path("/app/data") / pipeline_name / document_type
    if not scan_dir.exists():
        logger.error("Scan directory does not exist: %s", scan_dir)
        sys.exit(1)

    files = sorted(scan_dir.glob(f"*{file_ext}"))
    if not files:
        logger.warning("No %s files found in %s. Nothing to extract.", file_ext, scan_dir)
        sys.exit(0)

    logger.info("Found %d file(s) to process in %s", len(files), scan_dir)

    # -- Resolve Pydantic schema -----------------------------------------------
    schemas_module = importlib.import_module("schemas")
    schema_cls: type[BaseModel] = (
        resolve_schema(schema_name)
        or getattr(schemas_module, "GenericDocumentExtraction")
    )
    logger.info("Using schema: %s", schema_cls.__name__)

    # -- Initialise LLM client --------------------------------------------------
    client = get_llm_client()
    system_prompt = build_system_prompt(schema_cls, extraction_instructions)

    # -- Open a single DB connection for the whole run -------------------------
    try:
        conn = await get_db_connection()
        logger.info("Connected to PostgreSQL.")
    except Exception as exc:
        logger.error("Failed to connect to database: %s", exc)
        sys.exit(1)

    success_count = 0
    failure_count = 0

    try:
        for file_path in files:
            logger.info("Processing: %s", file_path.name)

            # 1. Read document text
            doc_text = read_document(file_path, document_type)
            if not doc_text.strip():
                logger.warning("  [!] Empty document, skipping: %s", file_path.name)
                failure_count += 1
                continue

            # 2. Call LLM
            raw_result = call_ollama(client, ai_model, system_prompt, doc_text)
            if raw_result is None:
                logger.error("  [!] LLM returned no result for: %s", file_path.name)
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
