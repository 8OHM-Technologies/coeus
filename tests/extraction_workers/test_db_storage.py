import sys
import os
import pytest
from unittest.mock import AsyncMock

# Ensure project root is in path so relative imports within extraction_workers package work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.db_storage import get_existing_urls, get_existing_case_numbers


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
