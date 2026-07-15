import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project root is in path so package-level relative imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.sabinet_scraper import run_extraction


@pytest.mark.asyncio
async def test_run_extraction_forward_setup(monkeypatch, mocker):
    """Verify that run_extraction retrieves correct config and queries the db with the shared record type in forward mode."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "shared_record_type": "sabinet_ccma_shared",
            "reverse_direction": False
        }
    }

    # Mock utilities
    mock_fetch_config = AsyncMock(return_value=mock_config)
    mocker.patch("extraction_workers.sabinet_scraper.fetch_pipeline_config", mock_fetch_config)

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_get_db = AsyncMock(return_value=mock_conn)
    mocker.patch("extraction_workers.sabinet_scraper.get_db_connection", mock_get_db)

    # Mock db_storage methods
    mock_resolve_target = AsyncMock(return_value="target-123")
    mocker.patch("extraction_workers.db_storage.resolve_target_id", mock_resolve_target)

    mock_get_urls = AsyncMock(return_value={"url1"})
    mocker.patch("extraction_workers.db_storage.get_existing_urls", mock_get_urls)

    mock_get_cases = AsyncMock(return_value={"case1"})
    mocker.patch("extraction_workers.db_storage.get_existing_case_numbers", mock_get_cases)

    # Mock progress state: start at year 2021, month 5, last window completed cleanly
    mock_progress = {
        "last_year": 2021,
        "last_month": 5,
        "last_completed": True
    }
    mock_load_state = AsyncMock(return_value=mock_progress)
    mocker.patch("extraction_workers.db_storage.load_pipeline_state", mock_load_state)

    saved_states = []
    async def capture_save(conn, pipeline_name, state):
        saved_states.append(dict(state))
    mock_save_state = AsyncMock(side_effect=capture_save)
    mocker.patch("extraction_workers.db_storage.save_pipeline_state", mock_save_state)

    # Mock Playwright browser interactions
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value=[[2020, 100], [2021, 50], [2022, 10]])
    mock_page.locator = MagicMock()
    mock_page.locator.count = AsyncMock(return_value=0)

    mock_manager = MagicMock()
    mock_manager.start = AsyncMock(return_value=mock_page)
    mock_manager.page = mock_page
    mock_manager.close = AsyncMock()
    mock_manager.recycle = AsyncMock()

    mocker.patch("extraction_workers.sabinet_scraper.BrowserManager", return_value=mock_manager)

    mock_pw = MagicMock()
    mock_pw.stop = AsyncMock()
    mock_pw_start = AsyncMock(return_value=mock_pw)
    mocker.patch("extraction_workers.sabinet_scraper.async_playwright", return_value=MagicMock(start=mock_pw_start))

    # Mock sys.exit to prevent test runner from exiting if error happens
    mock_exit = mocker.patch("sys.exit")

    # Run the function
    # Let's mock loop internals to stop quickly (e.g. make loop raise a mock error when it hits first month)
    mock_page.locator("input[placeholder=\"Date From\"]").first.is_visible = AsyncMock(side_effect=Exception("StopLoop"))

    await run_extraction("sabinet_ccma_test")

    # 1. Verify shared record type is passed to db_storage queries
    mock_get_urls.assert_called_once_with(mock_conn, "sabinet_ccma_shared")
    mock_get_cases.assert_called_once_with(mock_conn, "sabinet_ccma_shared")

    # 2. Verify resume calculations:
    # Forward direction: year 2021 month 5 completed -> next month is 2021 month 6
    assert any(
        s.get("last_year") == 2021 and s.get("last_month") == 6 and s.get("last_completed") is False
        for s in saved_states
    )


@pytest.mark.asyncio
async def test_run_extraction_reverse_setup(monkeypatch, mocker):
    """Verify that run_extraction handles reverse direction (newest-to-oldest) year/month sorting and skipping."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "shared_record_type": "sabinet_ccma_shared",
            "reverse_direction": True
        }
    }

    mock_fetch_config = AsyncMock(return_value=mock_config)
    mocker.patch("extraction_workers.sabinet_scraper.fetch_pipeline_config", mock_fetch_config)

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=0)
    mock_get_db = AsyncMock(return_value=mock_conn)
    mocker.patch("extraction_workers.sabinet_scraper.get_db_connection", mock_get_db)

    mocker.patch("extraction_workers.db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    mocker.patch("extraction_workers.db_storage.get_existing_urls", AsyncMock(return_value=set()))
    mocker.patch("extraction_workers.db_storage.get_existing_case_numbers", AsyncMock(return_value=set()))

    # Mock progress state: start at year 2021, month 5, last window completed cleanly
    mock_progress = {
        "last_year": 2021,
        "last_month": 5,
        "last_completed": True
    }
    mock_load_state = AsyncMock(return_value=mock_progress)
    mocker.patch("extraction_workers.db_storage.load_pipeline_state", mock_load_state)

    saved_states = []
    async def capture_save(conn, pipeline_name, state):
        saved_states.append(dict(state))
    mock_save_state = AsyncMock(side_effect=capture_save)
    mocker.patch("extraction_workers.db_storage.save_pipeline_state", mock_save_state)

    # Mock Playwright browser interactions
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value=[[2020, 100], [2021, 50], [2022, 10]])
    mock_page.locator = MagicMock()
    mock_page.locator.count = AsyncMock(return_value=0)

    mock_manager = MagicMock()
    mock_manager.start = AsyncMock(return_value=mock_page)
    mock_manager.page = mock_page
    mock_manager.close = AsyncMock()
    mock_manager.recycle = AsyncMock()

    mocker.patch("extraction_workers.sabinet_scraper.BrowserManager", return_value=mock_manager)

    mock_pw = MagicMock()
    mock_pw.stop = AsyncMock()
    mock_pw_start = AsyncMock(return_value=mock_pw)
    mocker.patch("extraction_workers.sabinet_scraper.async_playwright", return_value=MagicMock(start=mock_pw_start))

    mocker.patch("sys.exit")

    # Stop the loop at first iteration
    mock_page.locator("input[placeholder=\"Date From\"]").first.is_visible = AsyncMock(side_effect=Exception("StopLoop"))

    await run_extraction("sabinet_ccma_test")

    # Verify resume calculations for reverse direction:
    # 2021 month 5 completed -> next month is 2021 month 4 (decrement instead of increment)
    assert any(
        s.get("last_year") == 2021 and s.get("last_month") == 4 and s.get("last_completed") is False
        for s in saved_states
    )
