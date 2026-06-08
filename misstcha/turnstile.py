import asyncio
import logging
from typing import Any, Dict
from playwright.async_api import Page
from .base import BaseSolver

logger = logging.getLogger(__name__)


class TurnstileSolver(BaseSolver):
    """Solver for Cloudflare Turnstile verification challenge."""

    def __init__(self, check_timeout: float = 5.0, solve_delay: float = 7.0):
        self.check_timeout = check_timeout
        self.solve_delay = solve_delay

    async def find_turnstile_frame(self, page: Page):
        """Locates the Cloudflare Turnstile challenge iframe on the page."""
        steps = int(self.check_timeout / 0.5)
        for _ in range(max(1, steps)):
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    return frame
            await asyncio.sleep(0.5)
        return None

    async def solve(
        self,
        page: Page,
        wait_selector: str = None,
        wait_timeout: float = 15.0,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """Attempts to find and solve the Cloudflare Turnstile checkbox on the page."""
        turnstile_frame = await self.find_turnstile_frame(page)
        if not turnstile_frame:
            return {"success": False, "error": "Cloudflare Turnstile frame not found."}

        logger.info("Cloudflare Turnstile challenge detected. Attempting to solve...")
        await asyncio.sleep(self.solve_delay)

        frame_el = await turnstile_frame.frame_element()
        if not frame_el:
            return {"success": False, "error": "Could not retrieve frame element."}

        box = await frame_el.bounding_box()
        if not box:
            return {
                "success": False,
                "error": "Could not retrieve bounding box of turnstile frame.",
            }

        click_x = box["x"] + 30
        click_y = box["y"] + 32
        logger.info(f"Clicking verification checkbox at ({click_x}, {click_y})")

        try:
            await page.mouse.move(click_x, click_y)
            await asyncio.sleep(0.3)
            await page.mouse.click(click_x, click_y)

            # If a selector is provided, wait for it to confirm success/navigation
            if wait_selector:
                logger.info(f"Waiting for selector: {wait_selector}")
                try:
                    await page.wait_for_selector(
                        wait_selector, timeout=wait_timeout * 1000
                    )
                except Exception as e:
                    logger.warning(
                        f"Timeout waiting for selector {wait_selector} after click: {e}"
                    )
                    # Return success of click, but note warning
                    return {
                        "success": True,
                        "warning": f"Selector {wait_selector} not found after click.",
                        "error": None,
                    }

            return {"success": True, "error": None}
        except Exception as e:
            logger.error(f"Error solving Turnstile: {e}")
            return {"success": False, "error": str(e)}
