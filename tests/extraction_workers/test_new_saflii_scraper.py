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
    extract_date_from_title,
    DATABASES_INDEX_URL,
)
from datetime import date


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


def test_extract_date_from_title():
    # Primary user specified format: brackets at the end of the title string
    title_primary = (
        "Caxton CTP Publishers and Printers Limited and Naspers Ltd / Electronic Media "
        "Network Ltd / Supersport International Holdings Ltd / Competition Commission "
        "(16/FN/Mar04) [2004] ZACT 25; [2004] 1 CPLR 217 (CT) (13 April 2004)"
    )
    assert extract_date_from_title(title_primary) == date(2004, 4, 13)

    # Various date variations and trailing characters
    assert extract_date_from_title("S v Dlamini (CC12/2020) [2021] ZAGPPHC 1 (15 January 2021)") == date(2021, 1, 15)
    assert extract_date_from_title("Minister of Police v Smith (123/2019) [2020] ZASCA 50 (28 May 2020)   ") == date(2020, 5, 28)
    assert extract_date_from_title("Some Case [2022] ZAWCHC 10 (12/03/2022)") == date(2022, 3, 12)
    assert extract_date_from_title("Some Case [2022] ZAWCHC 10 (2022-03-12)") == date(2022, 3, 12)
    assert extract_date_from_title("Some Case (01 Dec 2023).") == date(2023, 12, 1)
    assert extract_date_from_title("Some Case without date brackets") is None
    assert extract_date_from_title("") is None
    assert extract_date_from_title(None) is None


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
    """Test that encountering a Turnstile block retries the item immediately in the next browser sessions up to 6 passes."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    scraper = SafliiScraper(pipeline_name="saflii_test")
    scraper.warmup_session = False
    mocker.patch.object(scraper, "_navigate_and_handle_turnstile", return_value="BLOCKED")

    import queue
    work_queue = queue.Queue()
    work_queue.put((1, "https://www.saflii.org/za/cases/ZAGPPHC/2010/585.html", 1))

    # Mock SB context manager
    mock_sb_ctx = MagicMock()
    mock_sb = MagicMock()
    mock_sb_ctx.__enter__.return_value = mock_sb
    mocker.patch("coeus.extraction_workers.new_saflii_scraper.SB", return_value=mock_sb_ctx)

    loop = AsyncMock()

    # Limit while True loop: put None to terminate if it attempts to read next queue item
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

    # Verify that the scraper retried the same item immediately for 6 passes
    calls = scraper._navigate_and_handle_turnstile.call_args_list
    assert len(calls) == 6
    assert calls[0][0][2] == "worker_1_dataset_585_pass_1_att_1"
    assert calls[1][0][2] == "worker_1_dataset_585_pass_2_att_1"
    assert calls[5][0][2] == "worker_1_dataset_585_pass_6_att_1"

    # Verify that the item was not re-queued to the back of the shared queue
    items = []
    while not work_queue.empty():
        item = work_queue.get_nowait()
        if item is not None:
            items.append(item)

    assert (1, "https://www.saflii.org/za/cases/ZAGPPHC/2010/585.html", 2) not in items


def test_proxy_state_transition_triggers_recycling(mocker):
    """Test that a proxy state transition correctly updates worker_current_use_proxy and recycles the SB session."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    scraper = SafliiScraper(pipeline_name="saflii_test")
    scraper.warmup_session = False
    scraper.proxy_url = "http://username:password@proxy.example.com:8080"
    scraper.use_proxy = False  # Start with proxy disabled (worker_original_use_proxy)

    import queue
    work_queue = queue.Queue()
    # pass_number = 4 triggers proxy state transition expected_use_proxy = True
    work_queue.put((1, "https://www.saflii.org/za/cases/ZACC/2026/1.html", 4))

    # Mock all extraction operations to avoid real calls
    scraper._is_duplicate_url = MagicMock(return_value=False)
    scraper._navigate_and_handle_turnstile = MagicMock(return_value="OK")
    scraper._extract_page_content = MagicMock(return_value=("Title", "HTML", "Text", False))
    scraper._save_scraped_result = MagicMock()
    scraper._mark_scraped_state = MagicMock()
    scraper.log_outbound_ip = MagicMock()

    # Track how SB is initialized
    sb_calls = []
    mock_sb_ctx = MagicMock()
    mock_sb = MagicMock()
    mock_sb_ctx.__enter__.return_value = mock_sb

    def mock_sb_init(*args, **kwargs):
        sb_calls.append(kwargs)
        return mock_sb_ctx

    mocker.patch("coeus.extraction_workers.new_saflii_scraper.SB", side_effect=mock_sb_init)

    loop = AsyncMock()

    scraper._detailing_worker_thread(
        worker_id=1,
        work_queue=work_queue,
        loop=loop,
        total_datasets=1,
    )

    # The detailing thread should run, encounter the proxy transition exception, recycle,
    # and then run the second session with proxy enabled.
    assert len(sb_calls) == 2
    # First SB session (proxy disabled)
    assert sb_calls[0].get("proxy") is None
    assert sb_calls[0].get("multi_proxy") is False
    # Second SB session (proxy enabled)
    assert sb_calls[1].get("proxy") == "username:password@proxy.example.com:8080"
    assert sb_calls[1].get("multi_proxy") is True


@pytest.mark.asyncio
async def test_saflii_scraper_skip_stages_from_config(mocker):
    """Test that skip_stages is correctly loaded from extraction_params configuration."""
    from coeus.extraction_workers.new_saflii_scraper import SafliiScraper

    mock_config = {
        "start_url": "",
        "document_type": "awards",
        "extraction_params": {
            "datasets": "ZACC",
            "shared_record_type": "saflii_courts",
            "skip_stages": "1",
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

    # skip_stages should be parsed from config extraction_params
    assert scraper.skip_stages == frozenset({1})

