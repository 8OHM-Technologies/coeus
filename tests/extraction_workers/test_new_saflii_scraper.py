import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
import requests

# Ensure parent of project root is in path so package-level relative imports work
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from coeus.extraction_workers.new_saflii_scraper import (
    check_page_state,
    parse_dataset_url,
    extract_dataset_number_from_text,
    extract_dataset_code_from_url,
    extract_metadata_by_category,
    DATABASES_INDEX_URL,
)


def test_check_page_state():
    # Cloudflare / turnstile blocking indicators (checked on page_title or body_text)
    assert check_page_state("Just a moment...", "", "") == "BLOCKED"
    assert check_page_state("Security Verification", "", "") == "BLOCKED"
    assert check_page_state("", "", "please verify you are human to continue") == "BLOCKED"

    # Apache / standard forbidden & not found errors (checked on page_title or h1_title)
    assert check_page_state("404 Not Found", "", "") == "NOT_FOUND"
    assert check_page_state("", "403 Forbidden", "") == "BLOCKED"
    assert check_page_state("", "", "you don't have permission to access this resource") == "BLOCKED"

    # Valid page
    assert check_page_state("SAFLII Judgments", "Constitutional Court", "Case details here...") == "OK"


def test_parse_dataset_url():
    # Standard SAFLII URL: /za/cases/ZACC/2026/1.html
    court, year, entry_id = parse_dataset_url("https://www.saflii.org/za/cases/ZACC/2026/1.html")
    assert court == "ZACC"
    assert year == "2026"
    assert entry_id == "1"

    # Deep URL: /za/cases/ZAGPJHC/2025/123.html
    court, year, entry_id = parse_dataset_url("https://www.saflii.org/za/cases/ZAGPJHC/2025/123.html")
    assert court == "ZAGPJHC"
    assert year == "2025"
    assert entry_id == "123"

    # Non-conforming URL should fallback to default
    court, year, entry_id = parse_dataset_url("https://www.invalid/format")
    assert court == "SAFLII"
    assert year == "unknown"
    assert entry_id == "unknown"


def test_extract_dataset_code_from_url():
    # Standard cases URL
    assert extract_dataset_code_from_url("https://www.saflii.org/za/cases/ZACC/") == "ZACC"
    assert extract_dataset_code_from_url("https://www.saflii.org/za/cases/ZAGPJHC/2025/1.html") == "ZAGPJHC"

    # Gazette URL
    assert extract_dataset_code_from_url("https://www.saflii.org/za/gaz/ZANGAZ/") == "ZANGAZ"

    # Journal URL
    assert extract_dataset_code_from_url("https://www.saflii.org/za/journals/DEIJURE/") == "DEIJURE"

    # Other URL
    assert extract_dataset_code_from_url("https://www.saflii.org/za/other/ZAJSC/") == "ZAJSC"

    # Non-matching URL
    assert extract_dataset_code_from_url("https://www.saflii.org/content/databases.html") is None

    # Non-SA URL
    assert extract_dataset_code_from_url("https://www.saflii.org/ls/cases/LSHC/") is None


def test_extract_metadata_by_category():
    # 1. Cases
    text_cases = "Case No: 123/2025\nCitation: [2025] ZACC 10"
    meta_cases = extract_metadata_by_category("cases", "S v Zuma", text_cases)
    assert meta_cases.get("case_number") == "123/2025"
    assert meta_cases.get("citation") == "[2025] ZACC 10"

    # 2. Gazettes
    text_gaz = "Government Gazette No: 45678"
    meta_gaz = extract_metadata_by_category("gaz", "Gazette Title", text_gaz)
    assert meta_gaz.get("gazette_number") == "45678"

    # 3. Journals
    text_journal = "DE JURE Vol 50 No 2"
    meta_journal = extract_metadata_by_category("journals", "Journal Title", text_journal)
    assert meta_journal.get("volume") == "50"
    assert meta_journal.get("issue") == "2"


def test_basic_scraper_connectivity():
    """
    A basic connectivity integration test to check that the scraper's
    target endpoint (SAFLII start_url or main site) is reachable.
    """
    target_url = "https://www.saflii.org/"
    try:
        response = requests.get(
            target_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
            },
            timeout=10,
        )
        assert response.status_code in (200, 403), f"Failed to connect: Status code {response.status_code}"
        print(f"\n[Connectivity Test] Successfully connected to {target_url} (status: {response.status_code})!")
    except Exception as e:
        pytest.fail(f"Connectivity check failed for {target_url}: {e}")


def test_extract_dataset_number_from_text():
    assert extract_dataset_number_from_text("S v Zuma (1/2026)") == "1/2026"
    assert extract_dataset_number_from_text("A v B (JR2672/2021) [2026] ZALC 1") == "JR2672/2021"
    assert extract_dataset_number_from_text("Smith v State (123/15) (15 January 2026)") == "123/15"
    assert extract_dataset_number_from_text("No case number in here") is None
    assert extract_dataset_number_from_text("Date in paren (15 January 2026)") is None


@pytest.mark.asyncio
async def test_saflii_scraper_initialize_defaults(mocker):
    """Test that the refactored scraper initializes with sensible defaults and reads config correctly."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    mock_config = {
        "start_url": "",
        "document_type": "awards",
        "extraction_params": {
            "datasets": "ZACC,ZAGPJHB",
            "shared_record_type": "saflii_courts",
        },
    }
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mocker.patch("coeus.extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    mocker.patch("extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    mocker.patch("coeus.extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    mocker.patch("extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    mocker.patch("db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    mocker.patch("db_storage.get_existing_urls", AsyncMock(return_value=set()))
    mocker.patch("db_storage.get_existing_dataset_numbers", AsyncMock(return_value=set()))

    scraper = SafliiScraper(pipeline_name="saflii_test")
    await scraper.initialize()

    # Default index URL should be used when start_url is empty
    assert scraper.index_url == DATABASES_INDEX_URL

    # Dataset filter should be parsed from config extraction_params
    assert scraper.dataset_filter == ["ZACC", "ZAGPJHB"]


@pytest.mark.asyncio
async def test_saflii_scraper_datasets_from_cli(mocker):
    """Test that CLI-supplied datasets take precedence over config."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    mock_config = {
        "start_url": "",
        "document_type": "awards",
        "extraction_params": {
            "datasets": "ZACC",
            "shared_record_type": "saflii_courts",
        },
    }
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mocker.patch("coeus.extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    mocker.patch("extraction_workers.base_scraper.fetch_pipeline_config", AsyncMock(return_value=mock_config))
    mocker.patch("coeus.extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    mocker.patch("extraction_workers.base_scraper.get_db_connection", AsyncMock(return_value=mock_conn))
    mocker.patch("db_storage.resolve_target_id", AsyncMock(return_value="target-123"))
    mocker.patch("db_storage.get_existing_urls", AsyncMock(return_value=set()))
    mocker.patch("db_storage.get_existing_dataset_numbers", AsyncMock(return_value=set()))

    # CLI datasets override config datasets
    scraper = SafliiScraper(pipeline_name="saflii_test", datasets=["ZAGPJHB", "ZAWCHC"])
    await scraper.initialize()

    assert scraper.dataset_filter == ["ZAGPJHB", "ZAWCHC"]


def test_turnstile_block_triggers_ip_rotation(mocker):
    """Test that encountering a Turnstile block immediately re-queues the item and raises to rotate IP."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    scraper = SafliiScraper(pipeline_name="saflii_test")
    mocker.patch.object(scraper, "_navigate_and_handle_turnstile", return_value="BLOCKED")

    import queue
    work_queue = queue.Queue()
    work_queue.put((1, "https://www.saflii.org/za/cases/ZAGPPHC/2010/585.html", 1))

    # Mock SB context manager to throw BlockedException on exit when _navigate_and_handle_turnstile returns BLOCKED
    mock_sb_ctx = MagicMock()
    mock_sb = MagicMock()
    mock_sb_ctx.__enter__.return_value = mock_sb
    mocker.patch("coeus.extraction_workers.new_saflii_scraper.SB", return_value=mock_sb_ctx)

    loop = AsyncMock()

    # Limit while True loop to 1 cycle by putting sentinel None after the first item is re-queued
    session_count = 0
    def mock_ip(sb, label=""):
        nonlocal session_count
        session_count += 1
        if session_count >= 1:
            work_queue.put(None)

    mocker.patch.object(scraper, "log_outbound_ip", side_effect=mock_ip)

    scraper._detailing_worker_thread(
        worker_id=1,
        work_queue=work_queue,
        loop=loop,
        total_datasets=1,
    )

    # Verify that item was re-queued with pass_number 2
    items = []
    while not work_queue.empty():
        item = work_queue.get_nowait()
        if item is not None:
            items.append(item)

    assert (1, "https://www.saflii.org/za/cases/ZAGPPHC/2010/585.html", 2) in items
