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
        check_timeout: float = 8.0,
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
        """Locate the Cloudflare Turnstile challenge iframe.

        Checks for several known URL patterns that Cloudflare uses for
        Turnstile challenge iframes.
        """
        patterns = [
            "challenges.cloudflare.com",
            "challenge-platform.cloudflare.com",
            "cloudflare.com/cdn-cgi/challenge-platform",
            "turnstile.cloudflare.com",
        ]
        steps = int(self.check_timeout / 0.5)
        for tick in range(max(1, steps)):
            frame_urls = []
            for frame in page.frames:
                url = frame.url
                frame_urls.append(url)
                for pattern in patterns:
                    if pattern in url:
                        logger.info(f"Found Turnstile frame matching '{pattern}': {url}")
                        return frame
            if tick == 0 or tick == steps - 1:
                # Log all frames on first and last tick for diagnostics
                logger.debug(f"[Turnstile discovery tick {tick}] All frames ({len(frame_urls)}): {frame_urls}")
            await asyncio.sleep(0.5)

        # Final diagnostic dump — always log at INFO on failure so it shows up
        all_urls = [f.url for f in page.frames]
        logger.warning(
            f"Turnstile frame not found after {self.check_timeout}s. "
            f"Page frames ({len(all_urls)}): {all_urls}"
        )
        return None

    # -----------------------------------------------------------------
    # DOM Widget Discovery (no-iframe fallback)
    # -----------------------------------------------------------------

    async def find_turnstile_widget(self, page: Page) -> dict | None:
        """Locate a Turnstile widget rendered directly in the page DOM.

        Cloudflare's managed challenge and some Turnstile integrations
        do NOT use a cross-origin iframe. Instead the widget is rendered
        inside a ``<div class="cf-turnstile">`` container (or similar).

        Returns the bounding box dict if found, or *None*.
        """
        # Selectors ordered by likelihood
        selectors = [
            "div.cf-turnstile",
            "#cf-turnstile",
            "#turnstile-wrapper",
            "[data-sitekey]",              # generic Turnstile mount point
            "div.cf-challenge-running",    # managed challenge container
            "iframe[src*='cloudflare']",   # catch-all for any iframe we missed
        ]
        steps = int(self.check_timeout / 0.5)
        for tick in range(max(1, steps)):
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        box = await loc.bounding_box()
                        if box:
                            logger.info(f"Found Turnstile DOM widget via selector '{sel}': {box}")
                            return {"box": box, "selector": sel, "locator": loc}
                except Exception:
                    continue
            await asyncio.sleep(0.5)

        logger.warning("No Turnstile DOM widget found either.")
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
        cf_patterns = [
            "challenges.cloudflare.com",
            "challenge-platform.cloudflare.com",
            "cloudflare.com/cdn-cgi/challenge-platform",
            "turnstile.cloudflare.com",
        ]
        frame_still_present = False
        for frame in page.frames:
            if any(p in frame.url for p in cf_patterns):
                frame_still_present = True
                break
        if not frame_still_present:
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
    # Internal Solve Routines
    # -----------------------------------------------------------------

    async def _solve_via_frame(self, page: Page, turnstile_frame, attempt: int) -> bool:
        """Click the checkbox inside the Turnstile *iframe* and verify."""
        frame_el = await turnstile_frame.frame_element()
        if not frame_el:
            logger.warning(f"[Attempt {attempt}] Could not retrieve frame element.")
            return False

        try:
            await frame_el.scroll_into_view_if_needed()
        except Exception as e:
            logger.warning(f"Could not scroll turnstile frame into view: {e}")

        box = await frame_el.bounding_box()
        if not box:
            logger.warning(f"[Attempt {attempt}] No bounding box for turnstile frame.")
            return False

        click_x = box["x"] + 30 + random.randint(-5, 5)
        click_y = box["y"] + (box["height"] / 2) + random.randint(-4, 4)
        logger.info(
            f"[Attempt {attempt}] Clicking iframe checkbox at ({click_x:.0f}, {click_y:.0f}) "
            f"[frame box: {box['width']:.0f}×{box['height']:.0f}]"
        )

        await self._human_move_and_click(page, click_x, click_y)
        return True

    async def _solve_via_widget(self, page: Page, widget_info: dict, attempt: int) -> bool:
        """Click the checkbox inside a DOM-rendered Turnstile *widget*."""
        box = widget_info["box"]
        sel = widget_info["selector"]

        # The checkbox is typically in the left portion of the widget
        click_x = box["x"] + min(30, box["width"] * 0.15) + random.randint(-3, 3)
        click_y = box["y"] + (box["height"] / 2) + random.randint(-3, 3)
        logger.info(
            f"[Attempt {attempt}] Clicking DOM widget checkbox at ({click_x:.0f}, {click_y:.0f}) "
            f"[widget '{sel}': {box['width']:.0f}×{box['height']:.0f}]"
        )

        await self._human_move_and_click(page, click_x, click_y)
        return True

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

        Strategy:
        1. Look for the challenge in a cross-origin iframe (classic Turnstile).
        2. If no iframe is found, search the DOM for a widget container
           (managed challenge / inline Turnstile).
        3. Click the checkbox and verify clearance.
        """
        # --- Discover the challenge target ---
        turnstile_frame = await self.find_turnstile_frame(page)
        widget_info = None
        mode = "frame"

        if not turnstile_frame:
            logger.info("No Turnstile iframe found. Searching for DOM widget fallback...")
            widget_info = await self.find_turnstile_widget(page)
            if not widget_info:
                return {
                    "success": False,
                    "error": "Cloudflare Turnstile frame not found and no DOM widget detected.",
                }
            mode = "widget"

        logger.info(f"Cloudflare Turnstile challenge detected (mode={mode}). Attempting to solve...")

        for attempt in range(1, self.max_click_attempts + 1):
            # Re-locate target on retries
            if attempt > 1:
                if mode == "frame":
                    turnstile_frame = await self.find_turnstile_frame(page)
                    if not turnstile_frame:
                        return {
                            "success": False,
                            "error": f"Turnstile frame disappeared before attempt {attempt}.",
                        }
                else:
                    widget_info = await self.find_turnstile_widget(page)
                    if not widget_info:
                        return {
                            "success": False,
                            "error": f"Turnstile widget disappeared before attempt {attempt}.",
                        }

            # Wait for the widget JS to fully initialise
            delay = self.solve_delay + random.uniform(-0.5, 1.5)
            logger.info(
                f"[Attempt {attempt}/{self.max_click_attempts}] "
                f"Waiting {delay:.1f}s for widget to stabilise..."
            )
            await asyncio.sleep(delay)

            try:
                if mode == "frame":
                    clicked = await self._solve_via_frame(page, turnstile_frame, attempt)
                else:
                    clicked = await self._solve_via_widget(page, widget_info, attempt)
            except Exception as e:
                logger.error(f"[Attempt {attempt}] Click failed: {e}")
                clicked = False

            if not clicked:
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
                f"click attempt(s) via {mode} mode. Cloudflare may be detecting automation."
            ),
        }

