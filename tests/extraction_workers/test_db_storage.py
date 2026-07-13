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

