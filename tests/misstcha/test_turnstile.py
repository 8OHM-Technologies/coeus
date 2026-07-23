import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from misstcha.turnstile import TurnstileSolver


@pytest.mark.asyncio
async def test_find_turnstile_frame_success():
    solver = TurnstileSolver(check_timeout=1.0)
    
    mock_frame_other = MagicMock()
    mock_frame_other.url = "https://some-other-site.com"
    
    mock_frame_cloudflare = MagicMock()
    mock_frame_cloudflare.url = "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/ov2"

    mock_page = MagicMock()
    mock_page.frames = [mock_frame_other, mock_frame_cloudflare]

    found = await solver.find_turnstile_frame(mock_page)
    assert found == mock_frame_cloudflare


@pytest.mark.asyncio
async def test_find_turnstile_frame_not_found():
    solver = TurnstileSolver(check_timeout=0.2)
    
    mock_frame_other = MagicMock()
    mock_frame_other.url = "https://some-other-site.com"

    mock_page = MagicMock()
    mock_page.frames = [mock_frame_other]

    found = await solver.find_turnstile_frame(mock_page)
    assert found is None


@pytest.mark.asyncio
async def test_solve_turnstile_success():
    solver = TurnstileSolver()
    
    # Mock Playwright page
    mock_page = MagicMock()
    mock_page.url = "https://nowsecure.nl/"
    mock_page.wait_for_selector = AsyncMock()
    
    # Mock SeleniumBase tab
    mock_tab = MagicMock()
    mock_tab.url = "https://nowsecure.nl/"
    
    # Mock SeleniumBase driver
    mock_driver = MagicMock()
    del mock_driver.cdp_base
    mock_driver.tabs = [mock_tab]
    # Mock update_targets to return a dummy coroutine/object
    mock_driver.update_targets.return_value = asyncio.Future()
    mock_driver.update_targets.return_value.set_result(None)
    
    # Mock SeleniumBase sb instance
    mock_sb = MagicMock()
    mock_sb.driver = mock_driver
    mock_sb.loop = MagicMock()
    mock_sb.loop.run_until_complete = MagicMock()
    mock_sb.switch_to_tab = MagicMock()
    mock_sb.solve_captcha = MagicMock()
    
    result = await solver.solve(mock_page, sb=mock_sb, wait_selector=".success-indicator", wait_timeout=1.0)
    
    # Assertions
    mock_driver.update_targets.assert_called_once()
    mock_sb.loop.run_until_complete.assert_called_once_with(mock_driver.update_targets.return_value)
    mock_sb.switch_to_tab.assert_called_once_with(mock_tab)
    mock_sb.solve_captcha.assert_called_once()
    mock_page.wait_for_selector.assert_called_once_with(".success-indicator", timeout=1000.0)
    
    assert result["success"] is True
    assert result["error"] is None
