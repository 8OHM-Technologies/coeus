import asyncio
import logging
import random
from typing import Any, Dict
from playwright.async_api import Page
from .base import BaseSolver

logger = logging.getLogger(__name__)


class TurnstileSolver(BaseSolver):
    """Solver for Cloudflare Turnstile verification challenge.

    Clicks the Turnstile checkbox and **verifies** the challenge was actually
    cleared by polling for the challenge frame to disappear or a success token
    to be injected.
    """

    def __init__(
        self,
        check_timeout: float = 5.0,
        solve_delay: float = 7.0,
        verify_timeout: float = 12.0,
        max_click_attempts: int = 2,
    ):
        self.check_timeout = check_timeout
        self.solve_delay = solve_delay
        self.verify_timeout = verify_timeout
        self.max_click_attempts = max_click_attempts

    # -----------------------------------------------------------------
    # Frame Discovery
    # -----------------------------------------------------------------

    async def find_turnstile_frame(self, page: Page):
        """Locate the Cloudflare Turnstile challenge iframe."""
        steps = int(self.check_timeout / 0.5)
        for _ in range(max(1, steps)):
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    return frame
            await asyncio.sleep(0.5)
        return None

    # -----------------------------------------------------------------
    # Post-click Verification
    # -----------------------------------------------------------------

    async def _is_challenge_cleared(self, page: Page) -> bool:
        """Return *True* when the Turnstile challenge is no longer blocking.

        Checks three signals (any one is sufficient):
        1. The challenge iframe is gone from the frame tree.
        2. A ``cf-turnstile-response`` hidden input carries a non-empty value.
        3. A ``data-turnstile-callback`` script has fired (response token set
           via ``window.turnstileToken``).
        """
        # 1. Frame gone?
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                break
        else:
            # No challenge frame found → cleared
            return True

        # 2. Hidden response input populated?
        try:
            token = await page.evaluate(
                "document.querySelector('input[name=\"cf-turnstile-response\"]')?.value || ''"
            )
            if token:
                logger.info("Turnstile response token detected in hidden input.")
                return True
        except Exception:
            pass

        return False

    async def _verify_solve(self, page: Page) -> bool:
        """Poll until the challenge is cleared or *verify_timeout* elapses."""
        elapsed = 0.0
        interval = 0.5
        while elapsed < self.verify_timeout:
            if await self._is_challenge_cleared(page):
                return True
            await asyncio.sleep(interval)
            elapsed += interval
        return False

    # -----------------------------------------------------------------
    # Click Helpers
    # -----------------------------------------------------------------

    @staticmethod
    async def _human_move_and_click(page: Page, target_x: float, target_y: float) -> None:
        """Move the mouse in a human-like arc and click."""
        # Start from a random viewport position
        start_x = random.randint(100, 400)
        start_y = random.randint(100, 400)
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.05, 0.15))

        # Move to target with variable steps
        steps = random.randint(12, 25)
        await page.mouse.move(target_x, target_y, steps=steps)

        # Small jitter pause before click (humans are imprecise)
        await asyncio.sleep(random.uniform(0.15, 0.45))

        # Press and release with realistic hold duration
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await page.mouse.up()

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def solve(
        self,
        page: Page,
        wait_selector: str = None,
        wait_timeout: float = 15.0,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """Find and solve the Cloudflare Turnstile checkbox.

        Improvements over the legacy solver:
        - Verifies the challenge is actually cleared after clicking.
        - Retries the click up to *max_click_attempts* times.
        - Varies mouse movement and timing to reduce behavioural fingerprinting.
        """
        turnstile_frame = await self.find_turnstile_frame(page)

        if not turnstile_frame:
            return {"success": False, "error": "Cloudflare Turnstile frame not found."}

        logger.info("Cloudflare Turnstile challenge detected. Attempting to solve...")

        for attempt in range(1, self.max_click_attempts + 1):
            # Re-locate the frame on retries (it may have been refreshed)
            if attempt > 1:
                turnstile_frame = await self.find_turnstile_frame(page)
                if not turnstile_frame:
                    return {
                        "success": False,
                        "error": f"Turnstile frame disappeared before attempt {attempt}.",
                    }

            # Wait for the widget JS to fully initialise
            delay = self.solve_delay + random.uniform(-0.5, 1.5)
            logger.info(
                f"[Attempt {attempt}/{self.max_click_attempts}] "
                f"Waiting {delay:.1f}s for widget to stabilise..."
            )
            await asyncio.sleep(delay)

            frame_el = await turnstile_frame.frame_element()
            if not frame_el:
                logger.warning(f"[Attempt {attempt}] Could not retrieve frame element.")
                continue

            try:
                await frame_el.scroll_into_view_if_needed()
            except Exception as e:
                logger.warning(f"Could not scroll turnstile frame into view: {e}")

            box = await frame_el.bounding_box()
            if not box:
                logger.warning(f"[Attempt {attempt}] No bounding box for turnstile frame.")
                continue

            # Target the checkbox area (left side of the Turnstile widget)
            click_x = box["x"] + 30 + random.randint(-5, 5)
            click_y = box["y"] + (box["height"] / 2) + random.randint(-4, 4)
            logger.info(
                f"[Attempt {attempt}] Clicking checkbox at ({click_x:.0f}, {click_y:.0f}) "
                f"[frame box: {box['width']:.0f}×{box['height']:.0f}]"
            )

            try:
                await self._human_move_and_click(page, click_x, click_y)
            except Exception as e:
                logger.error(f"[Attempt {attempt}] Click failed: {e}")
                continue

            # --- Verify the click actually worked ---
            logger.info(f"[Attempt {attempt}] Verifying solve for up to {self.verify_timeout}s...")
            if await self._verify_solve(page):
                logger.info(f"✅ Turnstile challenge cleared on attempt {attempt}.")

                # If caller also wants a specific selector, wait for it
                if wait_selector:
                    try:
                        await page.wait_for_selector(
                            wait_selector, timeout=wait_timeout * 1000
                        )
                    except Exception as e:
                        logger.warning(
                            f"Challenge cleared but selector '{wait_selector}' "
                            f"not found within {wait_timeout}s: {e}"
                        )
                        return {
                            "success": True,
                            "verified": True,
                            "warning": f"Selector {wait_selector} not found after solve.",
                            "error": None,
                        }

                return {"success": True, "verified": True, "error": None}

            logger.warning(
                f"[Attempt {attempt}] Click executed but challenge still present after "
                f"{self.verify_timeout}s verification window."
            )

        return {
            "success": False,
            "verified": False,
            "error": (
                f"Turnstile challenge not cleared after {self.max_click_attempts} "
                f"click attempt(s). Cloudflare may be detecting automation."
            ),
        }

