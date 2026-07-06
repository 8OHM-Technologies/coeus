import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
import requests

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from new_saflii_scraper import check_page_state, parse_case_url, wait_for_page_load


def test_check_page_state():
    # Cloudflare / turnstile blocking indicators (checked on page_title or body_text)
    assert check_page_state("Just a moment...", "", "") == "BLOCKED"
    assert check_page_state("Security Verification", "", "") == "BLOCKED"
    assert check_page_state("", "", "please verify you are human to continue") == "BLOCKED"

    # Apache / standard forbidden & not found errors (checked on page_title or h1_title)
    assert check_page_state("404 Not Found", "", "") == "NOT_FOUND"
    assert check_page_state("", "403 Forbidden", "") == "NOT_FOUND"
    assert check_page_state("", "", "you don't have permission to access this resource") == "NOT_FOUND"

    # Valid page
    assert check_page_state("SAFLII Judgments", "Constitutional Court", "Case details here...") == "OK"


def test_parse_case_url():
    # Standard SAFLII URL: /za/cases/ZACC/2026/1.html
    court, year, case_id = parse_case_url("https://www.saflii.org/za/cases/ZACC/2026/1.html")
    assert court == "ZACC"
    assert year == "2026"
    assert case_id == "1"

    # Deep URL: /za/cases/ZAGPJHC/2025/123.html
    court, year, case_id = parse_case_url("https://www.saflii.org/za/cases/ZAGPJHC/2025/123.html")
    assert court == "ZAGPJHC"
    assert year == "2025"
    assert case_id == "123"

    # Non-conforming URL should fallback to default
    court, year, case_id = parse_case_url("https://www.saflii.org/invalid/format")
    assert court == "SAFLII"
    assert year == "unknown"
    assert case_id == "unknown"


@pytest.mark.asyncio
async def test_wait_for_page_load_ok():
    # Mock playwright page
    mock_page = AsyncMock()
    mock_page.title.return_value = "SAFLII Judgments"
    
    # Mock locator for h1
    mock_h1 = AsyncMock()
    mock_h1.count.return_value = 1
    mock_h1.first = AsyncMock()
    mock_h1.first.inner_text.return_value = "Constitutional Court"
    
    # Mock locator for body
    mock_body = AsyncMock()
    mock_body.count.return_value = 1
    mock_body.inner_text.return_value = "A long body text with more than 500 characters..." * 15
    
    # Assign locator behavior
    def locator_mock(selector):
        if selector == "h1":
            return mock_h1
        return mock_body
        
    mock_page.locator = locator_mock

    # Test "case" load type
    state = await wait_for_page_load(mock_page, url_type="case")
    assert state == "OK"


@pytest.mark.asyncio
async def test_wait_for_page_load_not_found():
    # Mock playwright page
    mock_page = AsyncMock()
    mock_page.title.return_value = "404 Not Found"
    
    # Mock locator for h1
    mock_h1 = AsyncMock()
    mock_h1.count.return_value = 1
    mock_h1.first = AsyncMock()
    mock_h1.first.inner_text.return_value = "Not Found"
    
    # Mock locator for body
    mock_body = AsyncMock()
    mock_body.count.return_value = 1
    mock_body.inner_text.return_value = "The requested URL was not found on this server."
    
    def locator_mock(selector):
        if selector == "h1":
            return mock_h1
        return mock_body
        
    mock_page.locator = locator_mock

    state = await wait_for_page_load(mock_page, url_type="case")
    assert state == "NOT_FOUND"


def test_basic_scraper_connectivity():
    """
    A basic connectivity integration test to check that the scraper's
    target endpoint (SAFLII start_url or main site) is reachable.
    Since SAFLII utilizes Cloudflare Turnstile, a 403 or 200 response
    both verify that network connectivity to the host is functioning.
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
