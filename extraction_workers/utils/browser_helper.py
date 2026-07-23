"""Shared Playwright browser utilities for Coeus extraction workers.

Provides helpers for:
- CDP-based browser launch (anti-bot fingerprint evasion)
- Browser context creation with consistent defaults
- Cookie consent dismissal
- Storage state resolution
- Centralised logger setup
"""

import asyncio
import logging
import os
import socket
import subprocess
import sys
import tempfile
import urllib.parse
from typing import Optional

from seleniumbase import sb_cdp
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def _get_default_user_agent() -> str:
    """Get a default User-Agent matching the host platform to avoid anti-bot checks."""
    if sys.platform.startswith("linux"):
        return (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    elif sys.platform == "darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    else:  # win32 or others
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

DEFAULT_USER_AGENT = _get_default_user_agent()


DEFAULT_VIEWPORT = {"width": 1280, "height": 720}

# Default selectors tried (in order) when dismissing cookie consent banners.
DEFAULT_COOKIE_SELECTORS = [
    "button:has-text('Accept all cookies')",
    "button.accept-btn",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logger(name: str) -> logging.Logger:
    """Return a logger with the standard Coeus format.

    Safe to call multiple times – ``basicConfig`` is a no-op after the first
    call in a process.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# CDP Browser Launch
# ---------------------------------------------------------------------------
def _find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


async def launch_browser_cdp(
    playwright: Playwright,
    *,
    headless: bool = True,
    proxy_url: Optional[str] = None,
    extra_args: list[str] | None = None,
) -> tuple[Browser, object, Optional[str]]:
    """Launch Chrome via SeleniumBase Pure CDP mode and connect Playwright to it.

    Returns:
        A tuple of ``(browser, sb, user_data_dir)`` so callers
        can clean up when finished.
    """

    sb_proxy = None
    if proxy_url:
        parsed_proxy = urllib.parse.urlparse(proxy_url)
        sb_proxy = ""
        if parsed_proxy.username and parsed_proxy.password:
            sb_proxy += f"{parsed_proxy.username}:{parsed_proxy.password}@"
        if parsed_proxy.hostname:   
            sb_proxy += parsed_proxy.hostname
        if parsed_proxy.port:
            sb_proxy += f":{parsed_proxy.port}"

    import shutil
    chrome_in_path = (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("google-chrome-stable")
    )
    loop = asyncio.get_running_loop()
    
    if chrome_in_path:
        logger.info(f"Google Chrome detected in PATH: {chrome_in_path}. Letting SeleniumBase launch it automatically.")
        sb = await loop.run_in_executor(
            None,
            lambda: sb_cdp.Chrome(
                headless=headless,
                proxy=sb_proxy
            )
        )
    else:
        chrome_path = playwright.chromium.executable_path
        logger.info(f"Google Chrome not found in PATH. Falling back to Playwright Chromium: {chrome_path}")
        sb = await loop.run_in_executor(
            None,
            lambda: sb_cdp.Chrome(
                headless=headless,
                proxy=sb_proxy,
                browser_executable_path=chrome_path
            )
        )
    endpoint_url = sb.get_endpoint_url()

    logger.info(f"Connecting Playwright over CDP to: {endpoint_url}")
    browser = await playwright.chromium.connect_over_cdp(endpoint_url)
    return browser, sb, None


async def close_browser_cdp(
    browser: Browser,
    chrome_proc: object,
) -> None:
    """Gracefully close a CDP-launched browser and its Chrome subprocess/SeleniumBase."""
    try:
        await browser.close()
    except Exception as e:
        logger.warning(f"Error closing browser: {e}")
    if chrome_proc:
        if hasattr(chrome_proc, "quit"):
            try:
                logger.info("Quitting SeleniumBase Chrome browser...")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, chrome_proc.quit)
            except Exception as e:
                logger.warning(f"Error terminating SeleniumBase: {e}")
        elif isinstance(chrome_proc, subprocess.Popen):
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Error terminating Chrome process: {e}")


# ---------------------------------------------------------------------------
# Browser Context Creation
# ---------------------------------------------------------------------------
async def create_browser_context(
    browser: Browser,
    *,
    ignore_https_errors: bool = False,
    storage_state: Optional[str | dict] = None,
    proxy_url: Optional[str] = None,
    user_agent: str = DEFAULT_USER_AGENT,
    viewport: dict | None = None,
    anti_webdriver: bool = True,
) -> tuple[BrowserContext, Page]:
    """Create a Playwright browser context with sensible defaults.

    Returns:
        A tuple of ``(context, page)``; the page is already created.
    """
    context_kwargs: dict = {
        "viewport": viewport or DEFAULT_VIEWPORT,
        "user_agent": user_agent,
        "ignore_https_errors": ignore_https_errors,
    }

    if storage_state:
        if isinstance(storage_state, dict):
            context_kwargs["storage_state"] = storage_state
            logger.info("🔑 Loading browser session state from database dict.")
        elif isinstance(storage_state, str) and os.path.exists(storage_state):
            context_kwargs["storage_state"] = storage_state
            logger.info(f"🔑 Loading browser session state from: {storage_state}")

    if proxy_url:
        parsed_proxy = urllib.parse.urlparse(proxy_url)
        server_url = f"{parsed_proxy.scheme}://{parsed_proxy.hostname}"
        if parsed_proxy.port:
            server_url += f":{parsed_proxy.port}"

        proxy_config: dict = {"server": server_url}
        if parsed_proxy.username:
            proxy_config["username"] = parsed_proxy.username
        if parsed_proxy.password:
            proxy_config["password"] = parsed_proxy.password

        context_kwargs["proxy"] = proxy_config

        # Log with masked password
        masked = server_url
        if parsed_proxy.username:
            masked = (
                f"{parsed_proxy.scheme}://{parsed_proxy.username}:****@"
                f"{parsed_proxy.hostname}"
            )
            if parsed_proxy.port:
                masked += f":{parsed_proxy.port}"
        logger.info(f"Using proxy for browser context: {masked}")

    context = await browser.new_context(**context_kwargs)

    if anti_webdriver:
        await context.add_init_script(
            "Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined})"
        )

    page = await context.new_page()
    return context, page


# ---------------------------------------------------------------------------
# Cookie Consent Dismissal
# ---------------------------------------------------------------------------
async def dismiss_cookie_consent(
    page: Page,
    selectors: list[str] | None = None,
    timeout: int = 5000,
) -> bool:
    """Attempt to dismiss a cookie consent banner.

    Tries each selector in order. Returns ``True`` if a banner was found and
    dismissed, ``False`` otherwise.
    """
    selectors = selectors or DEFAULT_COOKIE_SELECTORS
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            await btn.wait_for(state="visible", timeout=timeout)
            logger.info("🍪 Cookie consent detected. Accepting...")
            await btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            return True
        except Exception:
            continue
    logger.info("No cookie consent modal detected or already dismissed.")
    return False


# ---------------------------------------------------------------------------
# Storage State Resolution
# ---------------------------------------------------------------------------
def resolve_storage_state(*search_paths: str) -> Optional[str]:
    """Return the first existing path from *search_paths*, or ``None``.

    Typical usage::

        state = resolve_storage_state(
            os.path.join(output_dir, "state.json"),
            os.path.join(base_dir, "state.json"),
            "data/state.json",
        )
    """
    for path in search_paths:
        if path and os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------------------
# Browser lifecycle and recycling manager
# ---------------------------------------------------------------------------
class BrowserManager:
    """Manages the lifetime and recycling of a Playwright browser instance.

    Automatically handles the deletion of Chromium temporary profile/user-data
    directories upon shutdown or recycling, preventing disk leaks.
    """

    def __init__(
        self,
        playwright: Playwright,
        *,
        headless: bool = True,
        ignore_https_errors: bool = False,
        storage_state: Optional[str | dict] = None,
        proxy_url: Optional[str] = None,
        user_agent: str = DEFAULT_USER_AGENT,
        viewport: Optional[dict] = None,
        anti_webdriver: bool = True,
    ):
        self.playwright = playwright
        self.headless = headless
        self.ignore_https_errors = ignore_https_errors
        self.storage_state = storage_state
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self.viewport = viewport or DEFAULT_VIEWPORT
        self.anti_webdriver = anti_webdriver

        self.browser: Optional[Browser] = None
        self.chrome_proc: Optional[subprocess.Popen] = None
        self.user_data_dir: Optional[str] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def start(self) -> Page:
        """Launch browser and set up context/page if not already running."""
        if not self.browser:
            self.browser, self.chrome_proc, self.user_data_dir = await launch_browser_cdp(
                self.playwright,
                headless=self.headless,
                proxy_url=self.proxy_url,
            )
            # Find default context(s) before we create our custom context
            default_contexts = list(self.browser.contexts)

            self.context, self.page = await create_browser_context(
                self.browser,
                ignore_https_errors=self.ignore_https_errors,
                storage_state=self.storage_state,
                proxy_url=self.proxy_url,
                user_agent=self.user_agent,
                viewport=self.viewport,
                anti_webdriver=self.anti_webdriver,
            )

            # Note: Do not close default contexts when connected over CDP,
            # as it will close the entire browser session.
            pass
        return self.page

    async def recycle(self) -> Page:
        """Gracefully close current browser context/process, clean up, and start fresh."""
        logger.info("Recycling browser context to clear memory...")
        await self.close()
        return await self.start()

    async def close(self) -> None:
        """Shut down the browser and cleanly remove the temporary user profile dir."""
        if self.browser:
            try:
                await close_browser_cdp(self.browser, self.chrome_proc)
            except Exception as e:
                logger.warning(f"Error during close_browser_cdp in BrowserManager: {e}")
            finally:
                self.browser = None
                self.chrome_proc = None
                self.context = None
                self.page = None

        if self.user_data_dir and os.path.exists(self.user_data_dir):
            try:
                import shutil
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
                logger.info(f"Cleaned up temporary user data directory: {self.user_data_dir}")
            except Exception as e:
                logger.warning(f"Failed to remove user data dir {self.user_data_dir}: {e}")
            finally:
                self.user_data_dir = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

