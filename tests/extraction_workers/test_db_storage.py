import sys
import os
import pytest
from unittest.mock import AsyncMock

# Ensure project root is in path so relative imports within extraction_workers package work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.db_storage import (
    get_existing_urls,
    get_existing_dataset_numbers,
    is_record_complete,
    upsert_scraped_record,
    upsert_scraped_records_batch,
    load_records_needing_detail,
    update_record_data,
)


@pytest.mark.asyncio
async def test_get_existing_urls():
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"source_url": "https://example.com/doc1"},
        {"source_url": "https://example.com/doc2"},
        {"source_url": None},
    ]

    urls = await get_existing_urls(mock_conn, "test_pipeline")

    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "SELECT source_url FROM extracted_records" in query
    assert "record_type = $1" in query
    assert urls == {"https://example.com/doc1", "https://example.com/doc2"}


@pytest.mark.asyncio
async def test_get_existing_dataset_numbers():
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"dataset_number": "JR123/2022"},
        {"dataset_number": "JS456/2023"},
        {"dataset_number": None},
    ]

    dataset_numbers = await get_existing_dataset_numbers(mock_conn, "test_pipeline")

    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "SELECT data->>'dataset_number' AS dataset_number FROM extracted_records" in query
    assert "record_type = $1" in query
    assert dataset_numbers == {"JR123/2022", "JS456/2023"}


@pytest.mark.asyncio
async def test_is_record_complete():
    mock_conn = AsyncMock()
    
    # 1. No inputs -> return False
    res = await is_record_complete(mock_conn, "test_pipeline", None, None)
    assert res is False

    # 2. Both inputs present
    mock_conn.fetchval.return_value = True
    res = await is_record_complete(mock_conn, "test_pipeline", "https://example.com/doc1", "JR123")
    assert res is True
    mock_conn.fetchval.assert_called_once()
    query = mock_conn.fetchval.call_args[0][0]
    assert "source_url = $2" in query
    assert "data->>'dataset_number' = $3" in query
    assert "details_scraped_at" in query

    # 3. Only url present
    mock_conn.fetchval.reset_mock()
    mock_conn.fetchval.return_value = False
    res = await is_record_complete(mock_conn, "test_pipeline", "https://example.com/doc1", None)
    assert res is False
    mock_conn.fetchval.assert_called_once()
    query = mock_conn.fetchval.call_args[0][0]
    assert "source_url = $2" in query

    # 4. Only dataset_number present
    mock_conn.fetchval.reset_mock()
    mock_conn.fetchval.return_value = True
    res = await is_record_complete(mock_conn, "test_pipeline", None, "JR123")
    assert res is True
    mock_conn.fetchval.assert_called_once()
    query = mock_conn.fetchval.call_args[0][0]
    assert "data->>'dataset_number' = $3" in query


@pytest.mark.asyncio
async def test_upsert_scraped_record(mocker):
    mock_conn = AsyncMock()
    import uuid
    from datetime import date
    target_uuid = uuid.uuid4()
    
    # Test upsert with default status
    await upsert_scraped_record(
        mock_conn, target_uuid, "test_pipeline", "https://example.com/doc", {"title": "Doc"}, date(2026, 1, 1)
    )
    
    mock_conn.execute.assert_called_once()
    query = mock_conn.execute.call_args[0][0]
    params = mock_conn.execute.call_args[0][1:]
    assert "INSERT INTO extracted_records" in query
    assert "status" in query
    # Check that status 'indexed' is passed
    assert params[-1] == "indexed"


@pytest.mark.asyncio
async def test_upsert_scraped_records_batch():
    mock_conn = AsyncMock()
    import uuid
    target_uuid = uuid.uuid4()
    
    records = [
        {"detail_url": "https://example.com/1", "title": "1"},
        {"detail_url": "https://example.com/2", "title": "2"},
    ]
    
    count = await upsert_scraped_records_batch(
        mock_conn, target_uuid, "test_pipeline", records, url_key="detail_url", status="indexed"
    )
    
    assert count == 2
    mock_conn.executemany.assert_called_once()
    query = mock_conn.executemany.call_args[0][0]
    batch_args = mock_conn.executemany.call_args[0][1]
    assert "INSERT INTO extracted_records" in query
    assert len(batch_args) == 2
    assert batch_args[0][-1] == "indexed"


@pytest.mark.asyncio
async def test_load_records_needing_detail():
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"id": "uuid-1", "source_url": "https://example.com/1", "data": '{"title": "1"}'},
    ]
    
    res = await load_records_needing_detail(mock_conn, "test_pipeline")
    
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "status = 'indexed'" in query
    assert "status IS NULL" in query
    assert len(res) == 1
    assert res[0]["id"] == "uuid-1"


@pytest.mark.asyncio
async def test_update_record_data():
    mock_conn = AsyncMock()
    import uuid
    record_uuid = uuid.uuid4()
    
    # Test update with status provided
    await update_record_data(mock_conn, record_uuid, {"title": "updated"}, status="detailed")
    mock_conn.execute.assert_called_once()
    query = mock_conn.execute.call_args[0][0]
    assert "SET data = $1, status = $2" in query
    
    # Test update without status
    mock_conn.execute.reset_mock()
    await update_record_data(mock_conn, record_uuid, {"title": "updated"})
    mock_conn.execute.assert_called_once()
    query = mock_conn.execute.call_args[0][0]
    assert "SET data = $1" in query
    assert "status" not in query


@pytest.mark.asyncio
async def test_compute_dynamic_pipeline_state():
    from extraction_workers.db_storage import compute_dynamic_pipeline_state

    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"rec_year": 2024, "rec_status": "detailed", "count": 10},
        {"rec_year": 2024, "rec_status": "indexed", "count": 5},
        {"rec_year": 2025, "rec_status": "indexed", "count": 3},
    ]

    state = await compute_dynamic_pipeline_state(mock_conn, "test_pipeline")

    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "FROM extracted_records" in query
    assert "WHERE record_type = $1" in query

    assert state["record_type"] == "test_pipeline"
    assert state["total_records"] == 18
    assert state["total_detailed"] == 10
    assert state["total_indexed"] == 8
    assert state["records_needing_detail"] == 8
    assert state["yearly_stats"]["2024"] == {"total": 15, "indexed": 5, "detailed": 10, "needing_detail": 5}
    assert state["yearly_stats"]["2025"] == {"total": 3, "indexed": 3, "detailed": 0, "needing_detail": 3}
    assert state["incomplete_years"] == [2024, 2025]
    assert state["completed_years"] == []
    assert state["fully_complete"] is False


@pytest.mark.asyncio
async def test_sync_dynamic_pipeline_state():
    from extraction_workers.db_storage import sync_dynamic_pipeline_state

    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = '{"last_year": 2024}'
    mock_conn.fetch.return_value = [
        {"rec_year": 2024, "rec_status": "detailed", "count": 5},
    ]

    state = await sync_dynamic_pipeline_state(
        mock_conn, "test_pipeline", "test_record_type", extra_state={"last_month": 12}
    )

    mock_conn.execute.assert_called_once()
    assert state["total_records"] == 5
    assert state["records_needing_detail"] == 0
    assert state["completed_years"] == [2024]
    assert state["fully_complete"] is True
    assert state["last_year"] == 2024
    assert state["last_month"] == 12



