import asyncio
import logging
from typing import Any, Dict
from playwright.async_api import Page
from .base import BaseSolver

logger = logging.getLogger(__name__)


class TurnstileSolver(BaseSolver):
    """Solver for Cloudflare Turnstile verification challenge using SeleniumBase."""

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
        screenshot_dir: str = None,
        sb: Any = None,
        *args,
        **kwargs,
    ) -> Dict[str, Any]:
        """Attempts to solve the Cloudflare Turnstile challenge using SeleniumBase."""
        if not sb:
            logger.error("[TurnstileSolver] SeleniumBase (sb) instance must be provided.")
            return {"success": False, "error": "SeleniumBase instance not provided"}

        logger.info("[TurnstileSolver] Initiating SeleniumBase Turnstile solve...")
        try:
            loop = asyncio.get_running_loop()
            
            # 1. Update targets and switch to correct tab on sb's thread/loop
            def update_and_switch():
                driver = sb.driver
                if hasattr(driver, "cdp_base"):
                    driver = driver.cdp_base
                
                sb.loop.run_until_complete(driver.update_targets())
                
                # Find the tab matching the Playwright page URL
                current_url = page.url
                target_tab = None
                for tab in driver.tabs:
                    if tab.url == current_url or current_url.startswith(tab.url) or tab.url.startswith(current_url):
                        target_tab = tab
                        break
                
                # Fallback to match by URL domains/paths if exact match fails
                if not target_tab:
                    from urllib.parse import urlparse
                    curr_parsed = urlparse(current_url)
                    for tab in driver.tabs:
                        tab_parsed = urlparse(tab.url)
                        if curr_parsed.netloc == tab_parsed.netloc and curr_parsed.path == tab_parsed.path:
                            target_tab = tab
                            break
                            
                # Fallback to the active/newest tab if not matched
                if not target_tab and driver.tabs:
                    target_tab = driver.tabs[-1]

                if not target_tab:
                    raise RuntimeError("No matching tab found in SeleniumBase")

                logger.info(f"[TurnstileSolver] Switching sb to tab: {target_tab}")
                sb.switch_to_tab(target_tab)
                
                logger.info("[TurnstileSolver] Solving captcha via sb.solve_captcha()...")
                sb.solve_captcha()
                logger.info("[TurnstileSolver] sb.solve_captcha() finished.")
                return True

            await loop.run_in_executor(None, update_and_switch)
            
            if wait_selector:
                logger.info(f"[TurnstileSolver] Waiting for selector: {wait_selector}")
                try:
                    await page.wait_for_selector(wait_selector, timeout=wait_timeout * 1000)
                except Exception as e:
                    logger.warning(f"Timeout waiting for selector {wait_selector} after click: {e}")
                    return {
                        "success": True,
                        "warning": f"Selector {wait_selector} not found after click.",
                        "error": None,
                    }

            return {"success": True, "error": None}
        except Exception as e:
            logger.error(f"[TurnstileSolver] Error solving Turnstile via SeleniumBase: {e}")
            return {"success": False, "error": str(e)}
