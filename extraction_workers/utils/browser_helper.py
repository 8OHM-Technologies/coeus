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

from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
    extra_args: list[str] | None = None,
) -> tuple[Browser, subprocess.Popen, str]:
    """Launch Chrome via subprocess with CDP and connect Playwright to it.

    This approach avoids the ``navigator.webdriver`` flag that standard
    ``playwright.chromium.launch()`` sets, which many anti-bot systems
    detect.

    Returns:
        A tuple of ``(browser, chrome_process, user_data_dir)`` so callers
        can clean up when finished.
    """
    cdp_port = _find_free_port()
    user_data_dir = tempfile.mkdtemp()

    chrome_args = [
        playwright.chromium.executable_path,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-gcm",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-component-update",
        "--disable-features=WebRtcHideLocalIpsWithMdns,WebRTC",
        "--disable-peer-connection-encryption",
        "--window-size=1280,720",
    ]

    if extra_args:
        chrome_args.extend(extra_args)

    if headless:
        chrome_args.append("--headless=new")

    logger.info(f"Starting Chrome with CDP on port {cdp_port}...")
    chrome_proc = subprocess.Popen(chrome_args)

    # Give Chrome a moment to bind the debug port
    await asyncio.sleep(3)

    browser = await playwright.chromium.connect_over_cdp(
        f"http://127.0.0.1:{cdp_port}"
    )
    return browser, chrome_proc, user_data_dir


async def close_browser_cdp(
    browser: Browser,
    chrome_proc: subprocess.Popen,
) -> None:
    """Gracefully close a CDP-launched browser and its Chrome subprocess."""
    try:
        await browser.close()
    except Exception as e:
        logger.warning(f"Error closing browser: {e}")
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
    storage_state: Optional[str] = None,
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

    if storage_state and os.path.exists(storage_state):
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
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
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
