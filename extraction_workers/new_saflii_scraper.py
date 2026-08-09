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
from typing import Any, Dict, List, Optional, Tuple, Set

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
# SAFLII Databases Index & Dataset Discovery Constants
# ---------------------------------------------------------------------------

DATABASES_INDEX_URL = "https://www.saflii.org/content/databases.html"

# Valid SAFLII URL path prefixes for South African datasets
_VALID_ZA_PATH_PREFIXES = ("/za/cases/", "/za/gaz/", "/za/journals/", "/za/other/")


def extract_dataset_code_from_url(url: str) -> Optional[str]:
    """Extract the SAFLII dataset/dataset code from a URL path.

    Recognises patterns: ``/za/datasets/{CODE}/``, ``/za/gaz/{CODE}/``,
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


class RetryItemException(Exception):
    """Raised when processing a queue item fails in a way that requires recycling the browser and retrying the same item immediately."""
    def __init__(self, message, item, next_proxy=None):
        super().__init__(message)
        self.item = item
        self.next_proxy = next_proxy


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
    # the string "404" (e.g. a real dataset titled "ZALCJHB 404 [2025]" is NOT a 404 page).
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


def parse_dataset_url(dataset_url: str, default_dataset: str = "SAFLII") -> Tuple[str, str, str]:
    """Parse a valid SAFLII asset path to map structural parameters (dataset_code, year, document_id)."""
    parsed = urllib.parse.urlparse(dataset_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3:
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 4:
                return parts[i - 1], parts[i], os.path.splitext(parts[i + 1])[0]
    return default_dataset, "unknown", "unknown"


def get_dataset_category_from_url(url: str) -> str:
    """Extract category (cases, gaz, journals, other) from SAFLII URL path."""
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "za" and i + 1 < len(parts):
            category = parts[i + 1]
            if category in ("cases", "gaz", "journals", "other"):
                return category
    return "other"


def extract_dataset_number_from_text(text: str) -> Optional[str]:
    """Extract standard SAFLII dataset numbers or formal citations from strings."""
    if not text:
        return None

    # Matches actual documents containing "Case No:" or "Case Number:" or similar text.
    # Generalized search patterns to isolate identifiers.
    match = re.search(r'(?:Case|Application|Matter|Dataset|Gazette|Gaz)\s+(?:No|Number|Ref)\s*:\s*([A-Za-z0-9/ -]+)', text, re.IGNORECASE)
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


def extract_metadata_by_category(category: str, title: str, text: str) -> Dict[str, Any]:
    """Extract category-specific metadata fields based on document category."""
    metadata = {}

    if category == "cases":
        # Extract case number
        case_no = extract_dataset_number_from_text(text)
        if case_no:
            metadata["case_number"] = case_no

        # Extract citation
        citation_match = re.search(r'\[\d{4}\]\s+[A-Z]+\s+\d+', text)
        if citation_match:
            metadata["citation"] = citation_match.group(0).strip()

    elif category == "gaz":
        # Extract gazette number
        gaz_no_match = re.search(
            r'(?:Gazette|Gaz)\s+(?:No|Number)\s*:\s*([A-Za-z0-9/ -]+)',
            text,
            re.IGNORECASE
        )
        if gaz_no_match:
            metadata["gazette_number"] = gaz_no_match.group(1).strip()
        else:
            # Look for "No. 12345" or similar near "Gazette" or "Gaz"
            gaz_no_match2 = re.search(
                r'(?:Government\s+)?Gazette\s+.*?\b(?:No\.?|Number)\s*(\d+)',
                text,
                re.IGNORECASE
            )
            if gaz_no_match2:
                metadata["gazette_number"] = gaz_no_match2.group(1).strip()

    elif category == "journals":
        # Extract volume, issue, authors
        vol_match = re.search(r'\b(?:Vol\.?|Volume)\s*(\d+)', text, re.IGNORECASE)
        if vol_match:
            metadata["volume"] = vol_match.group(1).strip()

        issue_match = re.search(r'\b(?:No\.?|Issue)\s*(\d+)', text, re.IGNORECASE)
        if issue_match:
            metadata["issue"] = issue_match.group(1).strip()

    return metadata


# ---------------------------------------------------------------------------
# Core Framework Implementation (SeleniumBase UC Mode + Multithreaded Worker)
# ---------------------------------------------------------------------------

class SafliiScraper(BaseScraper):
    """
    SAFLII Scraper subclassing BaseScraper.
    Discovers all South African legal datasets from the SAFLII databases index
    page, uses SeleniumBase UC mode with lxml parsing, multithreaded detailing
    workers, database record persistence, progress tracking, and proxy support.
    """

    def __init__(
        self,
        pipeline_name: str,
        datasets: Optional[List[str]] = None,
        year: Optional[int] = None,
        headless: bool = False,
        use_xvfb: bool = True,
        skip_stages: Optional[str] = None,
    ):
        super().__init__(pipeline_name, skip_stages=skip_stages)
        self.dataset_filter: Optional[List[str]] = datasets
        self.year: Optional[int] = year
        self.headless: bool = headless
        self.use_xvfb: bool = use_xvfb

        self.index_url: str = DATABASES_INDEX_URL
        self.cooldown_seconds: float = 1.5
        self.take_debug_screenshots: bool = False
        self.screenshots_dir: str = ""

        # Multi-dataset state
        self.dataset_base_urls: Dict[str, Tuple[str, str]] = {}  # code -> (display_name, base_url)
        self.dataset_urls: List[str] = []
        self.url_to_dataset_number: Dict[str, str] = {}
        
        # Concurrency & State locking
        self.db_lock = asyncio.Lock()
        self.state_lock = threading.Lock()
        self._dataset_target_ids: Dict[str, Any] = {}

    def _is_duplicate_url(self, url: str) -> bool:
        """Thread-safe check for existing URL."""
        with self.state_lock:
            return url in self.existing_urls

    def _is_duplicate_dataset_number(self, dataset_no: str) -> bool:
        """Thread-safe check for existing dataset number."""
        with self.state_lock:
            return dataset_no in self.existing_dataset_numbers

    def _mark_scraped_state(self, url: str, dataset_no: Optional[str]) -> None:
        """Thread-safe registration of scraped URL and identifier."""
        with self.state_lock:
            self.existing_urls.add(url)
            if dataset_no:
                self.existing_dataset_numbers.add(dataset_no)

    async def _get_target_id_for_dataset(self, dataset_code: str, dataset_url: Optional[str] = None) -> Any:
        """Resolve the target ID dynamically for a given dataset code."""
        if dataset_code not in self._dataset_target_ids:
            display_name = dataset_code
            base_url = None

            if dataset_code in self.dataset_base_urls:
                display_name, base_url = self.dataset_base_urls[dataset_code]
            elif dataset_url:
                parsed = urllib.parse.urlparse(dataset_url)
                parts = [p for p in parsed.path.split("/") if p]
                for i, part in enumerate(parts):
                    if part == "za" and i + 2 < len(parts):
                        category = parts[i + 1]
                        if category in ("cases", "gaz", "journals", "other"):
                            base_url = f"{parsed.scheme}://{parsed.netloc}/za/{category}/{parts[i + 2]}/"
                            break

            if not base_url:
                base_url = f"https://www.saflii.org/za/cases/{dataset_code}/"

            entity_name = self.config.get("name") or self.pipeline_name
            target_id = await db_storage.resolve_target_id(
                self.conn, entity_name, dataset_code, base_url
            )
            self._dataset_target_ids[dataset_code] = target_id
            logger.info(f"Resolved target ID for dataset {dataset_code}: {target_id}")

        return self._dataset_target_ids[dataset_code]

    async def initialize(self) -> None:
        """Hydrate configuration, resolve target IDs, DB connections, and progress state."""
        await super().initialize()

        extraction_params = self.config.get("extraction_params", {})

        # Allow index URL override from config; fall back to DATABASES_INDEX_URL
        self.index_url = self.config.get("start_url") or DATABASES_INDEX_URL

        # Dataset filter: CLI argument takes precedence, then config extraction_params
        if not self.dataset_filter:
            cfg_datasets = extraction_params.get("datasets")
            if cfg_datasets:
                if isinstance(cfg_datasets, str):
                    self.dataset_filter = [c.strip() for c in cfg_datasets.split(",") if c.strip()]
                elif isinstance(cfg_datasets, list):
                    self.dataset_filter = cfg_datasets

        self.cooldown_seconds = float(extraction_params.get("cooldown_seconds", 1.5))
        self.take_debug_screenshots = self.config.get("take_debug_screenshots", False)

        if self.output_dir:
            self.screenshots_dir = os.path.join(os.path.dirname(self.output_dir), "screenshots")
            os.makedirs(self.screenshots_dir, exist_ok=True)

        datasets_desc = ", ".join(self.dataset_filter) if self.dataset_filter else "ALL (auto-discover)"
        year_desc = str(self.year) if self.year else "auto-discover"

        logger.info("==================================================")
        logger.info(f"🚀 COEUS SAFLII WORKER (SeleniumBase UC Mode) ({self.pipeline_name})")
        logger.info(f"Target Datasets: {datasets_desc} | Year: {year_desc}")
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

        # 2. Open URL via sb.open()
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

            # Attempt 1: UC GUI captcha click
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

            # Attempt 2: UC GUI handle captcha
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
    # Dataset & Year Discovery
    # ------------------------------------------------------------------

    def _discover_dataset_urls_from_page(self, sb: SB) -> Dict[str, Tuple[str, str]]:
        """Navigate to SAFLII databases index page and extract all South African dataset/dataset URLs.

        Returns dict mapping ``dataset_code`` → ``(display_name, base_url)``.
        """
        datasets: Dict[str, Tuple[str, str]] = {}

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

                dataset_code = extract_dataset_code_from_url(abs_url)
                if dataset_code:
                    base_url = abs_url.rstrip("/") + "/"
                    datasets[dataset_code] = (link_text, base_url)

            if datasets:
                logger.info(f"Discovered {len(datasets)} South African dataset URLs from index page.")
                break
            else:
                logger.warning(
                    f"No dataset URLs found on attempt {attempt}/3. Page may not have loaded correctly."
                )
                if attempt < 3:
                    sb.sleep(attempt * 4)

        return datasets

    def _discover_year_links_from_page(
        self, sb: SB, dataset_code: str, base_url: str
    ) -> List[Tuple[int, str]]:
        """Navigate to a dataset's base page and discover available year directory links.

        Year links typically appear as ``<a href="YYYY/">`` inside ``<h3>`` elements.

        Returns list of ``(year, year_url)`` tuples sorted by year.
        """
        year_links: List[Tuple[int, str]] = []

        for attempt in range(1, 4):
            state = self._navigate_and_handle_turnstile(
                sb, base_url, f"dataset_{dataset_code}_years_attempt_{attempt}"
            )
            if state == "BLOCKED":
                logger.warning(f"Dataset page {dataset_code} blocked on attempt {attempt}/3.")
                if attempt < 3:
                    sb.sleep(attempt * 4)
                    continue
                else:
                    logger.error(
                        f"Failed to access dataset page {dataset_code} after 3 attempts. Skipping."
                    )
                    return []
            if state == "NOT_FOUND":
                logger.warning(f"Dataset page {dataset_code} returned NOT_FOUND. Skipping.")
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
                    f"Dataset {dataset_code}: Discovered {len(year_links)} year directories: "
                    f"{[y for y, _ in year_links]}"
                )
                break
            else:
                logger.warning(f"Dataset {dataset_code}: No year links found on attempt {attempt}/3.")
                if attempt < 3:
                    sb.sleep(attempt * 4)

        return year_links

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _discover_and_filter_datasets(self, sb_proxy: Optional[str], indexing_profile: str) -> Dict[str, Tuple[str, str]]:
        """Navigate to the databases index page, discover available datasets, and apply filter rules."""
        shutil.rmtree(indexing_profile, ignore_errors=True)
        os.makedirs(indexing_profile, exist_ok=True)
        logger.info(f"Discovering dataset URLs from: {self.index_url}")
        
        dataset_base_urls = {}
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

                self.log_outbound_ip(sb, label="SAFLII Indexing Stage (Discovery)")
                dataset_base_urls = self._discover_dataset_urls_from_page(sb)
        except Exception as discovery_err:
            logger.error(f"Failed to discover dataset URLs during indexing startup: {discovery_err}")
            return {}

        if not dataset_base_urls:
            logger.error("No dataset URLs discovered from index page. Aborting indexing.")
            return {}

        # Apply dataset filter if specified
        if self.dataset_filter:
            filter_set = {c.upper() for c in self.dataset_filter}
            filtered = {
                k: v for k, v in dataset_base_urls.items() if k.upper() in filter_set
            }
            skipped = set(dataset_base_urls.keys()) - set(filtered.keys())
            if skipped:
                logger.info(
                    f"Dataset filter active. Skipping {len(skipped)} datasets: {sorted(skipped)}"
                )
            dataset_base_urls = filtered

        return dataset_base_urls

    def _index_single_dataset(
        self,
        sb: SB,
        loop: asyncio.AbstractEventLoop,
        db_record_type: str,
        dataset_code: str,
        display_name: str,
        base_url: str,
        indexed_this_run: Set[str],
        url_to_dataset_map: Dict[str, str]
    ) -> List[str]:
        """Harvest target candidate document URLs for a single dataset across configured years."""
        if self.year:
            year_links = [(self.year, f"{base_url}{self.year}/")]
        else:
            year_links = self._discover_year_links_from_page(sb, dataset_code, base_url)

        if not year_links:
            logger.warning(f"Dataset {dataset_code}: No year directories found. Skipping.")
            return []

        dataset_new_urls: List[str] = []

        # Process each year directory
        for year, year_url in year_links:
            logger.info(
                f"Processing structural year context directory: {dataset_code}/{year} -> {year_url}"
            )
            found_for_year = 0
            for attempt in range(1, 4):
                try:
                    state = self._navigate_and_handle_turnstile(
                        sb, year_url,
                        f"dataset_{dataset_code}_year_{year}_attempt_{attempt}"
                    )
                    if state == "BLOCKED":
                        logger.warning(
                            f"Year directory {dataset_code}/{year} blocked on attempt {attempt}/3."
                        )
                        if attempt < 3:
                            sb.sleep(attempt * 4)
                            continue
                        else:
                            break

                    if state == "NOT_FOUND":
                        logger.info(
                            f"Dataset {dataset_code} year {year}: Directory not found."
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
                                    if not self._is_duplicate_url(abs_url) and abs_url not in indexed_this_run:
                                        dataset_no = extract_dataset_number_from_text(a_tag.get_text(strip=True))
                                        if dataset_no:
                                            url_to_dataset_map[abs_url] = dataset_no
                                        dataset_new_urls.append(abs_url)
                                        found_for_year += 1
                    logger.info(
                        f"Dataset {dataset_code} year {year}: "
                        f"Harvested {found_for_year} new candidate URLs."
                    )
                    break
                except Exception as err:
                    logger.warning(
                        f"Error isolating index structures for {dataset_code}/{year} "
                        f"[Attempt {attempt}]: {err}"
                    )
                    if attempt < 3:
                        sb.sleep(attempt * 4)

            sb.sleep(self.cooldown_seconds + random.uniform(0.2, 0.6))

        # Batch-save this dataset's newly discovered URLs to DB for restart resilience
        if dataset_new_urls:
            batch_records = []
            for curl in dataset_new_urls:
                cc, cy, cid = parse_dataset_url(curl, default_dataset=dataset_code)
                rec = {
                    "detail_url": curl,
                    "dataset": cc,
                    "year": cy,
                    "entry_id": cid,
                }
                cn = url_to_dataset_map.get(curl)
                if cn:
                    rec["dataset_number"] = cn
                batch_records.append(rec)

            try:
                # Dynamically resolve target_id for the current dataset
                dataset_target_id = asyncio.run_coroutine_threadsafe(
                    self._get_target_id_for_dataset(dataset_code),
                    loop,
                ).result()

                future = asyncio.run_coroutine_threadsafe(
                    db_storage.upsert_scraped_records_batch(
                        self.conn,
                        dataset_target_id,
                        db_record_type,
                        batch_records,
                        url_key="detail_url",
                        status="indexed",
                    ),
                    loop,
                )
                saved = future.result(timeout=60)
                logger.info(
                    f"Dataset {dataset_code}: Saved {saved} indexed records to database."
                )
            except Exception as save_err:
                logger.error(
                    f"Dataset {dataset_code}: Failed to batch-save indexed records: {save_err}"
                )

            indexed_this_run.update(dataset_new_urls)

        return dataset_new_urls

    def _indexing_sync(
        self, loop: asyncio.AbstractEventLoop, db_record_type: str
    ) -> Tuple[List[str], Dict[str, str]]:
        """Synchronous indexing task running inside a dedicated SB UC context thread.

        Discovers datasets from the databases index page, auto-discovers available
        years per dataset, and harvests document URLs.  Indexed URLs are batch-saved
        to the database per-dataset for restart resilience.
        """
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        raw_dataset_urls: List[str] = []
        url_to_dataset_map: Dict[str, str] = {}
        indexed_this_run: Set[str] = set()

        indexing_profile = "/tmp/saflii_indexing_profile"

        # 1. Discover and apply configuration filters to dataset sources
        self.dataset_base_urls = self._discover_and_filter_datasets(sb_proxy, indexing_profile)
        if not self.dataset_base_urls:
            return [], {}

        total_datasets = len(self.dataset_base_urls)
        logger.info(f"Processing {total_datasets} datasets: {sorted(self.dataset_base_urls.keys())}")

        # 2. For each dataset, discover years and harvest document URLs in a recycled browser session
        for entry_idx, (dataset_code, (display_name, base_url)) in enumerate(
            sorted(self.dataset_base_urls.items()), 1
        ):
            logger.info(f"\n{'=' * 60}")
            logger.info(
                f"[Dataset {entry_idx}/{total_datasets}] {display_name} ({dataset_code})"
            )
            logger.info(f"Base URL: {base_url}")
            logger.info("=" * 60)

            # Clean user data directory before launching browser session for this dataset
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
                   self.log_outbound_ip(sb, label=f"SAFLII Indexing Stage - {dataset_code}")

                   dataset_new_urls = self._index_single_dataset(
                       sb=sb,
                       loop=loop,
                       db_record_type=db_record_type,
                       dataset_code=dataset_code,
                       display_name=display_name,
                       base_url=base_url,
                       indexed_this_run=indexed_this_run,
                       url_to_dataset_map=url_to_dataset_map
                   )
                   raw_dataset_urls.extend(dataset_new_urls)

            except Exception as dataset_err:
                logger.error(f"Dataset {dataset_code}: Browser session failed or crashed: {dataset_err}. Recycling browser session...")

        return sorted(list(set(raw_dataset_urls))), url_to_dataset_map

    async def indexing(self) -> None:
        """Sub-process A: Harvest dataset entry indexes across all discovered datasets."""
        logger.info("[Stage 1A start] Starting indexing stage via SeleniumBase UC thread...")
        extraction_params = self.config.get("extraction_params", {})
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name
        loop = asyncio.get_running_loop()

        self.dataset_urls, self.url_to_dataset_number = await asyncio.to_thread(
            self._indexing_sync, loop, db_record_type
        )
        logger.info(f"[Stage 1A complete] Total downstream asset indexes harvested: {len(self.dataset_urls)}")

    async def _save_record_to_db(self, dataset_url: str, record: dict, doc_date: Optional[dt_date]) -> None:
        """Thread-safe helper to write detailed scraped records to the database."""
        dataset_code = record.get("dataset") or "SAFLII"
        dataset_target_id = await self._get_target_id_for_dataset(dataset_code, dataset_url)
        
        # Determine fallback document date if none provided
        resolved_date = doc_date
        if not resolved_date:
            year = record.get("year")
            try:
                resolved_date = dt_date(int(year), 1, 1)
            except Exception:
                resolved_date = dt_date.today()

        async with self.db_lock:
            await db_storage.upsert_scraped_record(
                self.conn, dataset_target_id, self.pipeline_name, dataset_url, record, resolved_date, status="detailed"
            )

    # ------------------------------------------------------------------
    # Detailing Worker & Loop
    # ------------------------------------------------------------------

    def _extract_page_content(
        self,
        sb: SB,
        dataset_url: str,
        entry_id: str,
        is_pdf: bool,
        worker_id: int,
        idx: int,
        total_datasets: int
    ) -> Tuple[str, str, str, bool]:
        """Route content extraction depending on target file type (PDF vs HTML)."""
        requires_human_review = False
        if is_pdf:
            title, full_text = self._extract_text_from_pdf(sb, dataset_url, entry_id, worker_id, idx, total_datasets)
            center_html = ""
            if not full_text.strip():
                requires_human_review = True
                logger.warning(
                    f"[Worker {worker_id}][{idx}/{total_datasets}] Scanned PDF or empty text detected "
                    f"for {dataset_url}. Marking as requires_human_review = True."
                )
        else:
            title, center_html, full_text = self._extract_content_from_html(sb, entry_id)

        return title, center_html, full_text, requires_human_review

    def _extract_text_from_pdf(
        self,
        sb: SB,
        dataset_url: str,
        entry_id: str,
        worker_id: int,
        idx: int,
        total_datasets: int
    ) -> Tuple[str, str]:
        """Download PDF within browser context and parse text using PyMuPDF."""
        logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] PDF detected, downloading via browser session...")
        pdf_bytes = None

        # Primary: JavaScript fetch within current page context to pass CF/session cookies
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
            b64_data = sb.execute_async_script(js_script, dataset_url)
            if b64_data and not str(b64_data).startswith("ERROR:"):
                import base64
                pdf_bytes = base64.b64decode(b64_data)
        except Exception as js_err:
            logger.warning(f"[Worker {worker_id}] JS fetch failed: {js_err}")

        if not pdf_bytes:
            try:
                pdf_bytes = sb.download_file(dataset_url)
            except Exception as dl_err:
                logger.warning(f"[Worker {worker_id}] sb.download_file failed: {dl_err}")

        # Extract text using PyMuPDF / fitz
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
                logger.warning(f"[Worker {worker_id}][{idx}/{total_datasets}] PDF text extraction failed: {pdf_err}")
        elif pymupdf is None:
            logger.warning(f"[Worker {worker_id}] PyMuPDF/fitz not installed; skipping PDF text extraction.")

        title = pdf_title or sb.get_page_title() or entry_id
        return title, pdf_text

    def _extract_content_from_html(self, sb: SB, entry_id: str) -> Tuple[str, str, str]:
        """Locate text containers and extract HTML/CleanText details for HTML records."""
        soup = BeautifulSoup(sb.get_page_source(), "lxml")
        center_div = (
            soup.find("div", id="center")
            or soup.find("div", class_="judgment")
            or soup.find("article")
            or soup.find("body")
        )
        center_html = str(center_div) if center_div else ""

        h2_el = center_div.find("h2") if center_div else None
        title = h2_el.get_text(strip=True) if h2_el else sb.get_page_title() or entry_id
        full_text = center_div.get_text(separator="\n", strip=True) if center_div else ""

        return title, center_html, full_text

    def _save_scraped_result(
        self,
        loop: asyncio.AbstractEventLoop,
        record: dict,
        dataset_url: str,
        dataset_year: str,
        dataset_code: str,
        idx: int,
        total_datasets: int
    ) -> None:
        """Call async storage interfaces thread-safely via the event loop."""
        # Dispatch async DB save (with 30s timeout)
        future = asyncio.run_coroutine_threadsafe(
            self._save_record_to_db(dataset_url, record, None),
            loop
        )
        future.result(timeout=30)

        # Dispatch async progress state save (with 30s timeout)
        current_y = int(dataset_year) if dataset_year.isdigit() else datetime.now().year
        future_prog = asyncio.run_coroutine_threadsafe(
            self.save_progress(
                year=current_y,
                dataset_code=dataset_code,
                last_index=idx,
                total_datasets=total_datasets,
                last_url=dataset_url,
                completed=False,
            ),
            loop
        )
        future_prog.result(timeout=30)

    def _process_queue_item(
        self,
        sb: SB,
        item: Tuple[Any, ...],
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        total_datasets: int,
        worker_original_use_proxy: bool,
        worker_current_use_proxy: bool
    ) -> Tuple[bool, bool]:
        """Process a single queue item. Returns (should_recycle_browser, next_proxy_state)."""
        idx, dataset_url, pass_number = item if len(item) == 3 else (item[0], item[1], 1)

        dataset_code, dataset_year, entry_id = parse_dataset_url(dataset_url, default_dataset="SAFLII")
        dataset_no = self.url_to_dataset_number.get(dataset_url)

        # Check if the dataset requires a different proxy setting
        expected_use_proxy = worker_original_use_proxy if pass_number <= 3 else (not worker_original_use_proxy)
        if worker_current_use_proxy != expected_use_proxy:
            logger.info(
                f"[Worker {worker_id}] Dataset {entry_id} requires different proxy state "
                f"(current: {worker_current_use_proxy}, expected: {expected_use_proxy}). "
                f"Recycling browser session to match..."
            )
            raise RetryItemException(
                f"Proxy state transition to {expected_use_proxy}",
                item,
                next_proxy=expected_use_proxy
            )

        if self._is_duplicate_url(dataset_url):
            logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] Skipping pre-existing record: {dataset_url}")
            return False, worker_current_use_proxy

        logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] Detailed enrichment active [Pass {pass_number}/6] -> {dataset_url}")
        success = False

        for attempt in range(1, 4):
            try:
                state = self._navigate_and_handle_turnstile(
                    sb, dataset_url, f"worker_{worker_id}_dataset_{entry_id}_pass_{pass_number}_att_{attempt}"
                )
                if state == "BLOCKED":
                    raise BlockedException(f"Turnstile block on asset: {entry_id}")
                elif state == "NOT_FOUND":
                    if attempt < 3:
                        logger.warning(
                            f"[Worker {worker_id}][{idx}/{total_datasets}] NOT_FOUND on dataset {entry_id} "
                            f"[Pass {pass_number}, attempt {attempt}] — may be Turnstile misclassification. "
                            f"Attempting UC reconnect solve before next attempt..."
                        )
                        try:
                            sb.uc_open_with_reconnect(dataset_url, reconnect_time=5)
                            sb.sleep(3)
                            with gui_lock:
                                sb.uc_gui_handle_captcha()
                            sb.sleep(2)
                        except Exception:
                            pass
                        raise BlockedException(f"NOT_FOUND (possible Turnstile misclassification) on asset: {entry_id}")
                    else:
                        raise Exception(f"Resource missing (state: {state})")
                elif state == "NAVIGATION_FAILED":
                    raise Exception(f"Browser stuck on previous page, failed to navigate to target URL '{dataset_url}'")

                is_pdf = dataset_url.lower().endswith(".pdf")
                title, center_html, full_text, requires_human_review = self._extract_page_content(
                    sb=sb,
                    dataset_url=dataset_url,
                    entry_id=entry_id,
                    is_pdf=is_pdf,
                    worker_id=worker_id,
                    idx=idx,
                    total_datasets=total_datasets
                )

                if not dataset_no:
                    dataset_no = extract_dataset_number_from_text(title)

                if dataset_no and self._is_duplicate_dataset_number(dataset_no):
                    logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] Duplicate signature isolated via late mapping: {dataset_no}")
                    success = True
                    break

                category = get_dataset_category_from_url(dataset_url)
                record = {
                    "dataset": dataset_code,
                    "year": dataset_year,
                    "entry_id": entry_id,
                    "title": title,
                    "url": dataset_url,
                    "document_type": "pdf" if is_pdf else "html",
                    "category": category,
                    "center_content": center_html,
                    "full_text": full_text,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "worker_id": worker_id,
                    "requires_human_review": requires_human_review,
                }

                category_metadata = extract_metadata_by_category(category, title, full_text)
                record.update(category_metadata)

                # Map category identifiers to dataset_number if not already resolved
                if not dataset_no:
                    if "case_number" in category_metadata:
                        dataset_no = category_metadata["case_number"]
                    elif "gazette_number" in category_metadata:
                        dataset_no = category_metadata["gazette_number"]
                    elif "volume" in category_metadata:
                        dataset_no = f"Vol {category_metadata.get('volume', '')} No {category_metadata.get('issue', '')}".strip()

                if dataset_no:
                    record["dataset_number"] = dataset_no

                self._save_scraped_result(
                    loop=loop,
                    record=record,
                    dataset_url=dataset_url,
                    dataset_year=dataset_year,
                    dataset_code=dataset_code,
                    idx=idx,
                    total_datasets=total_datasets
                )

                self._mark_scraped_state(dataset_url, dataset_no)

                logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] [+] Saved record: {dataset_code}_{dataset_year}_{entry_id}")
                success = True
                break

            except BlockedException as be:
                proxy_log = "for a new proxy IP" if worker_current_use_proxy else "to rotate IP/session clearance"
                logger.warning(
                    f"[Worker {worker_id}][{idx}/{total_datasets}] 🛑 Turnstile block detected on dataset page {dataset_url} ({be}). "
                    f"Assuming current IP is blocked. Recycling browser {proxy_log}..."
                )
                if pass_number < 6:
                    raise RetryItemException(
                        f"Turnstile block on asset: {entry_id}",
                        (idx, dataset_url, pass_number + 1)
                    )
                else:
                    logger.error(f"[Worker {worker_id}][{idx}/{total_datasets}] ❌ Dataset {entry_id} ({dataset_url}) permanently failed after 6 retry passes.")
                    raise
            except Exception as err:
                err_msg = str(err)
                logger.warning(f"[Worker {worker_id}][{idx}/{total_datasets}] Processing error on dataset {entry_id} [Pass {pass_number}, attempt {attempt}]: {err}")
                _fatal_session_keywords = (
                    "no such window",
                    "target window already closed",
                    "web view not found",
                    "invalid session id",
                    "connection refused",
                )
                if any(w in err_msg.lower() for w in _fatal_session_keywords):
                    logger.info(f"[Worker {worker_id}][{idx}/{total_datasets}] Window or session connection disrupted. Recycling browser...")
                    if pass_number < 6:
                        raise RetryItemException(
                            f"Fatal browser error: {err_msg}",
                            (idx, dataset_url, pass_number + 1)
                        )
                    raise  # Break out to recycle browser session

                if attempt < 3:
                    sb.sleep(attempt * 3)

        if success:
            sb.sleep(self.cooldown_seconds + random.uniform(0.3, 0.9))
        else:
            if pass_number < 6:
                logger.warning(f"[Worker {worker_id}][{idx}/{total_datasets}] ⚠️ Dataset {entry_id} ({dataset_url}) failed all attempts on Pass {pass_number}/6. Re-queueing for retry pass {pass_number + 1}...")
                work_queue.put((idx, dataset_url, pass_number + 1))
                sb.sleep(2)
            else:
                logger.error(f"[Worker {worker_id}][{idx}/{total_datasets}] ❌ Dataset {entry_id} ({dataset_url}) permanently failed after 6 retry passes.")

        return False, worker_current_use_proxy

    def _run_browser_session_loop(
        self,
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        total_datasets: int,
        worker_profile: str,
        worker_original_use_proxy: bool
    ) -> None:
        """Browser launcher and queue processor block."""
        worker_current_use_proxy = worker_original_use_proxy
        max_datasets_per_session = 100
        current_item = None

        while True:
            if work_queue.empty() and current_item is None:
                break

            # Clean user data directory before launching browser session
            shutil.rmtree(worker_profile, ignore_errors=True)
            os.makedirs(worker_profile, exist_ok=True)

            logger.info(f"[Worker {worker_id}] Launching browser session (profile: {worker_profile}) [Proxy: {worker_current_use_proxy}]...")
            session_datasets = 0

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

                    self.log_outbound_ip(sb, label=f"SAFLII Detailing Worker {worker_id}")

                    while session_datasets < max_datasets_per_session:
                        if current_item is not None:
                            item = current_item
                            current_item = None
                        else:
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
                            should_recycle, next_proxy = self._process_queue_item(
                                sb=sb,
                                item=item,
                                worker_id=worker_id,
                                work_queue=work_queue,
                                loop=loop,
                                total_datasets=total_datasets,
                                worker_original_use_proxy=worker_original_use_proxy,
                                worker_current_use_proxy=worker_current_use_proxy
                            )
                            if should_recycle:
                                worker_current_use_proxy = next_proxy
                                break
                        except RetryItemException as retry_err:
                            current_item = retry_err.item
                            raise
                        finally:
                            pass

                        session_datasets += 1

            except RetryItemException as retry_err:
                logger.info(f"[Worker {worker_id}] Recycling browser session to retry same item immediately: {retry_err}")
                if retry_err.next_proxy is not None:
                    worker_current_use_proxy = retry_err.next_proxy
                time.sleep(3)
            except Exception as browser_err:
                logger.error(f"[Worker {worker_id}] Browser session failed or crashed: {browser_err}. Recycling browser session...")
                time.sleep(3)

    def _detailing_worker_thread(
        self,
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        total_datasets: int,
    ) -> None:
        """Synchronous thread running a dedicated SB UC instance for processing dataset items."""
        logger.info(f"[Worker {worker_id}] Starting detailing worker thread...")

        # Stagger worker startup to avoid race conditions during concurrent Chrome process creation
        if worker_id > 1:
            stagger_delay = (worker_id - 1) * 2.0
            logger.info(f"[Worker {worker_id}] Staggering startup by {stagger_delay:.1f}s...")
            time.sleep(stagger_delay)

        worker_original_use_proxy = self.use_proxy
        worker_profile = f"/tmp/saflii_worker_profile_{worker_id}"

        self._run_browser_session_loop(
            worker_id=worker_id,
            work_queue=work_queue,
            loop=loop,
            total_datasets=total_datasets,
            worker_profile=worker_profile,
            worker_original_use_proxy=worker_original_use_proxy
        )

        logger.info(f"[Worker {worker_id}] Thread execution complete.")

    async def detailing(self) -> None:
        """Sub-process B: Perform deep enrichment processing using concurrent SB UC worker threads."""
        import sys
        if "-n" not in sys.argv:
            sys.argv.append("-n")

        extraction_params = self.config.get("extraction_params", {})
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name

        # Refresh existing_urls and existing_dataset_numbers to only contain records already detailed
        try:
            detailed_urls = await db_storage.get_existing_urls_by_status(
                self.conn, db_record_type, status="detailed"
            )
            with self.state_lock:
                self.existing_urls = set(detailed_urls)
            logger.info(f"Refreshed existing_urls for detailing: {len(self.existing_urls)} already-detailed URLs.")

            detailed_datasets = await db_storage.get_existing_dataset_numbers(
                self.conn, db_record_type, status="detailed"
            )
            with self.state_lock:
                self.existing_dataset_numbers = set(detailed_datasets)
            logger.info(f"Refreshed existing_dataset_numbers for detailing: {len(self.existing_dataset_numbers)} already-detailed dataset numbers.")
        except Exception as refresh_err:
            logger.warning(f"Failed to refresh deduplication state: {refresh_err}. Using baseline deduplication state.")

        # Query extracted_records table dynamically to load all pending records needing detail
        logger.info("Querying database for any additional pending records needing detail...")
        db_records = await db_storage.load_records_needing_detail(
            self.conn,
            db_record_type,
            sort_desc=False,
            limit=None,
            include_data=False,
        )
        db_pending_urls = [r["source_url"] for r in db_records if r.get("source_url")] if db_records else []

        # Merge both sources, preserving order and removing duplicates
        seen = set(self.dataset_urls)
        merged_urls = list(self.dataset_urls)
        for url in db_pending_urls:
            if url not in seen:
                seen.add(url)
                merged_urls.append(url)

        self.dataset_urls = merged_urls
        if not self.dataset_urls:
            logger.info("No pending records needing detail found. Detailing complete.")
            return

        logger.info(
            f"Total dataset URLs scheduled for detailing: {len(self.dataset_urls)} "
            f"({len(merged_urls) - len(db_pending_urls)} new from indexing, "
            f"{len(db_pending_urls)} loaded from database)."
        )

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
            for idx, dataset_url in enumerate(self.dataset_urls, start=1):
                work_queue.put((idx, dataset_url))

            # Append sentinel None for each worker thread to signal completion
            for _ in range(concurrency):
                work_queue.put(None)

            loop = asyncio.get_running_loop()
            total_datasets = len(self.dataset_urls)

            worker_tasks = [
                asyncio.to_thread(
                    self._detailing_worker_thread,
                    worker_id=i,
                    work_queue=work_queue,
                    loop=loop,
                    total_datasets=total_datasets,
                )
                for i in range(1, concurrency + 1)
            ]

            await asyncio.gather(*worker_tasks)

            # Mark completed state upon processing all candidate URLs
            await self.save_progress(
                year=datetime.now().year,
                last_index=total_datasets,
                total_datasets=total_datasets,
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
        "--datasets",
        default=None,
        help="Comma-separated SAFLII dataset codes to scrape (e.g. ZACC,ZALCJHB). "
             "Omit to scrape all South African datasets.",
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

    datasets_list = None
    if args.datasets:
        datasets_list = [c.strip() for c in args.datasets.split(",") if c.strip()]

    scraper = SafliiScraper(
        pipeline_name=args.pipeline_name,
        datasets=datasets_list,
        year=args.year,
        headless=headless_value,
        use_xvfb=use_xvfb_value,
        skip_stages=args.skip_stages,
    )

    asyncio.run(scraper.run())
    sys.exit(0)