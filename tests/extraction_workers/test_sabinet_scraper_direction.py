import sys
import os
import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Ensure project root is in path so package-level relative imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from extraction_workers.db_storage import load_records_needing_detail
from extraction_workers.sabinet_scraper import SabinetScraper


@pytest.fixture(autouse=True)
def mock_resolve_data_dir(mocker):
    mocker.patch("extraction_workers.base_scraper.resolve_data_dir", return_value="/tmp/test_output_dir")


@pytest.mark.asyncio
async def test_load_records_needing_detail_sorting():
    """Verify that load_records_needing_detail passes the correct ordering command in SQL."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    # 1. Forward mode (ASC)
    await load_records_needing_detail(mock_conn, "test_pipeline", sort_desc=False)
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "ORDER BY scraped_at ASC" in query

    # 2. Reverse mode (DESC)
    mock_conn.fetch.reset_mock()
    await load_records_needing_detail(mock_conn, "test_pipeline", sort_desc=True)
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    assert "ORDER BY scraped_at DESC" in query


@pytest.mark.asyncio
async def test_load_records_needing_detail_params():
    """Verify that load_records_needing_detail respects limit, exclude_ids, and include_data."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []
    test_uuid = uuid.uuid4()

    await load_records_needing_detail(
        mock_conn,
        "test_pipeline",
        sort_desc=True,
        limit=10,
        exclude_ids=[test_uuid],
        include_data=False,
    )
    mock_conn.fetch.assert_called_once()
    query = mock_conn.fetch.call_args[0][0]
    params = mock_conn.fetch.call_args[0][1:]

    assert "ORDER BY scraped_at DESC" in query
    assert "LIMIT 10" in query
    assert "AND NOT (id = ANY($2::uuid[]))" in query
    assert "SELECT id, source_url" in query
    # Check that data is not selected before FROM clause
    assert "data" not in query.split("FROM")[0]
    assert params[1] == [test_uuid]


@pytest.mark.asyncio
async def test_run_indexing_forward_setup(mocker):
    """Verify that SabinetScraper.indexing retrieves correct config and sorts years ascending in forward mode."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "shared_record_type": "sabinet_ccma_shared",
            "reverse_direction": False
        }
    }

    mocker.patch("extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    
    mock_conn = AsyncMock()
    mocker.patch("extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    
    # Mock db_storage in both absolute and relative module namespaces
    mocker.patch("db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    
    mocker.patch("db_storage.get_existing_urls", AsyncMock(return_value={"url1"}))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.get_existing_urls", AsyncMock(return_value={"url1"}))
    
    mocker.patch("db_storage.get_existing_dataset_numbers", AsyncMock(return_value={"case1"}))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.get_existing_dataset_numbers", AsyncMock(return_value={"case1"}))

    mock_progress = {
        "last_year": 2021,
        "last_month": 5,
        "last_completed": True
    }
    mocker.patch("db_storage.load_pipeline_state", AsyncMock(return_value=mock_progress))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.load_pipeline_state", AsyncMock(return_value=mock_progress))
    mocker.patch("db_storage.save_pipeline_state", AsyncMock())
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.save_pipeline_state", AsyncMock())

    # Mock SeleniumBase
    mock_sb_ctx = MagicMock()
    mock_sb = MagicMock()
    mock_sb_ctx.__enter__.return_value = mock_sb
    mocker.patch("extraction_workers.sabinet_scraper.SB", return_value=mock_sb_ctx)

    # Return some mock years (2020, 2021, 2022)
    def custom_execute_script(script):
        if "Year-items" in script:
            return [[2020, 100], [2021, 50], [2022, 10]]
        if "btn-search" in script:
            return True
        return []
    mock_sb.execute_script.side_effect = custom_execute_script
    
    mock_sb.is_element_present.return_value = True
    
    def custom_is_element_visible(selector):
        if "a[rel=\"next\"]" in selector or "ant-pagination-next" in selector:
            return False
        return True
    mock_sb.is_element_visible.side_effect = custom_is_element_visible

    scraper = SabinetScraper("sabinet_test")
    await scraper.initialize()
    await scraper.indexing()

    type_calls = [
        args[0][1] for args in mock_sb.type.call_args_list 
        if args[0][0] == 'input[placeholder="Date From"]'
    ]
    assert len(type_calls) == 8 + 12  # 8 months in 2021 + 12 months in 2022
    assert type_calls[0] == "05/01/2021"
    assert type_calls[1] == "06/01/2021"
    assert type_calls[-1] == "12/01/2022"


@pytest.mark.asyncio
async def test_detailing_batch_loop(mocker):
    """Verify that SabinetScraper.detailing processes records in batches and handles sentinels."""
    mock_config = {
        "start_url": "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards",
        "document_type": "awards",
        "extraction_params": {
            "shared_record_type": "sabinet_ccma_shared",
            "concurrency": 2
        }
    }

    mocker.patch("extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    
    mock_conn = AsyncMock()
    mocker.patch("extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    
    mocker.patch("db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    
    mocker.patch("db_storage.get_existing_urls", AsyncMock(return_value=set()))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.get_existing_urls", AsyncMock(return_value=set()))
    
    mocker.patch("db_storage.get_existing_dataset_numbers", AsyncMock(return_value=set()))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.get_existing_dataset_numbers", AsyncMock(return_value=set()))
    
    mocker.patch("db_storage.load_pipeline_state", AsyncMock(return_value={}))
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.load_pipeline_state", AsyncMock(return_value={}))
    mocker.patch("db_storage.save_pipeline_state", AsyncMock())
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.save_pipeline_state", AsyncMock())

    # Mock total count is 2 records needing detailing
    mock_conn.fetchval = AsyncMock(return_value=2)

    # Mock cases to load
    mock_cases = [
        {"id": "uuid-1", "source_url": "https://example.com/case1"},
        {"id": "uuid-2", "source_url": "https://example.com/case2"}
    ]
    
    mock_load = AsyncMock()
    mock_load.side_effect = [mock_cases, []]
    mocker.patch("extraction_workers.sabinet_scraper.db_storage.load_records_needing_detail", mock_load)

    processed_items = []
    def mock_worker(*args, **kwargs):
        try:
            work_queue = kwargs.get("work_queue")
            while True:
                item = work_queue.get()
                if item is None:
                    work_queue.task_done()
                    break
                processed_items.append(item)
                work_queue.task_done()
        except Exception as e:
            import traceback
            traceback.print_exc()

    mocker.patch.object(SabinetScraper, "_detailing_worker_thread", side_effect=mock_worker)

    scraper = SabinetScraper("sabinet_test")
    await scraper.initialize()
    await scraper.detailing()

    assert mock_load.call_count == 2
    mock_load.assert_any_call(
        mock_conn,
        "sabinet_ccma_shared",
        limit=500,
        exclude_ids=None,
        include_data=False
    )

    assert len(processed_items) == 2
    assert processed_items[0][1]["id"] == "uuid-1"
    assert processed_items[1][1]["id"] == "uuid-2"
