"""
Coeus DB Storage Helpers
========================
Shared async helpers for persisting scraped records directly to the
``extracted_records`` Postgres table via ``asyncpg``.

Used by ``sabinet_scraper.py`` and ``new_saflii_scraper.py`` to replace the
old JSON-file + Google-Drive backup workflow.
"""

import json
import logging
import uuid
from datetime import date
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

def json_dumps(data: Any) -> str:
    """Serialize object to JSON string with default=str serialization."""
    return json.dumps(data, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Internal Utilities
# ---------------------------------------------------------------------------

def _extract_document_date(data_dict: dict[str, Any]) -> date:
    """Fallback extraction chain to resolve a baseline record date from raw data."""
    # 1. Attempt standard target keys
    for key in ("document_date", "date", "publication_date", "award_date"):
        val = data_dict.get(key)
        if not val:
            continue
        if isinstance(val, date):
            return val
        if isinstance(val, str):
            try:
                return date.fromisoformat(val[:10])
            except ValueError:
                pass

    # 2. Try target year fallback
    year_val = data_dict.get("year")
    if year_val:
        try:
            return date(int(year_val), 1, 1)
        except (ValueError, TypeError):
            pass

    # 3. Default to current execution day
    return date.today()


# ---------------------------------------------------------------------------
# Entity / Target resolution
# ---------------------------------------------------------------------------

async def resolve_target_id(
    conn: asyncpg.Connection,
    entity_name: str,
    target_name: str,
    location_url: str | None = None,
) -> uuid.UUID:
    """Upsert an Entity + Target pair and return the ``target_id``.

    * **entity_name** – derived from ``PipelineConfiguration.name``
    * **target_name** – derived from ``PipelineConfiguration.subset``
    * **location_url** – optional, stored on the Target row
    """
    # 1. Upsert entity
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

    # 2. Upsert target
    target_id = await conn.fetchval(
        """
        INSERT INTO targets (id, entity_id, target_name, location, created_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (entity_id, target_name)
        DO UPDATE SET location = COALESCE(EXCLUDED.location, targets.location)
        RETURNING id
        """,
        uuid.uuid4(),
        entity_id,
        target_name,
        location_url,
    )

    return target_id


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

async def get_existing_urls(
    conn: asyncpg.Connection,
    record_type: str,
) -> set[str]:
    """Return the set of ``source_url`` values already stored for *record_type*."""
    rows = await conn.fetch(
        """
        SELECT source_url FROM extracted_records
        WHERE record_type = $1 AND source_url IS NOT NULL
        """,
        record_type,
    )
    return {row["source_url"] for row in rows}

# TODO FIX THIS - USE URLS INSTEAD (NO CASE NUMBERS YET)
async def get_existing_case_numbers(
    conn: asyncpg.Connection,
    record_type: str,
) -> set[str]:
    """Return the set of ``case_number`` values already stored in the ``data`` column for *record_type*."""
    rows = await conn.fetch(
        """
        SELECT data->>'case_number' AS case_number FROM extracted_records
        WHERE record_type = $1 AND (data->>'case_number') IS NOT NULL
        """,
        record_type,
    )
    return {row["case_number"] for row in rows}


# ---------------------------------------------------------------------------
# Upsert scraped records
# ---------------------------------------------------------------------------

async def upsert_scraped_record(
    conn: asyncpg.Connection,
    target_id: uuid.UUID,
    record_type: str,
    source_url: str,
    data_dict: dict[str, Any],
    document_date: date | None = None,
    status: str = "indexed",
) -> None:
    """Upsert a single scraped record into ``extracted_records``.

    Conflict is resolved on ``source_url``; on conflict the ``data`` JSONB
    payload is updated (merged) and ``record_type`` is refreshed.
    """
    resolved_date = document_date or _extract_document_date(data_dict)

    await conn.execute(
        """
        INSERT INTO extracted_records (
            id, target_id, document_date, record_type,
            data, source_url, status, extracted_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        ON CONFLICT (source_url)
        DO UPDATE SET
            data        = EXCLUDED.data,
            record_type = EXCLUDED.record_type,
            status      = EXCLUDED.status
        """,
        uuid.uuid4(),
        target_id,
        resolved_date,
        record_type,
        json.dumps(data_dict, ensure_ascii=False),
        source_url,
        status,
    )


async def upsert_scraped_records_batch(
    conn: asyncpg.Connection,
    target_id: uuid.UUID,
    record_type: str,
    records: list[dict[str, Any]],
    url_key: str = "detail_url",
    status: str = "indexed",
) -> int:
    """Batch-upsert a list of scraped record dicts using an efficient single roundtrip connection.

    Each dict must contain a key identified by *url_key* which is used as the
    ``source_url``. Returns the number of records successfully prepared and upserted.
    """
    if not records:
        return 0

    batch_args = []
    for record in records:
        source_url = record.get(url_key)
        if not source_url:
            continue
        
        resolved_date = _extract_document_date(record)
        batch_args.append((
            uuid.uuid4(),
            target_id,
            resolved_date,
            record_type,
            json.dumps(record, ensure_ascii=False),
            source_url,
            status,
        ))

    if batch_args:
        await conn.executemany(
            """
            INSERT INTO extracted_records (
                id, target_id, document_date, record_type,
                data, source_url, status, extracted_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (source_url)
            DO UPDATE SET
                data        = EXCLUDED.data,
                record_type = EXCLUDED.record_type,
                status      = EXCLUDED.status
            """,
            batch_args,
        )

    return len(batch_args)


# ---------------------------------------------------------------------------
# Detail enrichment helpers
# ---------------------------------------------------------------------------

async def load_records_needing_detail(
    conn: asyncpg.Connection,
    record_type: str,
    sort_desc: bool = False,
) -> list[dict[str, Any]]:
    """Return records that have a ``source_url`` but haven't been detail-scraped yet.

    A record is considered "not detail-scraped" if its ``status`` is 'indexed'
    or if it is a legacy record where ``status`` is NULL and it doesn't have ``details_scraped_at``.
    """
    order = "DESC" if sort_desc else "ASC"
    rows = await conn.fetch(
        f"""
        SELECT id, source_url, data
        FROM extracted_records
        WHERE record_type = $1
          AND source_url IS NOT NULL
          AND (status = 'indexed' OR (status IS NULL AND (data->>'details_scraped_at') IS NULL))
        ORDER BY extracted_at {order}
        """,
        record_type,
    )
    
    results = []
    for row in rows:
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        results.append({
            "id": row["id"],
            "source_url": row["source_url"],
            "data": data,
        })
    return results


async def update_record_data(
    conn: asyncpg.Connection,
    record_id: uuid.UUID,
    merged_data: dict[str, Any],
    status: str | None = None,
) -> None:
    """Replace the ``data`` JSONB column and optionally update the ``status`` for a specific record."""
    if status is not None:
        await conn.execute(
            """
            UPDATE extracted_records
            SET data = $1, status = $2
            WHERE id = $3
            """,
            json.dumps(merged_data, ensure_ascii=False),
            status,
            record_id,
        )
    else:
        await conn.execute(
            """
            UPDATE extracted_records
            SET data = $1
            WHERE id = $2
            """,
            json.dumps(merged_data, ensure_ascii=False),
            record_id,
        )


async def is_record_complete(
    conn: asyncpg.Connection,
    record_type: str,
    source_url: str | None,
    case_number: str | None = None,
) -> bool:
    """Check if a record exists and has details scraped (details_scraped_at is not null)."""
    if not source_url and not case_number:
        return False

    is_complete = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM extracted_records
            WHERE record_type = $1
              AND (
                ($2::text IS NOT NULL AND source_url = $2)
                OR ($3::text IS NOT NULL AND data->>'case_number' = $3)
              )
              AND (data->>'details_scraped_at') IS NOT NULL
        )
        """,
        record_type,
        source_url,
        case_number,
    )
    return bool(is_complete)


# ---------------------------------------------------------------------------
# Pipeline state (progress tracking)
# ---------------------------------------------------------------------------

async def load_pipeline_state(
    conn: asyncpg.Connection,
    pipeline_name: str,
) -> dict[str, Any]:
    """Load the ``pipeline_state`` JSONB from ``pipelines_pipelineconfiguration``.

    Returns an empty dict if the pipeline doesn't exist or the state is null.
    """
    row = await conn.fetchval(
        """
        SELECT pipeline_state
        FROM pipelines_pipelineconfiguration
        WHERE name = $1
           OR LOWER(REPLACE(REPLACE(name, ' ', '_'), '-', '_')) = LOWER(REPLACE(REPLACE($1, ' ', '_'), '-', '_'))
        """,
        pipeline_name,
    )
    if row is None:
        return {}
    if isinstance(row, str):
        return json.loads(row)
    return dict(row)


async def save_pipeline_state(
    conn: asyncpg.Connection,
    pipeline_name: str,
    state: dict[str, Any],
) -> None:
    """Persist scraper progress into the ``pipeline_state`` JSONB column."""
    await conn.execute(
        """
        UPDATE pipelines_pipelineconfiguration
        SET pipeline_state = $1
        WHERE name = $2
           OR LOWER(REPLACE(REPLACE(name, ' ', '_'), '-', '_')) = LOWER(REPLACE(REPLACE($2, ' ', '_'), '-', '_'))
        """,
        json.dumps(state, ensure_ascii=False),
        pipeline_name,
    )