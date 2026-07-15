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
async def test_solve_turnstile_success(mocker):
    # Shorten delays for test speed
    solver = TurnstileSolver(check_timeout=0.1, solve_delay=0.1)

    # Mock the cloudflare frame
    mock_frame = AsyncMock()
    mock_frame.url = "https://challenges.cloudflare.com/turnstile"
    
    # Mock locator for checkbox input
    mock_locator = AsyncMock()
    mock_frame.locator.return_value = mock_locator
    
    # Mock frame element
    mock_frame_el = AsyncMock()
    mock_frame_el.bounding_box.return_value = {"x": 100, "y": 200, "width": 300, "height": 65}
    mock_frame.frame_element.return_value = mock_frame_el

    # Mock page
    mock_page = AsyncMock()
    mock_page.frames = [mock_frame]

    # Mock screenshot method to avoid disk writes
    mocker.patch.object(solver, "_take_screenshot", new_callable=AsyncMock)

    result = await solver.solve(mock_page, wait_selector=".success-indicator", wait_timeout=1.0)

    # Verify mouse clicks were invoked
    mock_page.mouse.move.assert_called()
    mock_page.mouse.down.assert_called_once()
    mock_page.mouse.up.assert_called_once()
    mock_page.wait_for_selector.assert_called_once_with(".success-indicator", timeout=1000.0)
    
    assert result["success"] is True
    assert result["error"] is None
