"""Shared SeleniumBase (UC Mode) browser utilities for Coeus extraction workers.

Provides helpers for:
- SeleniumBase Undetected Chromedriver (UC) driver management
- Stealth Cloudflare Turnstile & CAPTCHA auto-handling
- Stealth cookie consent dismissal
- Session cookie save/load resolution
- Browser context recycling and process lifecycle management
- Centralised logger setup
- Xvfb (Virtual Framebuffer) integration for server environments
"""

import logging
import os
import sys
import time
import urllib.parse
from contextlib import contextmanager
from typing import Generator, List, Optional, Union

from seleniumbase import SB

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_VIEWPORT = (1280, 720)

# Selectors tried (in order) when dismissing cookie consent banners in Selenium
DEFAULT_COOKIE_SELECTORS = [
    "//button[contains(translate(text(), 'ACCEPT', 'accept'), 'accept all')]",
    "//button[contains(translate(text(), 'ACCEPT', 'accept'), 'accept cookies')]",
    "button.accept-btn",
    "button#accept-cookies",
]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logger(name: str) -> logging.Logger:
    """Return a logger with standard Coeus formatting."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Proxy Formatting Helper
# ---------------------------------------------------------------------------
def format_sb_proxy(proxy_url: Optional[str]) -> Optional[str]:
    """Format standard proxy URL strings into SeleniumBase format (user:pass@host:port)."""
    if not proxy_url:
        return None
    
    parsed = urllib.parse.urlparse(proxy_url)
    sb_proxy = ""
    if parsed.username and parsed.password:
        sb_proxy += f"{parsed.username}:{parsed.password}@"
    if parsed.hostname:
        sb_proxy += parsed.hostname
    if parsed.port:
        sb_proxy += f":{parsed.port}"
    return sb_proxy or None


# ---------------------------------------------------------------------------
# Stealth Driver Launcher & Turnstile Bypass
# ---------------------------------------------------------------------------
@contextmanager
def get_stealth_driver(
    headless: bool = False,
    proxy_url: Optional[str] = None,
    user_data_dir: Optional[str] = None,
    test: bool = True,
    use_xvfb: bool = False,
) -> Generator:
    """Context manager wrapping SeleniumBase UC (Undetected) Chrome.

    Yields:
        An active `sb` context object with access to UC stealth methods.
    """
    sb_proxy = format_sb_proxy(proxy_url)
    
    # If Xvfb is requested, we MUST run "headed" (headless=False) so it 
    # renders to the virtual display. Turnstile blocks pure headless easily.
    if use_xvfb and headless:
        logger.info("🖥️ Xvfb enabled: Overriding headless=True to headless=False for virtual display routing.")
        headless = False

    logger.info("Initializing SeleniumBase UC stealth browser...")
    with SB(
        uc=True,
        headless=headless,
        proxy=sb_proxy,
        user_data_dir=user_data_dir,
        test=test,
        xvfb=use_xvfb,
    ) as sb:
        # Standardize viewport
        try:
            sb.set_window_size(DEFAULT_VIEWPORT[0], DEFAULT_VIEWPORT[1])
        except Exception as e:
            logger.warning(f"Could not resize window: {e}")
            
        yield sb


def navigate_and_bypass_turnstile(
    sb: SB,
    url: str,
    reconnect_time: int = 3,
    target_element_check: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """Navigate to a URL using UC reconnect logic and automatically solve Turnstile CAPTCHAs.

    Args:
        sb: The active SeleniumBase instance.
        url: Target page URL.
        reconnect_time: Pause time during reconnection phases.
        target_element_check: Optional CSS/XPath selector to confirm verification.
        timeout: Max wait time for confirmation element.
    """
    logger.info(f"Navigating with UC reconnect: {url}")
    sb.uc_open_with_reconnect(url, reconnect_time=reconnect_time)

    logger.info("Handling potential Turnstile/CAPTCHA challenges...")
    try:
        sb.uc_gui_handle_captcha()
    except Exception as e:
        logger.warning(f"CAPTCHA auto-handler finished or skipped: {e}")

    # Explicit buffer to allow Cloudflare to issue redirect token
    sb.sleep(4)

    if target_element_check:
        try:
            sb.assert_element(target_element_check, timeout=timeout)
            logger.info(f"Target element '{target_element_check}' verified.")
            return True
        except Exception:
            logger.error(f"Failed to locate target element '{target_element_check}' after Turnstile attempt.")
            return False

    return True


# ---------------------------------------------------------------------------
# Cookie Consent Dismissal (Selenium native)
# ---------------------------------------------------------------------------
def dismiss_cookie_consent(
    sb: SB,
    selectors: Optional[List[str]] = None,
    timeout: int = 3,
) -> bool:
    """Attempt to locate and click cookie consent buttons using stealth clicks."""
    selectors = selectors or DEFAULT_COOKIE_SELECTORS
    for selector in selectors:
        try:
            if sb.is_element_visible(selector):
                logger.info("🍪 Cookie consent detected. Clicking accept...")
                sb.uc_click(selector)
                time.sleep(1)
                return True
        except Exception:
            continue
    logger.info("No cookie consent modal detected or already dismissed.")
    return False


# ---------------------------------------------------------------------------
# Session & Cookie State Storage
# ---------------------------------------------------------------------------
def resolve_storage_state(*search_paths: str) -> Optional[str]:
    """Return the first existing state path from search_paths, or None."""
    for path in search_paths:
        if path and os.path.exists(path):
            return path
    return None


def save_session_cookies(sb: SB, filepath: str) -> None:
    """Save browser session cookies to a file."""
    try:
        sb.save_cookies(name=filepath)
        logger.info(f"🔑 Saved session cookies to: {filepath}")
    except Exception as e:
        logger.error(f"Failed to save cookies to {filepath}: {e}")


def load_session_cookies(sb: SB, filepath: str) -> bool:
    """Load session cookies from a saved file if it exists."""
    if os.path.exists(filepath):
        try:
            sb.load_cookies(name=filepath)
            logger.info(f"🔑 Loaded session cookies from: {filepath}")
            return True
        except Exception as e:
            logger.error(f"Failed to load cookies from {filepath}: {e}")
    return False


# ---------------------------------------------------------------------------
# Worker Browser Lifecycle Manager
# ---------------------------------------------------------------------------
class UCBrowserManager:
    """Manages SeleniumBase UC driver instances for Coeus extraction tasks."""

    def __init__(
        self,
        headless: bool = False,
        proxy_url: Optional[str] = None,
        cookies_file: Optional[str] = None,
        use_xvfb: bool = False,
    ):
        self.headless = headless
        self.proxy_url = proxy_url
        self.cookies_file = cookies_file
        self.use_xvfb = use_xvfb
        self._sb_context = None
        self.sb = None

    def start(self) -> SB:
        """Start the stealth UC driver session."""
        if not self.sb:
            self._sb_context = get_stealth_driver(
                headless=self.headless,
                proxy_url=self.proxy_url,
                use_xvfb=self.use_xvfb,
            )
            self.sb = self._sb_context.__enter__()

            if self.cookies_file:
                load_session_cookies(self.sb, self.cookies_file)

        return self.sb

    def recycle(self) -> SB:
        """Close current instance and spawn a fresh browser to clear memory."""
        logger.info("Recycling UC browser driver instance...")
        self.close()
        return self.start()

    def close(self) -> None:
        """Close the active driver instance."""
        if self.sb and self.cookies_file:
            save_session_cookies(self.sb, self.cookies_file)

        if self._sb_context:
            try:
                self._sb_context.__exit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error during browser teardown: {e}")
            finally:
                self._sb_context = None
                self.sb = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()