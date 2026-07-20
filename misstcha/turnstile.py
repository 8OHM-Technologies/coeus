import asyncio
import logging
import os
import random
from datetime import datetime
from typing import Any, Dict
from playwright.async_api import Page
from .base import BaseSolver

logger = logging.getLogger(__name__)


class TurnstileSolver(BaseSolver):
    """Solver for Cloudflare Turnstile verification challenge."""

    def __init__(self, check_timeout: float = 5.0, solve_delay: float = 7.0):
        self.check_timeout = check_timeout
        self.solve_delay = solve_delay

    async def _take_screenshot(self, page: Page, screenshot_dir: str, name: str):
        """Save a timestamped diagnostic screenshot if screenshot_dir is set."""
        if not screenshot_dir:
            return
        try:
            os.makedirs(screenshot_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            filename = f"{timestamp}_{name}.png"
            path = os.path.join(screenshot_dir, filename)
            await page.screenshot(path=path)
            logger.info(f"[TurnstileSolver] Saved screenshot: {path}")
        except Exception as e:
            logger.warning(f"[TurnstileSolver] Failed to take screenshot {name}: {e}")

    async def find_turnstile_frame(self, page: Page):
        """Locates the Cloudflare Turnstile challenge iframe on the page."""
        steps = int(self.check_timeout / 0.5)
        for _ in range(max(1, steps)):
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    return frame
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _human_move_and_click(page: Page, target_x: float, target_y: float) -> None:
        """Move the mouse in a human-like arc and click."""
        start_x = random.randint(0, 100)
        start_y = random.randint(0, 100)
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(0.1)

        steps = random.randint(10, 15)
        await page.mouse.move(target_x, target_y, steps=steps)
        await asyncio.sleep(random.uniform(0.2, 0.4))

        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.up()

    async def solve(
        self,
        page: Page,
        wait_selector: str = None,
        wait_timeout: float = 15.0,
        screenshot_dir: str = None,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """Attempts to find and solve the Cloudflare Turnstile checkbox on the page."""
        turnstile_frame = await self.find_turnstile_frame(page)
        
        await self._take_screenshot(page, screenshot_dir, "01_init")

        if not turnstile_frame:
            # Check for inline Cloudflare challenge elements (like "Verify you are human" buttons)
            logger.info("No Turnstile frame found. Checking for inline Cloudflare challenge button...")
            challenge_selectors = [
                "button:has-text('Verify you are human')",
                "input[type='button'][value='Verify you are human']",
                "#challenge-stage button",
                "#challenge-stage input[type='button']",
                "#challenge-stage",
            ]
            
            button_element = None
            for selector in challenge_selectors:
                try:
                    loc = page.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        button_element = loc
                        logger.info(f"Found inline Cloudflare challenge element matching selector: {selector}")
                        break
                except Exception:
                    continue

            if not button_element:
                await self._take_screenshot(page, screenshot_dir, "error_frame_not_found")
                return {"success": False, "error": "Cloudflare Turnstile frame and inline button not found."}

            logger.info("Inline Cloudflare challenge detected. Attempting to click...")
            await asyncio.sleep(self.solve_delay)
            await self._take_screenshot(page, screenshot_dir, "02_after_delay")

            try:
                await button_element.scroll_into_view_if_needed()
            except Exception as e:
                logger.warning(f"Could not scroll inline element into view: {e}")

            box = await button_element.bounding_box()
            if not box:
                await self._take_screenshot(page, screenshot_dir, "error_no_bounding_box")
                return {"success": False, "error": "Could not retrieve bounding box of inline challenge element."}

            await self._take_screenshot(page, screenshot_dir, "03_before_click")

            click_x = box["x"] + (box["width"] / 2) + random.randint(-4, 4)
            click_y = box["y"] + (box["height"] / 2) + random.randint(-4, 4)
            logger.info(f"Moving to and clicking inline challenge button at ({click_x}, {click_y})")

            try:
                await self._human_move_and_click(page, click_x, click_y)
                await self._take_screenshot(page, screenshot_dir, "04_after_click")

                if wait_selector:
                    logger.info(f"Waiting for selector: {wait_selector}")
                    try:
                        await page.wait_for_selector(wait_selector, timeout=wait_timeout * 1000)
                        await self._take_screenshot(page, screenshot_dir, "05_selector_found")
                    except Exception as e:
                        logger.warning(f"Timeout waiting for selector {wait_selector} after click: {e}")
                        await self._take_screenshot(page, screenshot_dir, "error_selector_timeout")
                        return {
                            "success": True,
                            "warning": f"Selector {wait_selector} not found after click.",
                            "error": None,
                        }

                await self._take_screenshot(page, screenshot_dir, "06_success")
                return {"success": True, "error": None}
            except Exception as e:
                logger.error(f"Error clicking inline challenge button: {e}")
                await self._take_screenshot(page, screenshot_dir, "error_exception")
                return {"success": False, "error": str(e)}

        logger.info("Cloudflare Turnstile challenge iframe detected. Attempting to solve...")

        try:
            logger.info("Waiting for Turnstile widget to fully load inside the frame...")
            await turnstile_frame.wait_for_load_state("load", timeout=15000)
            checkbox_locator = turnstile_frame.locator('input[type="checkbox"]')
            await checkbox_locator.wait_for(state="attached", timeout=15000)
            logger.info("Turnstile widget is fully loaded.")
        except Exception as e:
            logger.warning(f"Wait for Turnstile widget loading timed out or failed: {e}")

        await asyncio.sleep(self.solve_delay)

        await self._take_screenshot(page, screenshot_dir, "02_after_delay")

        frame_el = await turnstile_frame.frame_element()
        if not frame_el:
            await self._take_screenshot(page, screenshot_dir, "error_no_frame_element")
            return {"success": False, "error": "Could not retrieve frame element."}

        try:
            await frame_el.scroll_into_view_if_needed()
        except Exception as e:
            logger.warning(f"Could not scroll turnstile frame into view: {e}")

        box = await frame_el.bounding_box()
        if not box:
            await self._take_screenshot(page, screenshot_dir, "error_no_bounding_box")
            return {
                "success": False,
                "error": "Could not retrieve bounding box of turnstile frame.",
            }

        await self._take_screenshot(page, screenshot_dir, "03_before_click")

        click_x = box["x"] + 30 + random.randint(-4, 4)
        click_y = box["y"] + 32 + random.randint(-4, 4)
        logger.info(f"Moving to and clicking Turnstile checkbox at ({click_x}, {click_y}) with human-like steps")

        try:
            await self._human_move_and_click(page, click_x, click_y)

            await self._take_screenshot(page, screenshot_dir, "04_after_click")

            # If a selector is provided, wait for it to confirm success/navigation
            if wait_selector:
                logger.info(f"Waiting for selector: {wait_selector}")
                try:
                    await page.wait_for_selector(
                        wait_selector, timeout=wait_timeout * 1000
                    )
                    await self._take_screenshot(page, screenshot_dir, "05_selector_found")
                except Exception as e:
                    logger.warning(
                        f"Timeout waiting for selector {wait_selector} after click: {e}"
                    )
                    await self._take_screenshot(page, screenshot_dir, "error_selector_timeout")
                    return {
                        "success": True,
                        "warning": f"Selector {wait_selector} not found after click.",
                        "error": None,
                    }

            await self._take_screenshot(page, screenshot_dir, "06_success")
            return {"success": True, "error": None}
        except Exception as e:
            logger.error(f"Error solving Turnstile: {e}")
            await self._take_screenshot(page, screenshot_dir, "error_exception")
            return {"success": False, "error": str(e)}
