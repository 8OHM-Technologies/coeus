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


from extraction_workers.db_storage import load_records_needing_detail
from extraction_workers.sabinet_scraper import run_detail_extraction

@pytest.mark.asyncio
async def test_load_records_needing_detail_sorting():
    """Verify that load_records_needing_detail passes the correct ordering command in SQL."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    # 1. Forward mode (ASC)
    await load_records_needing_detail(mock_conn, "test_pipeline", sort_desc=False)
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "ORDER BY extracted_at ASC" in query

    # 2. Reverse mode (DESC)
    mock_conn.fetch.reset_mock()
    await load_records_needing_detail(mock_conn, "test_pipeline", sort_desc=True)
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "ORDER BY extracted_at DESC" in query


@pytest.mark.asyncio
async def test_run_detail_extraction_reverse_and_skip(mocker):
    """Verify that run_detail_extraction sorts descending in reverse mode and skips already detailed cases."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "index_pipeline_name": "sabinet_ccma_shared",
            "reverse_direction": True
        }
    }

    mock_fetch_config = AsyncMock(return_value=mock_config)
    mocker.patch("extraction_workers.sabinet_scraper.fetch_pipeline_config", mock_fetch_config)

    mock_conn = AsyncMock()
    
    # conn.fetchval is called for total_count, completed_count, and then status checks in the loop
    # We return total_count=2, completed_count=0, then status="detailed" (skip), then status="indexed" (process)
    mock_conn.fetchval.side_effect = [2, 0, "detailed", "indexed"]
    
    mock_get_db = AsyncMock(return_value=mock_conn)
    mocker.patch("extraction_workers.sabinet_scraper.get_db_connection", mock_get_db)

    # Mock cases to load: Case 1 (already detailed) and Case 2 (indexed)
    mock_cases = [
        {"id": "uuid-1", "source_url": "https://example.com/case1", "data": {}},
        {"id": "uuid-2", "source_url": "https://example.com/case2", "data": {}}
    ]
    mock_load_cases = AsyncMock(return_value=mock_cases)
    mocker.patch("extraction_workers.db_storage.load_records_needing_detail", mock_load_cases)
    mocker.patch("extraction_workers.db_storage.load_pipeline_state", AsyncMock(return_value={}))
    mocker.patch("extraction_workers.db_storage.update_record_data", AsyncMock())

    # Mock Playwright page and browser
    mock_page = AsyncMock()
    # Mock evaluate to return details paywall content info (auth ok, content loaded, empty metadata)
    mock_page.evaluate = AsyncMock(return_value={"content_loaded": True, "auth_ok": True, "metadata": {"test": "val"}})
    
    mock_manager = MagicMock()
    mock_manager.start = AsyncMock(return_value=mock_page)
    mock_manager.page = mock_page
    mock_manager.close = AsyncMock()
    
    mocker.patch("extraction_workers.sabinet_scraper.BrowserManager", return_value=mock_manager)

    mock_pw = MagicMock()
    mock_pw.stop = AsyncMock()
    mock_pw_start = AsyncMock(return_value=mock_pw)
    mocker.patch("extraction_workers.sabinet_scraper.async_playwright", return_value=MagicMock(start=mock_pw_start))

    mocker.patch("sys.exit")

    await run_detail_extraction("sabinet_ccma_test_details")

    # 1. Verify load_records_needing_detail was called with sort_desc=True
    mock_load_cases.assert_called_once_with(mock_conn, "sabinet_ccma_shared", sort_desc=True)

    # 2. Verify page.goto was called ONLY for case2, not case1 (since case1 status was "detailed")
    # case1 should be skipped, so page.goto was never called with case1 url
    # page.goto should be called once with case2 url
    assert mock_page.goto.call_count == 1
    mock_page.goto.assert_called_once_with("https://example.com/case2", wait_until="domcontentloaded", timeout=45000)


@pytest.mark.asyncio
async def test_run_extraction_reverse_fresh_run(monkeypatch, mocker):
    """Verify that a fresh run in reverse direction (empty progress state) processes years without skipping."""
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

    # Mock progress state: empty for a fresh run
    mock_load_state = AsyncMock(return_value={})
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

    # Stop the loop at first iteration of month search
    mock_page.locator("input[placeholder=\"Date From\"]").first.is_visible = AsyncMock(side_effect=Exception("StopLoop"))

    await run_extraction("sabinet_ccma_test")

    # The first processed year should be 2022 (newest) and first month 12
    # Verify save_progress was called to save the first state
    assert len(saved_states) > 0
    assert saved_states[0].get("last_year") == 2022
    assert saved_states[0].get("last_month") == 12
    assert saved_states[0].get("last_completed") is False


@pytest.mark.asyncio
async def test_run_detail_extraction_shared_record_type(mocker):
    """Verify that run_detail_extraction uses shared_record_type when loading and counting records needing detail."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "shared_record_type": "sabinet_ccma_shared",
            "reverse_direction": False
        }
    }

    mock_fetch_config = AsyncMock(return_value=mock_config)
    mocker.patch("extraction_workers.sabinet_scraper.fetch_pipeline_config", mock_fetch_config)

    mock_conn = AsyncMock()
    mock_conn.fetchval.side_effect = [2, 0, "indexed", "indexed"]
    
    mock_get_db = AsyncMock(return_value=mock_conn)
    mocker.patch("extraction_workers.sabinet_scraper.get_db_connection", mock_get_db)

    mock_cases = [
        {"id": "uuid-1", "source_url": "https://example.com/case1", "data": {}},
        {"id": "uuid-2", "source_url": "https://example.com/case2", "data": {}}
    ]
    mock_load_cases = AsyncMock(return_value=mock_cases)
    mocker.patch("extraction_workers.db_storage.load_records_needing_detail", mock_load_cases)
    mocker.patch("extraction_workers.db_storage.load_pipeline_state", AsyncMock(return_value={}))
    mocker.patch("extraction_workers.db_storage.update_record_data", AsyncMock())

    # Mock Playwright page and browser
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value={"content_loaded": True, "auth_ok": True, "metadata": {"test": "val"}})
    
    mock_manager = MagicMock()
    mock_manager.start = AsyncMock(return_value=mock_page)
    mock_manager.page = mock_page
    mock_manager.close = AsyncMock()
    
    mocker.patch("extraction_workers.sabinet_scraper.BrowserManager", return_value=mock_manager)

    mock_pw = MagicMock()
    mock_pw.stop = AsyncMock()
    mock_pw_start = AsyncMock(return_value=mock_pw)
    mocker.patch("extraction_workers.sabinet_scraper.async_playwright", return_value=MagicMock(start=mock_pw_start))

    mocker.patch("sys.exit")

    await run_detail_extraction("sabinet_ccma___oldest_first")

    # Verify load_records_needing_detail was called with the shared_record_type
    mock_load_cases.assert_called_once_with(mock_conn, "sabinet_ccma_shared", sort_desc=False)


