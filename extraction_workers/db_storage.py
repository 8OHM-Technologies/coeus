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
    return {row["source_url"] for row in rows if row["source_url"]}


async def get_existing_urls_by_status(
    conn: asyncpg.Connection,
    record_type: str,
    status: str = "detailed",
) -> set[str]:
    """Return ``source_url`` values for *record_type* filtered by *status*."""
    rows = await conn.fetch(
        """
        SELECT source_url FROM extracted_records
        WHERE record_type = $1 AND status = $2 AND source_url IS NOT NULL
        """,
        record_type,
        status,
    )
    return {row["source_url"] for row in rows if row["source_url"]}

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
    return {row["case_number"] for row in rows if row["case_number"]}


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
            data, source_url, status, scraped_at
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
                data, source_url, status, scraped_at
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
    limit: int | None = None,
    exclude_ids: list[uuid.UUID] | None = None,
    include_data: bool = True,
) -> list[dict[str, Any]]:
    """Return records that have a ``source_url`` but haven't been detail-scraped yet.

    A record is considered "not detail-scraped" if its ``status`` is 'indexed'
    or if it is a legacy record where ``status`` is NULL and it doesn't have ``details_scraped_at``.
    """
    order = "DESC" if sort_desc else "ASC"
    select_clause = "id, source_url, data" if include_data else "id, source_url"
    
    query_parts = [
        f"""
        SELECT {select_clause}
        FROM extracted_records
        WHERE record_type = $1
          AND source_url IS NOT NULL
          AND (status = 'indexed' OR (status IS NULL AND (data->>'details_scraped_at') IS NULL))
        """
    ]
    params = [record_type]

    if exclude_ids:
        query_parts.append(f"  AND NOT (id = ANY(${len(params) + 1}::uuid[]))")
        params.append(exclude_ids)

    query_parts.append(f"ORDER BY scraped_at {order}")

    if limit is not None:
        query_parts.append(f"LIMIT {limit}")

    query = "\n".join(query_parts)
    rows = await conn.fetch(query, *params)
    
    results = []
    for row in rows:
        data = None
        if include_data:
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


async def compute_dynamic_pipeline_state(
    conn: asyncpg.Connection,
    record_type: str,
) -> dict[str, Any]:
    """Dynamically query ``extracted_records`` for *record_type* to evaluate pipeline progress.

    Returns a dict containing:
      - ``total_records``: total count of records stored for this record_type
      - ``total_indexed``: count of records pending detailing
      - ``total_detailed``: count of records fully detailed
      - ``records_needing_detail``: count of records still needing detailing
      - ``yearly_stats``: per-year breakdown dict {year_str: {total, indexed, detailed, needing_detail}}
      - ``completed_years``: list of years where all harvested records are detailed
      - ``incomplete_years``: list of years with pending detailing items
      - ``fully_complete``: bool indicating whether all harvested records are detailed (and total > 0)
    """
    rows = await conn.fetch(
        """
        SELECT 
            COALESCE(
                EXTRACT(YEAR FROM document_date)::int,
                CASE WHEN (data->>'year') ~ '^[0-9]+$' THEN (data->>'year')::int ELSE NULL END
            ) AS rec_year,
            CASE 
                WHEN status = 'detailed' OR (data->>'details_scraped_at') IS NOT NULL THEN 'detailed'
                ELSE 'indexed'
            END AS rec_status,
            COUNT(*) AS count
        FROM extracted_records
        WHERE record_type = $1
        GROUP BY rec_year, rec_status
        """,
        record_type,
    )

    yearly_stats: dict[int, dict[str, int]] = {}
    total_records = 0
    total_indexed = 0
    total_detailed = 0

    for row in rows:
        ryear = row["rec_year"]
        rstatus = row["rec_status"]
        rcount = row["count"]

        total_records += rcount
        if rstatus == "detailed":
            total_detailed += rcount
        else:
            total_indexed += rcount

        if ryear is not None:
            if ryear not in yearly_stats:
                yearly_stats[ryear] = {"total": 0, "indexed": 0, "detailed": 0, "needing_detail": 0}
            yearly_stats[ryear]["total"] += rcount
            if rstatus == "detailed":
                yearly_stats[ryear]["detailed"] += rcount
            else:
                yearly_stats[ryear]["indexed"] += rcount
                yearly_stats[ryear]["needing_detail"] += rcount

    completed_years = [
        y for y, s in sorted(yearly_stats.items()) if s["total"] > 0 and s["needing_detail"] == 0
    ]
    incomplete_years = [
        y for y, s in sorted(yearly_stats.items()) if s["needing_detail"] > 0
    ]

    records_needing_detail = total_indexed
    fully_complete = bool(total_records > 0 and records_needing_detail == 0)

    yearly_stats_json = {str(k): v for k, v in sorted(yearly_stats.items())}

    return {
        "record_type": record_type,
        "total_records": total_records,
        "total_indexed": total_indexed,
        "total_detailed": total_detailed,
        "records_needing_detail": records_needing_detail,
        "yearly_stats": yearly_stats_json,
        "completed_years": completed_years,
        "incomplete_years": incomplete_years,
        "fully_complete": fully_complete,
    }


async def sync_dynamic_pipeline_state(
    conn: asyncpg.Connection,
    pipeline_name: str,
    record_type: str,
    extra_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute current dynamic state from ``extracted_records`` and persist into ``pipeline_state`` column."""
    existing = await load_pipeline_state(conn, pipeline_name)
    dynamic = await compute_dynamic_pipeline_state(conn, record_type)

    merged = {**existing, **dynamic}
    if extra_state:
        merged.update(extra_state)

    await save_pipeline_state(conn, pipeline_name, merged)
    return merged