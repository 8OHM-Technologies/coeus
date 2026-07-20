import asyncio
import os
import re
import sys
import urllib.parse
from datetime import datetime, date as dt_date

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

from base_scraper import BaseScraper, setup_logger
import db_storage
from misstcha.turnstile import TurnstileSolver
from utils.debug_helper import log_browser_proxy_ip
from utils.utils import take_screenshot

logger = setup_logger(__name__)
turnstile_solver = TurnstileSolver()


class BlockedException(Exception):
    """Raised when a Cloudflare/Turnstile verification wall cannot be breached."""
    pass


# ---------------------------------------------------------------------------
# Pure Utility Helpers
# ---------------------------------------------------------------------------

def check_page_state(page_title: str, h1_title: str, body_text: str) -> str:
    """Determine the logical state of the page to detect blocks or dead links.

    Args:
        page_title: The page's <title> text.
        h1_title: The page's first <h1> text.
        body_text: The page's <body> inner text.

    Returns:
        "BLOCKED": if it's a Cloudflare/Turnstile challenge page.
        "NOT_FOUND": if it's a 404/403 or similar error page.
        "OK": if it's a valid content page.
    """
    t_lower = (page_title or "").lower().strip()
    h_lower = (h1_title or "").lower().strip()
    b_lower = (body_text or "").lower().strip()

    if (
        "just a moment" in t_lower
        or "cloudflare" in t_lower
        or "security verification" in t_lower
        or "verify you are human" in b_lower
        or "turnstile" in b_lower
    ):
        return "BLOCKED"

    if (
        t_lower
        in (
            "not found",
            "page not found",
            "404 not found",
            "404 - not found",
            "404 error",
            "403 forbidden",
            "forbidden",
        )
        or h_lower
        in (
            "not found",
            "page not found",
            "404 not found",
            "404 - not found",
            "404 error",
            "403 forbidden",
            "forbidden",
        )
        or "you don't have permission to access this resource" in b_lower
    ):
        return "NOT_FOUND"

    return "OK"


async def get_page_signals(page: Page) -> tuple[str, str, str]:
    """Extract the title, h1, and body text from the current page for state checks."""
    title = await page.title()
    try:
        h1 = (
            await page.locator("h1").first.inner_text()
            if await page.locator("h1").count() > 0
            else ""
        )
    except Exception:
        h1 = ""
    try:
        body = (
            await page.locator("body").inner_text()
            if await page.locator("body").count() > 0
            else ""
        )
    except Exception:
        body = ""
    return title, h1, body


async def wait_for_page_load(page: Page, url_type: str = "page") -> str:
    """Wait for the page to settle after a Turnstile challenge or navigation.

    Polls for up to 30 seconds, checking both the page state and content-specific
    signals to confirm the real page has loaded.

    Args:
        page: The Playwright page instance.
        url_type: One of "start", "year", or "case" to check for specific content.

    Returns:
        "OK", "NOT_FOUND", or "TIMEOUT".
    """
    year_pattern = re.compile(r"^\d{4}$")
    for poll_sec in range(30):
        await asyncio.sleep(1)
        try:
            title, h1, body = await get_page_signals(page)
        except Exception:
            continue

        state = check_page_state(title, h1, body)
        if state == "NOT_FOUND":
            return "NOT_FOUND"
        if state == "BLOCKED":
            continue

        # Page is in OK state — verify the expected content is actually present
        if url_type == "start":
            anchors = await page.locator("a").all()
            for a in anchors:
                try:
                    text = (await a.inner_text()).strip()
                    if year_pattern.match(text):
                        return "OK"
                except Exception:
                    pass
        elif url_type == "year":
            anchors = await page.locator("a").all()
            for a in anchors:
                try:
                    href = await a.get_attribute("href")
                    if href and href.endswith(".html") and not ("toc-" in href or "index.html" in href):
                        return "OK"
                except Exception:
                    pass
        elif url_type == "case":
            if len(body.strip()) > 500:
                return "OK"
        else:
            if not await turnstile_solver.find_turnstile_frame(page):
                return "OK"

    return "TIMEOUT"


def parse_case_url(case_url: str, default_court: str = "SAFLII") -> tuple[str, str, str]:
    """Parse a valid SAFLII asset path to map structural parameters."""
    parsed = urllib.parse.urlparse(case_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3:
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 4:
                return parts[i - 1], parts[i], os.path.splitext(parts[i + 1])[0]
    return default_court, "unknown", "unknown"


def extract_case_number_from_text(text: str) -> str | None:
    """Extracts standard SAFLII case numbers or formal citations from strings."""
    if not text:
        return None
    # Matches: (123/2025) or [2026] ZACC 4
    match = re.search(r'\((?:\w+\s+)?\d+/\d+\)|\[\d{4}\]\s+\w+\s+\d+', text)
    if match:
        return match.group(0).strip()

    # Simple fallback for standard number/year slashes
    fallback_match = re.search(r'\b\d+/\d+\b', text)
    if fallback_match:
        return fallback_match.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Core Framework Implementation
# ---------------------------------------------------------------------------

class SafliiScraper(BaseScraper):
    
    def __init__(self, pipeline_name: str, headless: bool = False):
        super().__init__(pipeline_name)
        self.headless = headless
        
        # Configuration parameters resolved during initialization
        self.start_url: str = ""
        self.start_year: int = 2000
        self.end_year: int = datetime.now().year
        self.cooldown_seconds: float = 1.5
        self.take_debug_screenshots: bool = False
        self.screenshots_dir: str = ""
        self.use_proxy: bool = False
        self.proxy_url: str | None = None
        
        # Dynamic operational memory state across sub-processes
        self.case_urls: list[str] = []
        self.url_to_case_number: dict[str, str] = {}

        # Concurrency control locks
        self.db_lock = asyncio.Lock()
        self.turnstile_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Hydrate runtime variables, baseline tracking sets, and directory structures."""
        # Hydrates self.conn, self.target_id, self.existing_urls, self.existing_case_numbers, and self.progress_state
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
        
        # Hydrate internal proxies if present in config maps
        self.use_proxy = self.config.get("use_proxy", False)
        self.proxy_url = self.config.get("proxy_url")
        
        logger.info("==================================================")
        logger.info(f"🚀 COEUS NEW SAFLII WORKER RUNNING ({self.pipeline_name})")
        logger.info(f"Target Year Context Range Bounds: {self.start_year} - {self.end_year}")
        logger.info("==================================================")

    async def authenticate(self, headless: bool = False) -> None:
        """SAFLII bypasses login constraints via inline Cloudflare tokens. No-op implementation."""
        pass

    async def handle_turnstile_challenge(self, page: Page, attempt_prefix: str, url_type: str = "page") -> str:
        """Detect, solve, and verify Cloudflare Turnstile challenges.

        Uses the proven pattern: detect via targeted DOM signals → solve via
        click → wait for actual page content to load.

        Args:
            page: The Playwright page instance.
            attempt_prefix: A label for log messages (e.g. "start_url_attempt_1").
            url_type: One of "start", "year", or "case" — passed to wait_for_page_load.

        Returns:
            The page state after resolution: "OK", "BLOCKED", or "NOT_FOUND".
        """
        title, h1, body = await get_page_signals(page)
        state = check_page_state(title, h1, body)

        if state == "BLOCKED":
            async with self.turnstile_lock:
                # Reload the page to check if another worker already cleared the Turnstile challenge
                logger.info(f"Checking Turnstile cookie sharing via reload for [{attempt_prefix}]...")
                try:
                    await page.reload(timeout=15000)
                except Exception:
                    pass
                title, h1, body = await get_page_signals(page)
                state = check_page_state(title, h1, body)
                if state != "BLOCKED":
                    logger.info(f"✅ Bypassed Turnstile challenge for [{attempt_prefix}] via shared session cookies.")
                    return state

                logger.info(f"⚠️ Captcha challenge detected during [{attempt_prefix}]. Invoking Solver...")
                solve_res = await turnstile_solver.solve(page, screenshot_dir=self.screenshots_dir)

                if solve_res["success"]:
                    load_result = await wait_for_page_load(page, url_type)
                    if self.take_debug_screenshots:
                        await take_screenshot(page, self.screenshots_dir, f"{attempt_prefix}_post_solve")

                    if load_result == "TIMEOUT":
                        logger.warning(f"Post-solve page load timed out for [{attempt_prefix}].")
                        # Re-check state — it may have partially loaded
                        title, h1, body = await get_page_signals(page)
                        state = check_page_state(title, h1, body)
                    else:
                        state = load_result  # "OK" or "NOT_FOUND"
                else:
                    logger.warning(f"Solver failed for [{attempt_prefix}]: {solve_res.get('error', 'unknown')}")
                    await asyncio.sleep(2)
                    # Re-check state in case it cleared anyway
                    title, h1, body = await get_page_signals(page)
                    state = check_page_state(title, h1, body)

        return state

    async def indexing(self) -> None:
        """Sub-process A: Harvest case entry indexes matching timeframe distribution constraints."""
        self.playwright_instance = await async_playwright().start()
        
        from utils.browser_helper import BrowserManager
        self.browser_manager = BrowserManager(
            self.playwright_instance,
            headless=self.headless,
            ignore_https_errors=self.config.get("allow_insecure_requests", False),
            proxy_url=self.proxy_url,
            viewport={"width": 1280, "height": 720},
        )
        
        page = await self.browser_manager.start()
        await log_browser_proxy_ip(page, "Startup", self.use_proxy)

        # 1. Gather Year Links
        logger.info(f"Navigating to operational index anchor: {self.start_url}")
        start_success = False
        year_links = []
        
        for attempt in range(1, 6):
            try:
                await page.goto(self.start_url, timeout=30000)
                if self.take_debug_screenshots:
                    await take_screenshot(page, self.screenshots_dir, f"start_url_attempt_{attempt}_navigated")

                state = await self.handle_turnstile_challenge(page, f"start_url_attempt_{attempt}", url_type="start")
                if state == "BLOCKED":
                    raise BlockedException("Cloudflare clearance execution timed out at index node context.")
                elif state == "NOT_FOUND":
                    raise Exception(f"Anchor endpoint missing or unreachable. State evaluated: {state}")

                year_pattern = re.compile(r"^\d{4}$")
                anchors = await page.locator("a").all()
                for anchor in anchors:
                    href = await anchor.get_attribute("href")
                    text = (await anchor.inner_text()).strip()
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
                logger.warning(f"Index access pass {attempt}/5 obstructed: {err}")
                if self.take_debug_screenshots:
                    await take_screenshot(page, self.screenshots_dir, f"start_url_attempt_{attempt}_error")
                if attempt < 5:
                    await asyncio.sleep(attempt * 5)

        if not start_success:
            logger.error("Failed to verify structural clearance benchmarks for base tracking arrays.")
            sys.exit(1)

        logger.info(f"Found {len(year_links)} years to process: {[y for y, _ in year_links]}")

        # 2. Iterate through index vectors to isolate documents URLs
        raw_case_urls = []
        for year, year_url in year_links:
            logger.info(f"Processing structural year context directory: {year} -> {year_url}")
            await log_browser_proxy_ip(page, f"Year {year}", self.use_proxy)

            for attempt in range(1, 6):
                try:
                    await page.goto(year_url, timeout=30000)
                    if self.take_debug_screenshots:
                        await take_screenshot(page, self.screenshots_dir, f"year_{year}_attempt_{attempt}_navigated")

                    state = await self.handle_turnstile_challenge(page, f"year_{year}_attempt_{attempt}", url_type="year")
                    if state == "BLOCKED":
                        raise BlockedException(f"Resource locks applied on year directory {year}.")
                    elif state == "NOT_FOUND":
                        logger.warning(f"Target index {year_url} missing (404/403). Dropping step context.")
                        break

                    y_anchors = await page.locator("a").all()
                    for y_anchor in y_anchors:
                        href = await y_anchor.get_attribute("href")
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
                                        text = await y_anchor.inner_text()
                                        case_no = extract_case_number_from_text(text)
                                        if case_no:
                                            self.url_to_case_number[abs_url] = case_no
                                        raw_case_urls.append(abs_url)
                    break
                except Exception as err:
                    logger.warning(f"Error encountered isolating index structures for year {year} [Attempt {attempt}]: {err}")
                    if self.take_debug_screenshots:
                        await take_screenshot(page, self.screenshots_dir, f"year_{year}_attempt_{attempt}_error")
                    if attempt == 5:
                        logger.error(f"Terminated tracking bounds processing loops context for timeframe: {year}")
                    else:
                        await asyncio.sleep(attempt * 10)

        self.case_urls = sorted(list(set(raw_case_urls)))
        logger.info(f"Sub-process A complete. Total downstream asset indexes harvested: {len(self.case_urls)}")

    async def detailing(self) -> None:
        """Sub-process B: Perform deep enrichment processing on targeted file links."""
        if not self.case_urls:
            logger.info("No remote indices staged for detailed processing pipelines.")
            return

        extraction_params = self.config.get("extraction_params", {})
        concurrency = int(extraction_params.get("concurrency", 8))
        logger.info(f"Starting concurrent detailing with {concurrency} workers...")

        # Close the initial indexing page to free resources
        if self.browser_manager.page:
            try:
                await self.browser_manager.page.close()
            except Exception:
                pass

        queue = asyncio.Queue()
        for idx, case_url in enumerate(self.case_urls, start=1):
            await queue.put((idx, case_url))

        total_new = 0

        async def worker(worker_id: int):
            nonlocal total_new
            logger.info(f"Worker {worker_id} started.")
            
            # Create a dedicated page for this worker under the shared context
            page = await self.browser_manager.context.new_page()
            
            try:
                while not queue.empty():
                    try:
                        idx, case_url = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    c_court, c_year, c_id = parse_case_url(case_url)
                    case_no = self.url_to_case_number.get(case_url)

                    # Check against deduplication sets (read/write is safe in single-threaded event loop)
                    if case_url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                        logger.info(f"[Worker {worker_id}][{idx}/{len(self.case_urls)}] Skipping record entry (Pre-existing validation signature matches): {case_url}")
                        queue.task_done()
                        continue

                    logger.info(f"[Worker {worker_id}][{idx}/{len(self.case_urls)}] Detailed enrichment active -> {case_url}")
                    success = False

                    for attempt in range(1, 6):
                        try:
                            await page.goto(case_url, timeout=30000)
                            if self.take_debug_screenshots:
                                await take_screenshot(page, self.screenshots_dir, f"case_{c_id}_attempt_{attempt}_navigated")

                            state = await self.handle_turnstile_challenge(page, f"case_{c_id}_attempt_{attempt}", url_type="case")
                            if state == "BLOCKED":
                                raise BlockedException(f"Challenge wall containment failure active on asset element: {c_id}")
                            elif state == "NOT_FOUND":
                                logger.warning(f"[Worker {worker_id}] Target payload resource {case_url} not found (404/403). Dropping link tracking.")
                                success = True
                                break

                            html_content = await page.content()
                            soup = BeautifulSoup(html_content, "lxml")
                            center_div = soup.find("div", id="center")
                            center_html = str(center_div) if center_div else ""

                            h2_el = center_div.find("h2") if center_div else None
                            title = h2_el.get_text(strip=True) if h2_el else await page.title()

                            if not case_no:
                                case_no = extract_case_number_from_text(title)

                            if case_no and case_no in self.existing_case_numbers:
                                logger.info(f"[Worker {worker_id}]  [~] Duplicate signature isolated via late mapping step for identifier: {case_no}")
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
                            }

                            try:
                                doc_date = dt_date(int(c_year), 1, 1)
                            except Exception:
                                doc_date = dt_date.today()

                            # Push safely formatted record via database helper, protected by db_lock
                            async with self.db_lock:
                                await db_storage.upsert_scraped_record(
                                    self.conn, self.target_id, self.pipeline_name, case_url, record, doc_date, status="detailed"
                                )
                            
                            self.existing_urls.add(case_url)
                            if case_no:
                                self.existing_case_numbers.add(case_no)
                            
                            total_new += 1
                            logger.info(f"[Worker {worker_id}]  [+] Unified structural database record written: {c_court}_{c_year}_{c_id}")
                            success = True
                            break

                        except BlockedException as be:
                            logger.warning(f"[Worker {worker_id}] Blocked on case page {case_url} (attempt {attempt}/5): {be}")
                            if self.take_debug_screenshots:
                                await take_screenshot(page, self.screenshots_dir, f"case_{c_id}_attempt_{attempt}_blocked_error")
                            if attempt < 5:
                                await asyncio.sleep(attempt * 5)
                        except Exception as err:
                            logger.warning(f"[Worker {worker_id}] Enrichment payload extraction error on asset {c_id} [Attempt {attempt}]: {err}")
                            if self.take_debug_screenshots:
                                await take_screenshot(page, self.screenshots_dir, f"case_{c_id}_attempt_{attempt}_error")
                            if attempt < 5:
                                await asyncio.sleep(attempt * 5)

                    if success:
                        await asyncio.sleep(self.cooldown_seconds)
                    queue.task_done()
            finally:
                await page.close()
                logger.info(f"Worker {worker_id} stopped.")

        # Run workers concurrently
        workers = [asyncio.create_task(worker(i)) for i in range(1, concurrency + 1)]
        await queue.join()
        
        # Cancel any idle worker tasks (should already be done, but to be safe)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(f"✅ Scraping execution layer completely processed. Staged transactional commits: {total_new}")

    async def extraction(self) -> None:
        """Stage 2: Final post-processing, validation transformations, or metrics logging operations."""
        logger.info("Stage 2: Post-Scraping Data Extraction verification processes complete.")


# ---------------------------------------------------------------------------
# Runner Script Execution Pattern
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Coeus New SAFLII Scraper Refactored Instance")
    parser.add_argument(
        "--pipeline", "--pipeline_name", 
        required=True, 
        dest="pipeline_name",
        help="Target pipeline setup configuration key matching environment presets"
    )
    parser.add_argument(
        "--headless", 
        default="false", 
        help="Orchestrate worker browser processes via headless virtual framing parameters (true/false)"
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    scraper = SafliiScraper(pipeline_name=args.pipeline_name, headless=headless_value)
    
    asyncio.run(scraper.run())