import sys
import os
import pytest
from unittest.mock import AsyncMock

# Ensure project root is in path so relative imports within extraction_workers package work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.db_storage import (
    get_existing_urls,
    get_existing_case_numbers,
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
async def test_get_existing_case_numbers():
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [
        {"case_number": "JR123/2022"},
        {"case_number": "JS456/2023"},
        {"case_number": None},
    ]

    case_numbers = await get_existing_case_numbers(mock_conn, "test_pipeline")

    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "SELECT data->>'case_number' AS case_number FROM extracted_records" in query
    assert "record_type = $1" in query
    assert case_numbers == {"JR123/2022", "JS456/2023"}


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
    assert "source_url = $2 OR data->>'case_number' = $3" in query
    assert "details_scraped_at" in query

    # 3. Only url present
    mock_conn.fetchval.reset_mock()
    mock_conn.fetchval.return_value = False
    res = await is_record_complete(mock_conn, "test_pipeline", "https://example.com/doc1", None)
    assert res is False
    mock_conn.fetchval.assert_called_once()
    query = mock_conn.fetchval.call_args[0][0]
    assert "source_url = $2" in query
    assert "case_number" not in query

    # 4. Only case_number present
    mock_conn.fetchval.reset_mock()
    mock_conn.fetchval.return_value = True
    res = await is_record_complete(mock_conn, "test_pipeline", None, "JR123")
    assert res is True
    mock_conn.fetchval.assert_called_once()
    query = mock_conn.fetchval.call_args[0][0]
    assert "data->>'case_number' = $2" in query
    assert "source_url" not in query


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
    
    # We mock the single upsert function so we can test the batch wrapper
    with pytest.MonkeyPatch.context() as mp:
        mock_upsert = AsyncMock()
        mp.setattr("extraction_workers.db_storage.upsert_scraped_record", mock_upsert)
        
        records = [
            {"detail_url": "https://example.com/1", "title": "1"},
            {"detail_url": "https://example.com/2", "title": "2"},
        ]
        
        await upsert_scraped_records_batch(
            mock_conn, target_uuid, "test_pipeline", records, url_key="detail_url", status="indexed"
        )
        
        assert mock_upsert.call_count == 2
        # Check first call arguments
        call_args = mock_upsert.call_args_list[0][1]
        assert call_args["status"] == "indexed"


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


