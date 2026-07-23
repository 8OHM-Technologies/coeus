import asyncio
import os
import queue
import re
import sys
import urllib.parse
from datetime import datetime, date as dt_date
from typing import Optional

from bs4 import BeautifulSoup
from seleniumbase import SB

from base_scraper import BaseScraper, setup_logger
import db_storage

logger = setup_logger(__name__)


class BlockedException(Exception):
    """Raised when a Cloudflare/Turnstile verification wall cannot be breached."""
    pass


# ---------------------------------------------------------------------------
# Pure Utility Helpers
# ---------------------------------------------------------------------------

def format_proxy_for_sb(proxy_url: Optional[str]) -> Optional[str]:
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


def check_page_state(page_title: str, h1_title: str, body_text: str) -> str:
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
    ):
        return "NOT_FOUND"

    return "OK"


def get_sb_page_signals(sb: SB) -> tuple[str, str, str]:
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


def parse_case_url(case_url: str, default_court: str = "SAFLII") -> tuple[str, str, str]:
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


# ---------------------------------------------------------------------------
# Core Framework Implementation
# ---------------------------------------------------------------------------

class SafliiScraper(BaseScraper):
    
    def __init__(self, pipeline_name: str, headless: bool = False):
        super().__init__(pipeline_name)
        self.headless = headless
        
        self.start_url: str = ""
        self.start_year: int = 2000
        self.end_year: int = datetime.now().year
        self.cooldown_seconds: float = 1.5
        self.take_debug_screenshots: bool = False
        self.screenshots_dir: str = ""
        self.use_proxy: bool = False
        self.proxy_url: Optional[str] = None
        
        self.case_urls: list[str] = []
        self.url_to_case_number: dict[str, str] = {}

        self.db_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Hydrate runtime variables, baseline tracking sets, and directory structures."""
        await super().initialize()
        
        self.start_url = self.config.get("start_url")
        if not self.start_url:
            logger.error("No start_url configured in pipeline parameters.")
            sys.exit(1)

        extraction_params = self.config.get("extraction_params", {})
        self.start_year = int(extraction_params.get("start_year", 2000))
        self.end_year = int(extraction_params.get("end_year", datetime.now().year))
        self.cooldown_seconds = float(extraction_params.get("cooldown_seconds", 1.5))
        
        self.take_debug_screenshots = self.config.get("take_debug_screenshots", False)
        self.screenshots_dir = os.path.join(os.path.dirname(self.output_dir), "screenshots")
        os.makedirs(self.screenshots_dir, exist_ok=True)
        
        self.use_proxy = self.config.get("use_proxy", False)
        self.proxy_url = self.config.get("proxy_url")
        
        logger.info("==================================================")
        logger.info(f"🚀 COEUS SAFLII WORKER (SeleniumBase UC Mode) ({self.pipeline_name})")
        logger.info(f"Target Year Context Range Bounds: {self.start_year} - {self.end_year}")
        logger.info("==================================================")

    async def authenticate(self, headless: bool = False) -> None:
        """SAFLII bypasses login constraints via inline Cloudflare tokens. No-op implementation."""
        pass

    def _navigate_and_handle_turnstile(
        self,
        sb: SB,
        url: str,
        attempt_prefix: str,
    ) -> str:
        """Open a URL with SeleniumBase UC reconnect and handle Turnstile challenges."""
        logger.info(f"Navigating with UC reconnect to: {url} [{attempt_prefix}]")
        sb.uc_open_with_reconnect(url, reconnect_time=2)

        try:
            sb.uc_gui_handle_captcha()
        except Exception as captcha_err:
            logger.debug(f"Captcha auto-handler check finished: {captcha_err}")

        title, h1, body = get_sb_page_signals(sb)
        state = check_page_state(title, h1, body)

        if self.take_debug_screenshots:
            screenshot_path = os.path.join(self.screenshots_dir, f"{attempt_prefix}.png")
            try:
                sb.save_screenshot(screenshot_path)
            except Exception:
                pass

        return state

    def _indexing_sync(self) -> tuple[list[str], dict[str, str]]:
        """Synchronous indexing task running inside a dedicated SB UC context thread."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        raw_case_urls = []
        url_to_case_map = {}

        with SB(uc=True, headless=self.headless, proxy=sb_proxy, test=True) as sb:
            sb.set_window_size(1280, 720)
            
            # 1. Gather Year Links
            logger.info(f"Navigating to index start: {self.start_url}")
            year_links = []
            start_success = False

            for attempt in range(1, 4):
                try:
                    state = self._navigate_and_handle_turnstile(sb, self.start_url, f"start_url_attempt_{attempt}")
                    if state == "BLOCKED":
                        raise BlockedException("Cloudflare clearance execution timed out at index node context.")
                    elif state == "NOT_FOUND":
                        raise Exception(f"Anchor endpoint missing or unreachable. State evaluated: {state}")

                    soup = BeautifulSoup(sb.get_page_source(), "lxml")
                    year_pattern = re.compile(r"^\d{4}$")
                    
                    for a_tag in soup.find_all("a"):
                        href = a_tag.get("href")
                        text = a_tag.get_text(strip=True)
                        if href and year_pattern.match(text):
                            year = int(text)
                            if self.start_year <= year <= self.end_year:
                                abs_url = urllib.parse.urljoin(self.start_url, href)
                                year_links.append((year, abs_url))

                    if year_links:
                        year_links = sorted(list(set(year_links)), key=lambda x: x[0])
                        start_success = True
                        break
                    else:
                        raise Exception("No year index nodes isolated from element structures.")
                except Exception as err:
                    logger.warning(f"Index access pass {attempt}/3 obstructed: {err}")
                    if attempt < 3:
                        sb.sleep(attempt * 5)

            if not start_success:
                logger.error("Failed to verify structural clearance benchmarks for base tracking arrays.")
                sys.exit(1)

            logger.info(f"Found {len(year_links)} years to process: {[y for y, _ in year_links]}")

            # 2. Extract Document URLs from Year Directories
            for year, year_url in year_links:
                logger.info(f"Processing structural year context directory: {year} -> {year_url}")
                for attempt in range(1, 4):
                    try:
                        state = self._navigate_and_handle_turnstile(sb, year_url, f"year_{year}_attempt_{attempt}")
                        if state == "BLOCKED":
                            raise BlockedException(f"Resource locks applied on year directory {year}.")
                        elif state == "NOT_FOUND":
                            raise Exception(f"Target index {year_url} missing or returned error (state: {state}).")

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
                        break
                    except Exception as err:
                        logger.warning(f"Error isolating index structures for year {year} [Attempt {attempt}]: {err}")
                        if attempt < 3:
                            sb.sleep(attempt * 5)

        return sorted(list(set(raw_case_urls))), url_to_case_map

    async def indexing(self) -> None:
        """Sub-process A: Harvest case entry indexes matching timeframe distribution constraints."""
        logger.info("Starting indexing stage via SeleniumBase UC thread...")
        self.case_urls, self.url_to_case_number = await asyncio.to_thread(self._indexing_sync)
        logger.info(f"Sub-process A complete. Total downstream asset indexes harvested: {len(self.case_urls)}")

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
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        logger.info(f"[Worker {worker_id}] Initializing SeleniumBase UC browser session...")

        with SB(uc=True, headless=self.headless, proxy=sb_proxy, test=True) as sb:
            sb.set_window_size(1280, 720)

            while True:
                try:
                    item = work_queue.get_nowait()
                except queue.Empty:
                    break

                idx, case_url = item
                c_court, c_year, c_id = parse_case_url(case_url)
                case_no = self.url_to_case_number.get(case_url)

                # Pre-existing check against in-memory deduplication sets
                if case_url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                    logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Skipping pre-existing record: {case_url}")
                    work_queue.task_done()
                    continue

                logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Detailed enrichment active -> {case_url}")
                success = False

                for attempt in range(1, 4):
                    try:
                        state = self._navigate_and_handle_turnstile(sb, case_url, f"worker_{worker_id}_case_{c_id}_att_{attempt}")
                        if state == "BLOCKED":
                            raise BlockedException(f"Turnstile block on asset: {c_id}")
                        elif state == "NOT_FOUND":
                            raise Exception(f"Resource missing (state: {state})")

                        soup = BeautifulSoup(sb.get_page_source(), "lxml")
                        center_div = soup.find("div", id="center")
                        center_html = str(center_div) if center_div else ""

                        h2_el = center_div.find("h2") if center_div else None
                        title = h2_el.get_text(strip=True) if h2_el else sb.get_page_title()

                        if not case_no:
                            case_no = extract_case_number_from_text(title)

                        if case_no and case_no in self.existing_case_numbers:
                            logger.info(f"[Worker {worker_id}] Duplicate signature isolated via late mapping: {case_no}")
                            success = True
                            break

                        record = {
                            "court": c_court,
                            "year": c_year,
                            "case_id": c_id,
                            "title": title,
                            "url": case_url,
                            "center_content": center_html,
                            "scraped_at": datetime.now().isoformat(),
                            "worker_id": worker_id,
                        }

                        try:
                            doc_date = dt_date(int(c_year), 1, 1)
                        except Exception:
                            doc_date = dt_date.today()

                        # Dispatch async DB save back to the main event loop
                        future = asyncio.run_coroutine_threadsafe(
                            self._save_record_to_db(case_url, record, doc_date),
                            loop
                        )
                        future.result()  # Wait for DB transaction to finish

                        self.existing_urls.add(case_url)
                        if case_no:
                            self.existing_case_numbers.add(case_no)

                        logger.info(f"[Worker {worker_id}] [+] Saved record: {c_court}_{c_year}_{c_id}")
                        success = True
                        break

                    except BlockedException as be:
                        logger.warning(f"[Worker {worker_id}] Blocked on case page {case_url} (attempt {attempt}/3): {be}")
                        if attempt < 3:
                            sb.sleep(attempt * 4)
                    except Exception as err:
                        logger.warning(f"[Worker {worker_id}] Processing error on case {c_id} [Attempt {attempt}]: {err}")
                        if attempt < 3:
                            sb.sleep(attempt * 4)

                if success:
                    sb.sleep(self.cooldown_seconds)

                work_queue.task_done()

        logger.info(f"[Worker {worker_id}] Thread execution complete.")

    async def detailing(self) -> None:
        """Sub-process B: Perform deep enrichment processing using concurrent SB UC worker threads."""
        if not self.case_urls:
            logger.info("No remote indices staged for detailed processing pipelines.")
            return

        extraction_params = self.config.get("extraction_params", {})
        concurrency = int(extraction_params.get("concurrency", 4))
        logger.info(f"Starting detailing with {concurrency} SeleniumBase UC worker threads...")

        work_queue = queue.Queue()
        for idx, case_url in enumerate(self.case_urls, start=1):
            work_queue.put((idx, case_url))

        loop = asyncio.get_running_loop()
        total_cases = len(self.case_urls)

        # Launch worker threads
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
        logger.info("✅ Scraping execution layer completely processed.")

    async def extraction(self) -> None:
        """Stage 2: Post-processing and extraction metrics verification."""
        logger.info("Stage 2: Post-Scraping Data Extraction verification processes complete.")


# ---------------------------------------------------------------------------
# Runner Script Execution Pattern
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Coeus SAFLII Scraper (SeleniumBase UC Driver)")
    parser.add_argument(
        "--pipeline", "--pipeline_name", 
        required=True, 
        dest="pipeline_name",
        help="Target pipeline setup configuration key matching environment presets"
    )
    parser.add_argument(
        "--headless", 
        default="false", 
        help="Orchestrate worker browser processes via headless mode (true/false)"
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    scraper = SafliiScraper(pipeline_name=args.pipeline_name)
    
    asyncio.run(scraper.run())