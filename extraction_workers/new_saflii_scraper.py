import argparse
import asyncio
import io
import logging
import os
import queue
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, date as dt_date, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None

from bs4 import BeautifulSoup
from seleniumbase import SB


try:
    from .base_scraper import BaseScraper, setup_logger
    from . import db_storage
    from .utils.browser_helper import format_sb_proxy
except ImportError:
    from base_scraper import BaseScraper, setup_logger
    import db_storage
    from utils.browser_helper import format_sb_proxy

logger = setup_logger("saflii_scraper")
gui_lock = threading.Lock()


# ---------------------------------------------------------------------------
# SAFLII Databases Index & Court Discovery Constants
# ---------------------------------------------------------------------------

DATABASES_INDEX_URL = "https://www.saflii.org/content/databases.html"

# Valid SAFLII URL path prefixes for South African datasets
_VALID_ZA_PATH_PREFIXES = ("/za/cases/", "/za/gaz/", "/za/journals/", "/za/other/")


def extract_court_code_from_url(url: str) -> Optional[str]:
    """Extract the SAFLII court/dataset code from a URL path.

    Recognises patterns: ``/za/cases/{CODE}/``, ``/za/gaz/{CODE}/``,
    ``/za/journals/{CODE}/``, ``/za/other/{CODE}/``
    """
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "za" and i + 2 < len(parts):
            category = parts[i + 1]
            if category in ("cases", "gaz", "journals", "other"):
                return parts[i + 2]
    return None


class BlockedException(Exception):
    """Raised when a Cloudflare/Turnstile verification wall cannot be breached."""
    pass


# ---------------------------------------------------------------------------
# Pure Utility Helpers
# ---------------------------------------------------------------------------

def format_proxy_for_sb(proxy_url: Optional[str]) -> Optional[str]:
    """Format standard proxy URL strings into SeleniumBase format (user:pass@host:port)."""
    return format_sb_proxy(proxy_url)


def check_page_state(page_title: str = "", h1_title: str = "", body_text: str = "") -> str:
    """Determine the logical state of the page to detect blocks or dead links."""
    t_lower = (page_title or "").lower().strip()
    h_lower = (h1_title or "").lower().strip()
    b_lower = (body_text or "").lower().strip()

    if (
        "just a moment" in t_lower
        or "cloudflare" in t_lower
        or "security verification" in t_lower
        or "verify you are human" in b_lower
        or "turnstile" in b_lower
        or "403 forbidden" in t_lower
        or "forbidden" in t_lower
        or "403 forbidden" in h_lower
        or "forbidden" in h_lower
        or "you don't have permission to access this resource" in b_lower
    ):
        return "BLOCKED"

    # Only match EXPLICIT 404 error page titles — not titles that merely contain
    # the string "404" (e.g. a real case titled "ZALCJHB 404 [2025]" is NOT a 404 page).
    _NOT_FOUND_TITLES = {
        "not found",
        "page not found",
        "404 not found",
        "404 - not found",
        "404 error",
        "error 404",
        "http 404",
        "404",
    }
    _NOT_FOUND_H1 = {
        "not found",
        "page not found",
        "404 not found",
        "404 - not found",
        "404 error",
        "404",
    }
    if (
        t_lower in _NOT_FOUND_TITLES
        or h_lower in _NOT_FOUND_H1
        or "404 not found" in t_lower
        or "404 error" in t_lower
        or "page not found" in t_lower
    ):
        return "NOT_FOUND"

    return "OK"


def get_sb_page_signals(sb: SB) -> Tuple[str, str, str]:
    """Extract page title, first h1, and body text using SeleniumBase."""
    title = sb.get_page_title() or ""
    try:
        h1 = sb.get_text("h1") if sb.is_element_present("h1") else ""
    except Exception:
        h1 = ""
    try:
        body = sb.get_text("body") if sb.is_element_present("body") else ""
    except Exception:
        body = ""
    return title, h1, body


def parse_case_url(case_url: str, default_court: str = "SAFLII") -> Tuple[str, str, str]:
    """Parse a valid SAFLII asset path to map structural parameters."""
    parsed = urllib.parse.urlparse(case_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3:
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 4:
                return parts[i - 1], parts[i], os.path.splitext(parts[i + 1])[0]
    return default_court, "unknown", "unknown"


def extract_case_number_from_text(text: str) -> Optional[str]:
    """Extract standard SAFLII case numbers or formal citations from strings."""
    if not text:
        return None

    match = re.search(r'Case\s+(?:No|Number)\s*:\s*([A-Za-z0-9/\s-]+)', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    match = re.search(r'\((?:\w+\s+)?(\w+/\d+)\)', text)
    if match:
        return match.group(1).strip()

    match = re.search(r'\[\d{4}\]\s+\w+\s+\d+', text)
    if match:
        return match.group(0).strip()

    match = re.search(r'\b\w+/\d+\b', text)
    if match:
        return match.group(0).strip()

    return None


def parse_saflii_case(raw_html: str, url: str) -> Optional[Dict[str, Any]]:
    """
    Parses a SAFLII case HTML page string into structured metadata and body text using BeautifulSoup (lxml).
    Returns None if the page is a 404 or missing document.
    Raises ValueError if stuck on a Cloudflare challenge screen.
    """
    soup = BeautifulSoup(raw_html, "lxml")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    page_text = soup.get_text()

    if "404" in page_title or "page not found" in page_text.lower():
        return None

    if "just a moment" in page_title.lower() or "enable javascript" in page_text.lower():
        raise ValueError("Cloudflare challenge page detected; request was intercepted.")

    heading_el = soup.find(["h1", "h2", "h3"])
    case_name = heading_el.get_text(strip=True) if heading_el else page_title

    citation = None
    citation_match = re.search(r"\[\d{4}\]\s+[A-Z]+\s+\d+", page_text)
    if citation_match:
        citation = citation_match.group(0)

    case_number = extract_case_number_from_text(page_text)

    for elem in soup(["script", "style", "nav", "header", "footer"]):
        elem.decompose()

    content_container = (
        soup.find("div", id="center")
        or soup.find("div", class_="judgment")
        or soup.find("article")
        or soup.find("body")
    )
    clean_text = (
        content_container.get_text(separator="\n", strip=True)
        if content_container
        else ""
    )

    return {
        "url": url,
        "title": case_name,
        "citation": citation,
        "case_number": case_number,
        "center_content": str(content_container) if content_container else "",
        "full_text": clean_text,
    }


async def wait_for_page_load(page, url_type: str = "case") -> str:
    """Asynchronously checks page load status for Playwright/browser instances."""
    title = await page.title() or ""
    h1_loc = page.locator("h1")
    h1_text = ""
    if await h1_loc.count() > 0:
        h1_text = await h1_loc.first.inner_text() or ""
    body_loc = page.locator("body")
    body_text = ""
    if await body_loc.count() > 0:
        body_text = await body_loc.inner_text() or ""
    return check_page_state(title, h1_text, body_text)


# ---------------------------------------------------------------------------
# Core Framework Implementation (SeleniumBase UC Mode + Multithreaded Worker)
# ---------------------------------------------------------------------------

class SafliiScraper(BaseScraper):
    """
    SAFLII Scraper subclassing BaseScraper.
    Discovers all South African courts/datasets from the SAFLII databases index
    page, uses SeleniumBase UC mode with lxml parsing, multithreaded detailing
    workers, database record persistence, progress tracking, and proxy support.
    """

    def __init__(
        self,
        pipeline_name: str,
        courts: Optional[List[str]] = None,
        year: Optional[int] = None,
        headless: bool = False,
        use_xvfb: bool = True,
        skip_stages: Optional[str] = None,
    ):
        super().__init__(pipeline_name, skip_stages=skip_stages)
        self.courts_filter: Optional[List[str]] = courts
        self.year: Optional[int] = year
        self.headless: bool = headless
        self.use_xvfb: bool = use_xvfb

        self.index_url: str = DATABASES_INDEX_URL
        self.cooldown_seconds: float = 1.5
        self.take_debug_screenshots: bool = False
        self.screenshots_dir: str = ""

        # Multi-court state
        self.court_base_urls: Dict[str, Tuple[str, str]] = {}  # code -> (display_name, base_url)
        self.case_urls: List[str] = []
        self.url_to_case_number: Dict[str, str] = {}
        self.db_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Hydrate configuration, resolve target IDs, DB connections, and progress state."""
        await super().initialize()

        extraction_params = self.config.get("extraction_params", {})

        # Allow index URL override from config; fall back to DATABASES_INDEX_URL
        self.index_url = self.config.get("start_url") or DATABASES_INDEX_URL

        # Court filter: CLI argument takes precedence, then config extraction_params
        if not self.courts_filter:
            cfg_courts = extraction_params.get("courts")
            if cfg_courts:
                if isinstance(cfg_courts, str):
                    self.courts_filter = [c.strip() for c in cfg_courts.split(",") if c.strip()]
                elif isinstance(cfg_courts, list):
                    self.courts_filter = cfg_courts

        self.cooldown_seconds = float(extraction_params.get("cooldown_seconds", 1.5))
        self.take_debug_screenshots = self.config.get("take_debug_screenshots", False)

        if self.output_dir:
            self.screenshots_dir = os.path.join(os.path.dirname(self.output_dir), "screenshots")
            os.makedirs(self.screenshots_dir, exist_ok=True)

        courts_desc = ", ".join(self.courts_filter) if self.courts_filter else "ALL (auto-discover)"
        year_desc = str(self.year) if self.year else "auto-discover"

        logger.info("==================================================")
        logger.info(f"🚀 COEUS SAFLII WORKER (SeleniumBase UC Mode) ({self.pipeline_name})")
        logger.info(f"Target Courts: {courts_desc} | Year: {year_desc}")
        logger.info("==================================================")

    async def authenticate(self, headless: bool = False) -> None:
        """SAFLII bypasses login constraints via inline Cloudflare tokens. No-op implementation."""
        pass

    def _navigate_and_handle_turnstile(
        self,
        sb: SB,
        url: str,
        attempt_prefix: str,
        timeout: float = 15.0,
    ) -> str:
        """Open a URL with single-window navigation, wait for URL commit, and attempt Turnstile solving."""
        logger.info(f"Navigating to: {url} [{attempt_prefix}]")

        # 1. Clean up extra window handles if any accumulated to ensure window stability
        try:
            handles = sb.driver.window_handles
            if len(handles) > 1:
                main_handle = handles[0]
                for h in handles[1:]:
                    try:
                        sb.driver.switch_to.window(h)
                        sb.driver.close()
                    except Exception:
                        pass
                sb.driver.switch_to.window(main_handle)
            elif len(handles) == 1:
                sb.driver.switch_to.window(handles[0])
        except Exception:
            pass

        # Parse target filename cleanly (ignoring trailing slashes)
        path_parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
        target_filename = path_parts[-1] if path_parts else ""

        # 2. Open URL via sb.open() — in SeleniumBase UC mode this already activates CDP stealth
        # mode automatically ("open() in UC Mode now always activates CDP Mode"). Using
        # uc_open_with_reconnect as primary was crashing sessions by disconnecting CDP for too long.
        try:
            sb.open(url)
        except Exception as open_err:
            logger.warning(f"sb.open failed on {url} ({open_err}); attempting uc_open_with_reconnect fallback...")
            try:
                sb.uc_open_with_reconnect(url, reconnect_time=1)
            except Exception:
                pass

        # 3. Wait for browser URL transition to commit to target resource endpoint
        navigated = False
        max_attempts = max(1, int(timeout / 0.5))
        for _ in range(max_attempts):
            try:
                curr_url = sb.get_current_url()
                if (target_filename and target_filename in curr_url) or curr_url == url:
                    navigated = True
                    break
            except Exception:
                pass
            sb.sleep(0.5)

        # 4. If standard open didn't commit to target URL within timeout, attempt UC reconnect fallback
        if not navigated:
            logger.warning(
                f"URL transition slow/stuck for '{target_filename or url}' (current: '{sb.get_current_url()}'). "
                f"Executing recovery reconnect..."
            )
            try:
                sb.uc_open_with_reconnect(url, reconnect_time=3)
            except Exception:
                pass

            # Wait up to 10 seconds post-reconnect for page load to finish
            for _ in range(20):
                try:
                    curr_url = sb.get_current_url()
                    if (target_filename and target_filename in curr_url) or curr_url == url:
                        navigated = True
                        break
                except Exception:
                    pass
                sb.sleep(0.5)

            if not navigated:
                return "NAVIGATION_FAILED"

        # 5. Evaluate current page state
        title, h1, body = get_sb_page_signals(sb)
        state = check_page_state(title, h1, body)

        # 6. If blocked by Turnstile, actively attempt to solve the challenge before returning
        if state == "BLOCKED":
            logger.info(f"[{attempt_prefix}] Turnstile/Cloudflare wall detected — attempting challenge solve...")

            if self.take_debug_screenshots and self.screenshots_dir:
                try:
                    sb.save_screenshot(os.path.join(self.screenshots_dir, f"{attempt_prefix}_blocked_pre.png"))
                except Exception:
                    pass

            # Attempt 1: UC GUI captcha click (guarded by gui_lock to prevent multi-threaded X11 screen/mouse race conditions)
            solved = False
            try:
                with gui_lock:
                    sb.uc_gui_click_captcha()
                sb.sleep(3)
                title, h1, body = get_sb_page_signals(sb)
                state = check_page_state(title, h1, body)
                if state not in ("BLOCKED",):
                    logger.info(f"[{attempt_prefix}] Turnstile solved via uc_gui_click_captcha (state: {state})")
                    solved = True
            except Exception as captcha_err:
                logger.warning(f"[{attempt_prefix}] uc_gui_click_captcha failed: {captcha_err}")

            # Attempt 2: UC GUI handle captcha (broader handler — iframe-aware, guarded by gui_lock)
            if not solved:
                try:
                    with gui_lock:
                        sb.uc_gui_handle_captcha()
                    sb.sleep(3)
                    title, h1, body = get_sb_page_signals(sb)
                    state = check_page_state(title, h1, body)
                    if state not in ("BLOCKED",):
                        logger.info(f"[{attempt_prefix}] Turnstile solved via uc_gui_handle_captcha (state: {state})")
                        solved = True
                except Exception as captcha_err:
                    logger.warning(f"[{attempt_prefix}] uc_gui_handle_captcha failed: {captcha_err}")

            # Attempt 3: UC reconnect + re-open with short reconnect window as last resort
            # (reconnect_time=1 avoids crashing the Chrome session in multithreaded contexts)
            if not solved:
                try:
                    logger.info(f"[{attempt_prefix}] Attempting UC reconnect fallback to breach Turnstile...")
                    sb.uc_open_with_reconnect(url, reconnect_time=1)
                    sb.sleep(3)
                    title, h1, body = get_sb_page_signals(sb)
                    state = check_page_state(title, h1, body)
                    if state not in ("BLOCKED",):
                        logger.info(f"[{attempt_prefix}] Turnstile breached via uc_open_with_reconnect (state: {state})")
                        solved = True
                except Exception as reconnect_err:
                    logger.debug(f"[{attempt_prefix}] uc_open_with_reconnect fallback failed: {reconnect_err}")

            if not solved:
                logger.warning(f"[{attempt_prefix}] All Turnstile solve attempts failed. Returning BLOCKED.")

            if self.take_debug_screenshots and self.screenshots_dir:
                try:
                    sb.save_screenshot(os.path.join(self.screenshots_dir, f"{attempt_prefix}_blocked_post.png"))
                except Exception:
                    pass

        else:
            if self.take_debug_screenshots and self.screenshots_dir:
                try:
                    sb.save_screenshot(os.path.join(self.screenshots_dir, f"{attempt_prefix}.png"))
                except Exception:
                    pass

        return state

    # ------------------------------------------------------------------
    # Court & Year Discovery
    # ------------------------------------------------------------------

    def _discover_court_urls_from_page(self, sb: SB) -> Dict[str, Tuple[str, str]]:
        """Navigate to SAFLII databases index page and extract all South African court/dataset URLs.

        Returns dict mapping ``court_code`` → ``(display_name, base_url)``.
        """
        courts: Dict[str, Tuple[str, str]] = {}

        for attempt in range(1, 4):
            state = self._navigate_and_handle_turnstile(
                sb, self.index_url, f"index_page_attempt_{attempt}"
            )
            if state == "BLOCKED":
                logger.warning(f"Index page blocked on attempt {attempt}/3.")
                if attempt < 3:
                    sb.sleep(attempt * 4)
                    continue
                else:
                    raise BlockedException(
                        "Failed to access SAFLII databases index page after 3 attempts."
                    )

            soup = BeautifulSoup(sb.get_page_source(), "lxml")
            for a_tag in soup.find_all("a"):
                link_text = a_tag.get_text(strip=True)
                href = a_tag.get("href")

                if not link_text or not href or not link_text.startswith("South Africa:"):
                    continue

                abs_url = urllib.parse.urljoin(self.index_url, href)
                parsed = urllib.parse.urlparse(abs_url)

                # Accept only URLs matching valid SA path prefixes
                if not any(parsed.path.startswith(prefix) for prefix in _VALID_ZA_PATH_PREFIXES):
                    continue

                court_code = extract_court_code_from_url(abs_url)
                if court_code:
                    base_url = abs_url.rstrip("/") + "/"
                    courts[court_code] = (link_text, base_url)

            if courts:
                logger.info(f"Discovered {len(courts)} South African court/dataset URLs from index page.")
                break
            else:
                logger.warning(
                    f"No court URLs found on attempt {attempt}/3. Page may not have loaded correctly."
                )
                if attempt < 3:
                    sb.sleep(attempt * 4)

        return courts

    def _discover_year_links_from_page(
        self, sb: SB, court_code: str, base_url: str
    ) -> List[Tuple[int, str]]:
        """Navigate to a court's base page and discover available year directory links.

        Year links typically appear as ``<a href="YYYY/">`` inside ``<h3>`` elements.

        Returns list of ``(year, year_url)`` tuples sorted by year.
        """
        year_links: List[Tuple[int, str]] = []

        for attempt in range(1, 4):
            state = self._navigate_and_handle_turnstile(
                sb, base_url, f"court_{court_code}_years_attempt_{attempt}"
            )
            if state == "BLOCKED":
                logger.warning(f"Court page {court_code} blocked on attempt {attempt}/3.")
                if attempt < 3:
                    sb.sleep(attempt * 4)
                    continue
                else:
                    logger.error(
                        f"Failed to access court page {court_code} after 3 attempts. Skipping."
                    )
                    return []
            if state == "NOT_FOUND":
                logger.warning(f"Court page {court_code} returned NOT_FOUND. Skipping.")
                return []

            soup = BeautifulSoup(sb.get_page_source(), "lxml")
            seen_years: set = set()

            # Primary: look for year links inside <h3> elements
            for h3 in soup.find_all("h3"):
                for a_tag in h3.find_all("a"):
                    href = a_tag.get("href", "").strip()
                    year_match = re.match(r"^(\d{4})/?$", href)
                    if year_match:
                        year = int(year_match.group(1))
                        if year not in seen_years:
                            year_url = urllib.parse.urljoin(base_url, f"{year}/")
                            year_links.append((year, year_url))
                            seen_years.add(year)

            # Fallback: scan all links on the page for bare year hrefs
            if not year_links:
                for a_tag in soup.find_all("a"):
                    href = a_tag.get("href", "").strip()
                    year_match = re.match(r"^(\d{4})/?$", href)
                    if year_match:
                        year = int(year_match.group(1))
                        if year not in seen_years:
                            year_url = urllib.parse.urljoin(base_url, f"{year}/")
                            year_links.append((year, year_url))
                            seen_years.add(year)

            if year_links:
                year_links.sort(key=lambda x: x[0])
                logger.info(
                    f"Court {court_code}: Discovered {len(year_links)} year directories: "
                    f"{[y for y, _ in year_links]}"
                )
                break
            else:
                logger.warning(f"Court {court_code}: No year links found on attempt {attempt}/3.")
                if attempt < 3:
                    sb.sleep(attempt * 4)

        return year_links

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _indexing_sync(
        self, loop: asyncio.AbstractEventLoop, db_record_type: str
    ) -> Tuple[List[str], Dict[str, str]]:
        """Synchronous indexing task running inside a dedicated SB UC context thread.

        Discovers courts from the databases index page, auto-discovers available
        years per court, and harvests document URLs.  Indexed URLs are batch-saved
        to the database per-court for restart resilience.
        """
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        raw_case_urls: List[str] = []
        url_to_case_map: Dict[str, str] = {}
        indexed_this_run: set = set()  # Local dedup set for within-run indexing

        indexing_profile = "/tmp/saflii_indexing_profile"

        # 1. Discover all court URLs from the databases index page using a short-lived session
        shutil.rmtree(indexing_profile, ignore_errors=True)
        os.makedirs(indexing_profile, exist_ok=True)
        logger.info(f"Discovering court URLs from: {self.index_url}")
        try:
            with SB(
                uc=True,
                headless=self.headless,
                proxy=sb_proxy,
                multi_proxy=self.use_proxy,
                test=True,
                xvfb=self.use_xvfb,
                user_data_dir=indexing_profile,
                chromium_arg="--no-sandbox,--disable-dev-shm-usage"
            ) as sb:
                sb.set_window_size(1280, 720)
                sb.driver.set_page_load_timeout(30)
                sb.driver.set_script_timeout(30)

                # Verify and log outbound public IP
                self.log_outbound_ip(sb, label="SAFLII Indexing Stage (Discovery)")

                self.court_base_urls = self._discover_court_urls_from_page(sb)
        except Exception as discovery_err:
            logger.error(f"Failed to discover court URLs during indexing startup: {discovery_err}")
            return [], {}

        if not self.court_base_urls:
            logger.error("No court URLs discovered from index page. Aborting indexing.")
            return [], {}

        # 2. Apply court filter if specified
        if self.courts_filter:
            filter_set = {c.upper() for c in self.courts_filter}
            filtered = {
                k: v for k, v in self.court_base_urls.items() if k.upper() in filter_set
            }
            skipped = set(self.court_base_urls.keys()) - set(filtered.keys())
            if skipped:
                logger.info(
                    f"Court filter active. Skipping {len(skipped)} courts: {sorted(skipped)}"
                )
            self.court_base_urls = filtered

        total_courts = len(self.court_base_urls)
        logger.info(f"Processing {total_courts} courts: {sorted(self.court_base_urls.keys())}")

        # 3. For each court, discover years and harvest document URLs in a recycled browser session
        for court_idx, (court_code, (display_name, base_url)) in enumerate(
            sorted(self.court_base_urls.items()), 1
        ):
            logger.info(f"\n{'=' * 60}")
            logger.info(
                f"[Court {court_idx}/{total_courts}] {display_name} ({court_code})"
            )
            logger.info(f"Base URL: {base_url}")
            logger.info("=" * 60)

            # Clean user data directory before launching browser session for this court
            shutil.rmtree(indexing_profile, ignore_errors=True)
            os.makedirs(indexing_profile, exist_ok=True)

            try:
                with SB(
                    uc=True,
                    headless=self.headless,
                    proxy=sb_proxy,
                    multi_proxy=self.use_proxy,
                    test=True,
                    xvfb=self.use_xvfb,
                    user_data_dir=indexing_profile,
                    chromium_arg="--no-sandbox,--disable-dev-shm-usage"
                ) as sb:
                    sb.set_window_size(1280, 720)
                    sb.driver.set_page_load_timeout(30)
                    sb.driver.set_script_timeout(30)

                    # Verify and log outbound public IP
                    self.log_outbound_ip(sb, label=f"SAFLII Indexing Stage - {court_code}")

                    # 3a. Discover available years
                    if self.year:
                        year_links = [(self.year, f"{base_url}{self.year}/")]
                    else:
                        year_links = self._discover_year_links_from_page(sb, court_code, base_url)

                    if not year_links:
                        logger.warning(f"Court {court_code}: No year directories found. Skipping.")
                        continue

                    court_new_urls: List[str] = []

                    # 3b. Process each year directory
                    for year, year_url in year_links:
                        logger.info(
                            f"Processing structural year context directory: {court_code}/{year} -> {year_url}"
                        )
                        found_for_year = 0
                        for attempt in range(1, 4):
                            try:
                                state = self._navigate_and_handle_turnstile(
                                    sb, year_url,
                                    f"court_{court_code}_year_{year}_attempt_{attempt}"
                                )
                                if state == "BLOCKED":
                                    logger.warning(
                                        f"Year directory {court_code}/{year} blocked on attempt {attempt}/3."
                                    )
                                    if attempt < 3:
                                        sb.sleep(attempt * 4)
                                        continue
                                    else:
                                        break

                                if state == "NOT_FOUND":
                                    logger.info(
                                        f"Court {court_code} year {year}: Directory not found."
                                    )
                                    break

                                soup = BeautifulSoup(sb.get_page_source(), "lxml")
                                for a_tag in soup.find_all("a"):
                                    href = a_tag.get("href")
                                    if not href:
                                        continue
                                    abs_url = urllib.parse.urljoin(year_url, href)
                                    parsed_url = urllib.parse.urlparse(abs_url)
                                    parts = [p for p in parsed_url.path.split("/") if p]

                                    if len(parts) >= 2:
                                        parent_dir, filename = parts[-2], parts[-1]
                                        if parent_dir.isdigit() and len(parent_dir) == 4 and (
                                            filename.endswith(".html") or filename.endswith(".pdf")
                                        ):
                                            if not ("toc-" in filename or filename == "index.html"):
                                                if abs_url not in self.existing_urls and abs_url not in indexed_this_run:
                                                    case_no = extract_case_number_from_text(a_tag.get_text(strip=True))
                                                    if case_no:
                                                        url_to_case_map[abs_url] = case_no
                                                    court_new_urls.append(abs_url)
                                                    found_for_year += 1
                                logger.info(
                                    f"Court {court_code} year {year}: "
                                    f"Harvested {found_for_year} new candidate URLs."
                                )
                                break
                            except Exception as err:
                                logger.warning(
                                    f"Error isolating index structures for {court_code}/{year} "
                                    f"[Attempt {attempt}]: {err}"
                                )
                                if attempt < 3:
                                    sb.sleep(attempt * 4)

                        sb.sleep(self.cooldown_seconds + random.uniform(0.2, 0.6))

                    # Batch-save this court's newly discovered URLs to DB for restart resilience
                    if court_new_urls:
                        batch_records = []
                        for curl in court_new_urls:
                            cc, cy, cid = parse_case_url(curl, default_court=court_code)
                            rec = {
                                "detail_url": curl,
                                "court": cc,
                                "year": cy,
                                "case_id": cid,
                            }
                            cn = url_to_case_map.get(curl)
                            if cn:
                                rec["case_number"] = cn
                            batch_records.append(rec)

                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                db_storage.upsert_scraped_records_batch(
                                    self.conn,
                                    self.target_id,
                                    db_record_type,
                                    batch_records,
                                    url_key="detail_url",
                                    status="indexed",
                                ),
                                loop,
                            )
                            saved = future.result(timeout=60)
                            logger.info(
                                f"Court {court_code}: Saved {saved} indexed records to database."
                            )
                        except Exception as save_err:
                            logger.error(
                                f"Court {court_code}: Failed to batch-save indexed records: {save_err}"
                            )

                        # Track newly indexed URLs locally for within-run dedup
                        # (don't add to self.existing_urls — that would cause detailing to skip them)
                        indexed_this_run.update(court_new_urls)
                        raw_case_urls.extend(court_new_urls)
            except Exception as court_err:
                logger.error(f"Court {court_code}: Browser session failed or crashed: {court_err}. Recycling browser session...")

        return sorted(list(set(raw_case_urls))), url_to_case_map

    async def indexing(self) -> None:
        """Sub-process A: Harvest case entry indexes across all discovered courts."""
        logger.info("[Stage 1A start] Starting indexing stage via SeleniumBase UC thread...")
        extraction_params = self.config.get("extraction_params", {})
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name
        loop = asyncio.get_running_loop()

        self.case_urls, self.url_to_case_number = await asyncio.to_thread(
            self._indexing_sync, loop, db_record_type
        )
        logger.info(f"[Stage 1A complete] Total downstream asset indexes harvested: {len(self.case_urls)}")

    async def _save_record_to_db(self, case_url: str, record: dict, doc_date: dt_date) -> None:
        """Thread-safe helper to write detailed scraped records to the database."""
        async with self.db_lock:
            await db_storage.upsert_scraped_record(
                self.conn, self.target_id, self.pipeline_name, case_url, record, doc_date, status="detailed"
            )

    def _detailing_worker_thread(
        self,
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        total_cases: int,
    ) -> None:
        """Synchronous thread running a dedicated SB UC instance for processing case items."""
        logger.info(f"[Worker {worker_id}] Starting detailing worker thread...")

        # Stagger worker startup to avoid race conditions during concurrent Chrome process creation
        if worker_id > 1:
            stagger_delay = (worker_id - 1) * 2.0
            logger.info(f"[Worker {worker_id}] Staggering startup by {stagger_delay:.1f}s...")
            time.sleep(stagger_delay)

        worker_original_use_proxy = self.use_proxy
        worker_current_use_proxy = worker_original_use_proxy
        worker_profile = f"/tmp/saflii_worker_profile_{worker_id}"
        max_cases_per_session = 100

        while True:
            # Check if queue is empty before launching/re-launching browser
            if work_queue.empty():
                break

            # Clean user data directory before launching browser session
            shutil.rmtree(worker_profile, ignore_errors=True)
            os.makedirs(worker_profile, exist_ok=True)

            logger.info(f"[Worker {worker_id}] Launching browser session (profile: {worker_profile}) [Proxy: {worker_current_use_proxy}]...")
            session_cases = 0

            try:
                sb_proxy = format_proxy_for_sb(self.proxy_url) if worker_current_use_proxy else None
                with SB(
                    uc=True,
                    headless=self.headless,
                    proxy=sb_proxy,
                    multi_proxy=worker_current_use_proxy,
                    test=True,
                    xvfb=False,  # Global Xvfb display managed at process level in detailing()
                    user_data_dir=worker_profile,
                    chromium_arg="--no-sandbox,--disable-dev-shm-usage"
                ) as sb:
                    sb.set_window_size(1280, 720)
                    sb.driver.set_page_load_timeout(30)
                    sb.driver.set_script_timeout(30)

                    # Verify and log outbound public IP
                    self.log_outbound_ip(sb, label=f"SAFLII Detailing Worker {worker_id}")

                    while session_cases < max_cases_per_session:
                        try:
                            item = work_queue.get(timeout=1.0)
                        except queue.Empty:
                            if work_queue.empty():
                                break
                            continue

                        if item is None:
                            work_queue.task_done()
                            logger.info(f"[Worker {worker_id}] Sentinel received. Terminating thread execution.")
                            return

                        try:
                            if len(item) == 3:
                                idx, case_url, pass_number = item
                            else:
                                idx, case_url = item
                                pass_number = 1

                            c_court, c_year, c_id = parse_case_url(case_url, default_court="SAFLII")
                            case_no = self.url_to_case_number.get(case_url)

                            # Check if the case requires a different proxy setting
                            # Pass 1-3 uses original proxy; Pass 4-6 uses opposite proxy
                            expected_use_proxy = worker_original_use_proxy if pass_number <= 3 else (not worker_original_use_proxy)
                            if worker_current_use_proxy != expected_use_proxy:
                                logger.info(
                                    f"[Worker {worker_id}] Case {c_id} requires different proxy state "
                                    f"(current: {worker_current_use_proxy}, expected: {expected_use_proxy}). "
                                    f"Recycling browser session to match..."
                                )
                                # Re-queue the item so we don't lose it
                                work_queue.put(item)
                                # Update the proxy setting for the next session
                                worker_current_use_proxy = expected_use_proxy
                                # Break the inner loop to close the current browser and trigger a new one
                                break

                            if case_url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                                logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Skipping pre-existing record: {case_url}")
                                session_cases += 1
                                continue

                            logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Detailed enrichment active [Pass {pass_number}/6] -> {case_url}")
                            success = False

                            for attempt in range(1, 4):
                                try:
                                    state = self._navigate_and_handle_turnstile(
                                        sb, case_url, f"worker_{worker_id}_case_{c_id}_pass_{pass_number}_att_{attempt}"
                                    )
                                    if state == "BLOCKED":
                                        raise BlockedException(f"Turnstile block on asset: {c_id}")
                                    elif state == "NOT_FOUND":
                                        if attempt < 3:
                                            logger.warning(
                                                f"[Worker {worker_id}][{idx}/{total_cases}] NOT_FOUND on case {c_id} "
                                                f"[Pass {pass_number}, attempt {attempt}] — may be Turnstile misclassification. "
                                                f"Attempting UC reconnect solve before next attempt..."
                                            )
                                            try:
                                                sb.uc_open_with_reconnect(case_url, reconnect_time=5)
                                                sb.sleep(3)
                                                try:
                                                    with gui_lock:
                                                        sb.uc_gui_handle_captcha()
                                                    sb.sleep(2)
                                                except Exception:
                                                    pass
                                            except Exception:
                                                pass
                                            raise BlockedException(f"NOT_FOUND (possible Turnstile misclassification) on asset: {c_id}")
                                        else:
                                            raise Exception(f"Resource missing (state: {state})")
                                    elif state == "NAVIGATION_FAILED":
                                        raise Exception(f"Browser stuck on previous page, failed to navigate to target URL '{case_url}'")

                                    is_pdf = case_url.lower().endswith(".pdf")

                                    if is_pdf:
                                        # --- PDF document: download via browser session and extract text ---
                                        logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] PDF detected, downloading via browser session...")
                                        pdf_bytes = None

                                        # Primary: use JavaScript fetch inside browser context (inherits session cookies & CF clearance)
                                        js_script = """
                                        var callback = arguments[arguments.length - 1];
                                        fetch(arguments[0])
                                            .then(r => r.arrayBuffer())
                                            .then(buf => {
                                                var bytes = new Uint8Array(buf);
                                                var binary = '';
                                                for (var i = 0; i < bytes.byteLength; i++) {
                                                    binary += String.fromCharCode(bytes[i]);
                                                }
                                                callback(btoa(binary));
                                            })
                                            .catch(err => callback('ERROR:' + err));
                                        """
                                        try:
                                            b64_data = sb.execute_async_script(js_script, case_url)
                                            if b64_data and not str(b64_data).startswith("ERROR:"):
                                                import base64
                                                pdf_bytes = base64.b64decode(b64_data)
                                        except Exception as js_err:
                                            logger.warning(f"[Worker {worker_id}] JS fetch failed: {js_err}")

                                        if not pdf_bytes:
                                            try:
                                                pdf_bytes = sb.download_file(case_url)
                                            except Exception as dl_err:
                                                logger.warning(f"[Worker {worker_id}] sb.download_file failed: {dl_err}")

                                        # Extract text from PDF bytes using PyMuPDF / fitz if installed
                                        pdf_text = ""
                                        pdf_title = ""
                                        if pdf_bytes and pymupdf is not None:
                                            try:
                                                doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
                                                pdf_title = doc.metadata.get("title", "") or ""
                                                pages_text = [page.get_text() for page in doc]
                                                pdf_text = "\n".join(pages_text)
                                                doc.close()
                                            except Exception as pdf_err:
                                                logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] PDF text extraction failed: {pdf_err}")
                                        elif pymupdf is None:
                                            logger.warning(f"[Worker {worker_id}] PyMuPDF/fitz not installed in worker environment; skipping PDF text extraction.")

                                        title = pdf_title or sb.get_page_title() or c_id
                                        center_html = ""  # No HTML content for PDFs
                                        full_text = pdf_text

                                    else:
                                        # --- HTML document: parse with BeautifulSoup ---
                                        soup = BeautifulSoup(sb.get_page_source(), "lxml")
                                        center_div = (
                                            soup.find("div", id="center")
                                            or soup.find("div", class_="judgment")
                                            or soup.find("article")
                                            or soup.find("body")
                                        )
                                        center_html = str(center_div) if center_div else ""

                                        h2_el = center_div.find("h2") if center_div else None
                                        title = h2_el.get_text(strip=True) if h2_el else sb.get_page_title()
                                        full_text = center_div.get_text(separator="\n", strip=True) if center_div else ""

                                    # If the PDF has no text extracted (scanned PDF or download failed), mark for review
                                    requires_human_review = False
                                    if is_pdf and not full_text.strip():
                                        requires_human_review = True
                                        logger.warning(
                                            f"[Worker {worker_id}][{idx}/{total_cases}] Scanned PDF or empty text detected "
                                            f"for {case_url}. Marking as requires_human_review = True."
                                        )

                                    if not case_no:
                                        case_no = extract_case_number_from_text(title)

                                    if case_no and case_no in self.existing_case_numbers:
                                        logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Duplicate signature isolated via late mapping: {case_no}")
                                        success = True
                                        break

                                    record = {
                                        "court": c_court,
                                        "year": c_year,
                                        "case_id": c_id,
                                        "title": title,
                                        "url": case_url,
                                        "case_number": case_no,
                                        "document_type": "pdf" if is_pdf else "html",
                                        "center_content": center_html,
                                        "full_text": full_text,
                                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                                        "worker_id": worker_id,
                                        "requires_human_review": requires_human_review,
                                    }

                                    try:
                                        doc_date = dt_date(int(c_year), 1, 1)
                                    except Exception:
                                        doc_date = dt_date.today()

                                    # Dispatch async DB save (with 30s timeout)
                                    future = asyncio.run_coroutine_threadsafe(
                                        self._save_record_to_db(case_url, record, doc_date),
                                        loop
                                    )
                                    future.result(timeout=30)

                                    # Dispatch async progress state save (with 30s timeout)
                                    current_y = int(c_year) if c_year.isdigit() else datetime.now().year
                                    future_prog = asyncio.run_coroutine_threadsafe(
                                        self.save_progress(
                                            year=current_y,
                                            court_code=c_court,
                                            last_index=idx,
                                            total_cases=total_cases,
                                            last_url=case_url,
                                            completed=False,
                                        ),
                                        loop
                                    )
                                    future_prog.result(timeout=30)

                                    self.existing_urls.add(case_url)
                                    if case_no:
                                        self.existing_case_numbers.add(case_no)

                                    logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] [+] Saved record: {c_court}_{c_year}_{c_id}")
                                    success = True
                                    break

                                except BlockedException as be:
                                    proxy_log = "for a new proxy IP" if worker_current_use_proxy else "to rotate IP/session clearance"
                                    logger.warning(
                                        f"[Worker {worker_id}][{idx}/{total_cases}] 🛑 Turnstile block detected on case page {case_url} ({be}). "
                                        f"Assuming current IP is blocked by Cloudflare. Re-queueing case and recycling browser {proxy_log}..."
                                    )
                                    if pass_number < 6:
                                        work_queue.put((idx, case_url, pass_number + 1))
                                    raise  # Break out to recycle browser session and rotate proxy IP
                                except Exception as err:
                                    err_msg = str(err)
                                    logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] Processing error on case {c_id} [Pass {pass_number}, attempt {attempt}]: {err}")
                                    _fatal_session_keywords = (
                                        "no such window",
                                        "target window already closed",
                                        "web view not found",
                                        "invalid session id",
                                        "connection refused",
                                    )
                                    if any(w in err_msg.lower() for w in _fatal_session_keywords):
                                        logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Window or session connection disrupted. Re-queueing item and recycling browser...")
                                        if pass_number < 6:
                                            work_queue.put((idx, case_url, pass_number + 1))
                                        raise  # Break out to recycle browser session

                                    if attempt < 3:
                                        sb.sleep(attempt * 3)

                            if success:
                                sb.sleep(self.cooldown_seconds + random.uniform(0.3, 0.9))
                            else:
                                if pass_number < 6:
                                    logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] ⚠️ Case {c_id} ({case_url}) failed all attempts on Pass {pass_number}/6. Re-queueing for retry pass {pass_number + 1}...")
                                    work_queue.put((idx, case_url, pass_number + 1))
                                    sb.sleep(2)
                                else:
                                    logger.error(f"[Worker {worker_id}][{idx}/{total_cases}] ❌ Case {c_id} ({case_url}) permanently failed after 6 retry passes.")

                            session_cases += 1

                        finally:
                            work_queue.task_done()

            except Exception as browser_err:
                logger.error(f"[Worker {worker_id}] Browser session failed or crashed: {browser_err}. Recycling browser session...")
                time.sleep(3)

        logger.info(f"[Worker {worker_id}] Thread execution complete.")

    async def detailing(self) -> None:
        """Sub-process B: Perform deep enrichment processing using concurrent SB UC worker threads."""
        import sys
        if "-n" not in sys.argv:
            sys.argv.append("-n")

        extraction_params = self.config.get("extraction_params", {})
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name

        # Refresh existing_urls to only contain URLs already detailed,
        # so indexed-but-not-yet-detailed records are NOT skipped
        try:
            detailed_urls = await db_storage.get_existing_urls_by_status(
                self.conn, db_record_type, status="detailed"
            )
            self.existing_urls = set(detailed_urls)
            logger.info(f"Refreshed existing_urls for detailing: {len(self.existing_urls)} already-detailed URLs.")
        except AttributeError:
            # Fallback if get_existing_urls_by_status doesn't exist yet
            logger.warning("get_existing_urls_by_status not available; using unfiltered existing_urls.")

        if not self.case_urls:
            # Query extracted_records table dynamically to load all pending records needing detail
            logger.info("No in-memory case URLs found; querying extracted_records for pending records needing detail...")
            db_records = await db_storage.load_records_needing_detail(
                self.conn,
                db_record_type,
                sort_desc=False,
                limit=None,
                include_data=False,
            )
            if db_records:
                self.case_urls = [r["source_url"] for r in db_records if r.get("source_url")]
                logger.info(f"Loaded {len(self.case_urls)} pending URLs from extracted_records.")
            else:
                logger.info("No pending records needing detail found in extracted_records. Detailing complete.")
                return

        concurrency = int(extraction_params.get("concurrency", 4))
        logger.info(f"[Stage 1B start] Starting detailing with {concurrency} SeleniumBase UC worker threads...")

        # Start a single global Xvfb display for the process if in non-headless/Xvfb mode
        xvfb_display = None
        should_use_xvfb = self.use_xvfb or (not self.headless and (os.path.exists("/.dockerenv") or not os.environ.get("DISPLAY")))

        if should_use_xvfb:
            try:
                from pyvirtualdisplay import Display
                xvfb_display = Display(visible=0, size=(1440, 900))
                xvfb_display.start()
                logger.info(f"🖥️ Started process-level global Xvfb display on {os.environ.get('DISPLAY')}")
            except Exception as e:
                logger.warning(f"⚠️ Could not start global Xvfb display: {e}")

        try:
            work_queue = queue.Queue()
            for idx, case_url in enumerate(self.case_urls, start=1):
                work_queue.put((idx, case_url))

            # Append sentinel None for each worker thread to signal completion
            for _ in range(concurrency):
                work_queue.put(None)

            loop = asyncio.get_running_loop()
            total_cases = len(self.case_urls)

            worker_tasks = [
                asyncio.to_thread(
                    self._detailing_worker_thread,
                    worker_id=i,
                    work_queue=work_queue,
                    loop=loop,
                    total_cases=total_cases,
                )
                for i in range(1, concurrency + 1)
            ]

            await asyncio.gather(*worker_tasks)

            # Mark completed state upon processing all candidate URLs
            await self.save_progress(
                year=datetime.now().year,
                last_index=total_cases,
                total_cases=total_cases,
                completed=True,
            )
            logger.info("[Stage 1B complete] ✅ Scraping execution layer completely processed.")
        finally:
            if xvfb_display is not None:
                try:
                    xvfb_display.stop()
                    logger.info("🖥️ Terminated global Xvfb display.")
                except Exception:
                    pass

    async def extraction(self) -> None:
        """Stage 2: Post-processing and extraction metrics verification."""
        logger.info("[Stage 2 start] Post-Scraping Data Extraction and Processing.")
        logger.info("[Stage 2 complete] Post-Scraping Data Extraction and Processing complete.")
        pass


# ---------------------------------------------------------------------------
# CLI Execution Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus SAFLII Scraper (SeleniumBase UC Driver)")
    parser.add_argument(
        "--pipeline", "--pipeline_name",
        required=True,
        dest="pipeline_name",
        help="Target pipeline setup configuration key matching environment presets"
    )
    parser.add_argument(
        "--courts",
        default=None,
        help="Comma-separated SAFLII court codes to scrape (e.g. ZACC,ZALCJHB). "
             "Omit to scrape all South African courts.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Target year (e.g. 2026). Omit for auto-discovery of all available years.",
    )
    parser.add_argument(
        "--headless",
        default="false",
        help="Orchestrate worker browser processes via headless mode (true/false)"
    )
    parser.add_argument(
        "--use_xvfb",
        default="true",
        help="Run browser with Xvfb display (true/false)",
    )
    parser.add_argument(
        "--skip_stages",
        default=None,
        help="Comma-separated stage numbers to skip, e.g. '1' or '1,2'. "
             "Stage 1=Indexing, Stage 2=Detailing, Stage 3=Extraction."
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    use_xvfb_value = args.use_xvfb.lower() == "true"

    courts_list = None
    if args.courts:
        courts_list = [c.strip() for c in args.courts.split(",") if c.strip()]

    scraper = SafliiScraper(
        pipeline_name=args.pipeline_name,
        courts=courts_list,
        year=args.year,
        headless=headless_value,
        use_xvfb=use_xvfb_value,
        skip_stages=args.skip_stages,
    )

    asyncio.run(scraper.run())
    sys.exit(0)