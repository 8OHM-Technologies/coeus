import argparse
import asyncio
import logging
import os
import queue
import random
import re
import sys
import urllib.parse
from datetime import datetime, date as dt_date
from typing import Any, Dict, List, Optional, Tuple

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

    if (
        t_lower in (
            "not found",
            "page not found",
            "404 not found",
            "404 - not found",
            "404 error",
        )
        or h_lower in (
            "not found",
            "page not found",
            "404 not found",
            "404 - not found",
            "404 error",
        )
        or "404" in t_lower
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
    SAFLII Case Scraper subclassing BaseScraper.
    Uses SeleniumBase UC mode with lxml parsing, multithreaded detailing workers,
    database record persistence, progress tracking, and proxy support.
    """

    def __init__(
        self,
        pipeline_name: str,
        court_code: Optional[str] = None,
        year: Optional[int] = None,
        headless: bool = False,
        use_xvfb: bool = True,
    ):
        super().__init__(pipeline_name)
        self.court_code: Optional[str] = court_code
        self.year: Optional[int] = year
        self.headless: bool = headless
        self.use_xvfb: bool = use_xvfb

        self.start_url: str = ""
        self.start_year: int = 2000
        self.end_year: int = datetime.now().year
        self.cooldown_seconds: float = 1.5
        self.take_debug_screenshots: bool = False
        self.screenshots_dir: str = ""

        self.case_urls: List[str] = []
        self.url_to_case_number: Dict[str, str] = {}
        self.db_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Hydrate configuration, resolve target IDs, DB connections, and progress state."""
        await super().initialize()

        self.start_url = self.config.get("start_url") or "https://www.saflii.org/za/cases/ZALCJHB/"
        extraction_params = self.config.get("extraction_params", {})
        
        if not self.court_code:
            self.court_code = extraction_params.get("court_code")
            if not self.court_code and self.start_url:
                match = re.search(r"/za/cases/([A-Za-z0-9]+)/", self.start_url)
                if match:
                    self.court_code = match.group(1)
        self.court_code = self.court_code or "ZALCJHB"

        # Resolve Year Range with Progress State Respect
        if self.year:
            self.start_year = int(self.year)
            self.end_year = int(self.year)
        else:
            cfg_start = extraction_params.get("start_year") or extraction_params.get("year")
            prog_last_year = self.progress_state.get("last_year")
            prog_completed = self.progress_state.get("last_completed", False)

            if cfg_start:
                try:
                    self.start_year = int(cfg_start)
                except (ValueError, TypeError):
                    self.start_year = 2000
            elif prog_last_year and isinstance(prog_last_year, int) and prog_last_year > 1900:
                self.start_year = prog_last_year + 1 if prog_completed else prog_last_year
            else:
                self.start_year = 2000

            cfg_end = extraction_params.get("end_year")
            if cfg_end:
                try:
                    self.end_year = int(cfg_end)
                except (ValueError, TypeError):
                    self.end_year = datetime.now().year
            else:
                self.end_year = datetime.now().year

        self.cooldown_seconds = float(extraction_params.get("cooldown_seconds", 1.5))
        self.take_debug_screenshots = self.config.get("take_debug_screenshots", False)
        
        if self.output_dir:
            self.screenshots_dir = os.path.join(os.path.dirname(self.output_dir), "screenshots")
            os.makedirs(self.screenshots_dir, exist_ok=True)

        logger.info("==================================================")
        logger.info(f"🚀 COEUS SAFLII WORKER (SeleniumBase UC Mode) ({self.pipeline_name})")
        logger.info(f"Target Court: {self.court_code} | Year Range: {self.start_year} - {self.end_year}")
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
        """Open a URL with single-window navigation and wait for target URL commit and page load."""
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

        # 2. Open URL via standard SeleniumBase open
        try:
            sb.open(url)
        except Exception as open_err:
            logger.warning(f"Standard open failed on {url} ({open_err}); attempting uc_open_with_reconnect...")
            try:
                sb.uc_open_with_reconnect(url, reconnect_time=2)
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

        if self.take_debug_screenshots and self.screenshots_dir:
            screenshot_path = os.path.join(self.screenshots_dir, f"{attempt_prefix}.png")
            try:
                sb.save_screenshot(screenshot_path)
            except Exception:
                pass

        title, h1, body = get_sb_page_signals(sb)
        state = check_page_state(title, h1, body)

        return state

    def _indexing_sync(self) -> Tuple[List[str], Dict[str, str]]:
        """Synchronous indexing task running inside a dedicated SB UC context thread."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        raw_case_urls = []
        url_to_case_map = {}

        indexing_profile = "/tmp/saflii_indexing_profile"
        with SB(
            uc=True,
            headless=self.headless,
            proxy=sb_proxy,
            test=True,
            xvfb=self.use_xvfb,
            user_data_dir=indexing_profile,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage"
        ) as sb:
            sb.set_window_size(1280, 720)
            
            # 1. Directly construct year directory URLs for the configured range (bypasses top-level redirect walls)
            logger.info(f"Constructing year directory indices for court '{self.court_code}' across years {self.start_year}..{self.end_year}")
            year_links = [
                (y, f"https://www.saflii.org/za/cases/{self.court_code}/{y}/")
                for y in range(self.start_year, self.end_year + 1)
            ]
            logger.info(f"Found {len(year_links)} years to process: {[y for y, _ in year_links]}")

            # 2. Extract Document URLs from Year Directories
            for year, year_url in year_links:
                logger.info(f"Processing structural year context directory: {year} -> {year_url}")
                found_for_year = 0
                for attempt in range(1, 4):
                    try:
                        state = self._navigate_and_handle_turnstile(sb, year_url, f"year_{year}_attempt_{attempt}")
                        if state == "BLOCKED":
                            logger.warning(f"Year directory {year} blocked on attempt {attempt}/3.")
                            if attempt < 3:
                                sb.sleep(attempt * 4)
                                continue
                            else:
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
                                if parent_dir.isdigit() and len(parent_dir) == 4 and filename.endswith(".html"):
                                    if self.start_year <= int(parent_dir) <= self.end_year:
                                        if not ("toc-" in filename or filename == "index.html"):
                                            case_no = extract_case_number_from_text(a_tag.get_text(strip=True))
                                            if case_no:
                                                url_to_case_map[abs_url] = case_no
                                            raw_case_urls.append(abs_url)
                                            found_for_year += 1
                        logger.info(f"Year {year}: Harvested {found_for_year} candidate case URLs.")
                        break
                    except Exception as err:
                        logger.warning(f"Error isolating index structures for year {year} [Attempt {attempt}]: {err}")
                        if attempt < 3:
                            sb.sleep(attempt * 4)

        return sorted(list(set(raw_case_urls))), url_to_case_map

    async def indexing(self) -> None:
        """Sub-process A: Harvest case entry indexes matching timeframe distribution constraints."""
        if self.start_year > self.end_year:
            logger.info(f"Start year {self.start_year} exceeds end year {self.end_year}. Pipeline already complete.")
            self.case_urls = []
            return

        logger.info(f"[Stage 1A start] Starting indexing stage via SeleniumBase UC thread...")
        self.case_urls, self.url_to_case_number = await asyncio.to_thread(self._indexing_sync)
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
        logger.info("[Stage 1B start] Starting detailing stage via SeleniumBase UC thread...")
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        logger.info(f"[Worker {worker_id}] Initializing SeleniumBase UC browser session...")

        worker_profile = f"/tmp/saflii_worker_profile_{worker_id}"
        with SB(
            uc=True,
            headless=self.headless,
            proxy=sb_proxy,
            test=True,
            xvfb=self.use_xvfb,
            user_data_dir=worker_profile,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage"
        ) as sb:
            sb.set_window_size(1280, 720)

            while True:
                try:
                    item = work_queue.get_nowait()
                except queue.Empty:
                    break

                if len(item) == 3:
                    idx, case_url, pass_number = item
                else:
                    idx, case_url = item
                    pass_number = 1

                c_court, c_year, c_id = parse_case_url(case_url, default_court=self.court_code or "SAFLII")
                case_no = self.url_to_case_number.get(case_url)

                if case_url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                    logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Skipping pre-existing record: {case_url}")
                    work_queue.task_done()
                    continue

                logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Detailed enrichment active [Pass {pass_number}/3] -> {case_url}")
                success = False

                for attempt in range(1, 4):
                    try:
                        state = self._navigate_and_handle_turnstile(
                            sb, case_url, f"worker_{worker_id}_case_{c_id}_pass_{pass_number}_att_{attempt}"
                        )
                        if state == "BLOCKED":
                            raise BlockedException(f"Turnstile block on asset: {c_id}")
                        elif state == "NOT_FOUND":
                            raise Exception(f"Resource missing (state: {state})")
                        elif state == "NAVIGATION_FAILED":
                            raise Exception(f"Browser stuck on previous page, failed to navigate to target URL '{case_url}'")

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
                            "center_content": center_html,
                            "full_text": center_div.get_text(separator="\n", strip=True) if center_div else "",
                            "scraped_at": datetime.now().isoformat(),
                            "worker_id": worker_id,
                        }

                        try:
                            doc_date = dt_date(int(c_year), 1, 1)
                        except Exception:
                            doc_date = dt_date.today()

                        # Dispatch async DB save
                        future = asyncio.run_coroutine_threadsafe(
                            self._save_record_to_db(case_url, record, doc_date),
                            loop
                        )
                        future.result()

                        # Dispatch async progress state save
                        current_y = int(c_year) if c_year.isdigit() else self.start_year
                        asyncio.run_coroutine_threadsafe(
                            self.save_progress(
                                year=current_y,
                                court_code=self.court_code,
                                last_index=idx,
                                total_cases=total_cases,
                                last_url=case_url,
                                completed=False,
                            ),
                            loop
                        )

                        self.existing_urls.add(case_url)
                        if case_no:
                            self.existing_case_numbers.add(case_no)

                        logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] [+] Saved record: {c_court}_{c_year}_{c_id}")
                        success = True
                        break

                    except BlockedException as be:
                        logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] Blocked on case page {case_url} (Pass {pass_number}, attempt {attempt}/3): {be}")
                        if attempt < 3:
                            sb.sleep(attempt * 4)
                    except Exception as err:
                        err_msg = str(err)
                        logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] Processing error on case {c_id} [Pass {pass_number}, attempt {attempt}]: {err}")
                        if any(w in err_msg.lower() for w in ("no such window", "target window already closed", "web view not found", "invalid session id", "connection refused")):
                            logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Window or session connection disrupted. Recovering window handle...")
                            try:
                                handles = sb.driver.window_handles
                                if handles:
                                    sb.driver.switch_to.window(handles[0])
                                else:
                                    sb.open("about:blank")
                            except Exception:
                                try:
                                    sb.uc_open_with_reconnect("about:blank", reconnect_time=2)
                                except Exception:
                                    pass
                        if attempt < 3:
                            sb.sleep(attempt * 3)

                if success:
                    sb.sleep(self.cooldown_seconds + random.uniform(0.3, 0.9))
                else:
                    if pass_number < 3:
                        logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] ⚠️ Case {c_id} ({case_url}) failed all attempts on Pass {pass_number}/3. Re-queueing for retry pass {pass_number + 1}...")
                        work_queue.put((idx, case_url, pass_number + 1))
                        sb.sleep(2)
                    else:
                        logger.error(f"[Worker {worker_id}][{idx}/{total_cases}] ❌ Case {c_id} ({case_url}) permanently failed after 3 retry passes.")

                work_queue.task_done()

        logger.info(f"[Worker {worker_id}] Thread execution complete.")

    async def detailing(self) -> None:
        """Sub-process B: Perform deep enrichment processing using concurrent SB UC worker threads."""
        if not self.case_urls:
            logger.info("No indices staged for detailed processing pipelines.")
            return

        extraction_params = self.config.get("extraction_params", {})
        concurrency = int(extraction_params.get("concurrency", 4))
        logger.info(f"[Stage 1B start] Starting detailing with {concurrency} SeleniumBase UC worker threads...")

        work_queue = queue.Queue()
        for idx, case_url in enumerate(self.case_urls, start=1):
            work_queue.put((idx, case_url))

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
            year=self.end_year,
            court_code=self.court_code,
            last_index=total_cases,
            total_cases=total_cases,
            completed=True,
        )
        logger.info("[Stage 1B complete] ✅ Scraping execution layer completely processed.")

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
        "--court_code",
        default=None,
        help="SAFLII Court Code (e.g. ZALCJHB)",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Target year (e.g. 2026)",
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
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    use_xvfb_value = args.use_xvfb.lower() == "true"

    scraper = SafliiScraper(
        pipeline_name=args.pipeline_name,
        court_code=args.court_code,
        year=args.year,
        headless=headless_value,
        use_xvfb=use_xvfb_value,
    )
    
    asyncio.run(scraper.run())