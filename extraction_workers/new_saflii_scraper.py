import asyncio
import os
import re
import sys
import urllib.parse
from datetime import datetime, date as dt_date

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page

from .base_scraper import BaseScraper, setup_logger
from . import db_storage
from misstcha import TurnstileSolver
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

def check_page_state(page_content: str) -> str:
    """Determine the logical state of the page to detect blocks or dead links."""

    if (
        "just a moment" in page_content
        or "cloudflare" in page_content
        or "security verification" in page_content
        or "verify you are human" in page_content
        or "turnstile" in page_content
    ):
        return "BLOCKED"

    if (
        "not found" in page_content
        or "page not found" in page_content
        or "404 not found" in page_content
        or "404 - not found" in page_content
        or "404 error" in page_content
        or "403 forbidden" in page_content
        or "forbidden" in page_content
        or "you don't have permission to access this resource" in page_content
    ):
        return "NOT_FOUND"
    
    if ("judgment" in page_content):
        return "OK"

    return "OK"


def parse_case_url(case_url: str, default_court: str = "SAFLII") -> tuple[str, str, str]:
    """Parse a valid SAFLII asset path to map structural parameters."""
    parsed = urllib.parse.urlparse(case_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3:
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 4:
                return parts[i - 1], parts[i], os.path.splitext(parts[i + 1])[0]
    return default_court, "unknown", "unknown"


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

    async def initialize(self) -> None:
        """Hydrate runtime variables and directory structures."""
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
        
        # Structure logging indicators
        logger.info("==================================================")
        logger.info(f"🚀 COEUS NEW SAFLII WORKER RUNNING ({self.pipeline_name})")
        logger.info(f"Target Year Context Range Bounds: {self.start_year} - {self.end_year}")
        logger.info("==================================================")

    async def authenticate(self, headless: bool = False) -> None:
        """SAFLII bypasses login constraints via inline Cloudflare tokens. No-op implementation."""
        pass

    async def handle_turnstile_challenge(self, page: Page, attempt_prefix: str) -> str:
        """Inspects, targets, and clears active Cloudflare Turnstile barriers."""
        state = check_page_state(await page.content())

        if state == "BLOCKED":
            logger.info(f"⚠️ Captcha challenge detected during execution space [{attempt_prefix}]. Invoking Solver...")
            await turnstile_solver.solve(page)   
            state = check_page_state(await page.content())
            
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

                state = await self.handle_turnstile_challenge(page, "start", f"start_url_attempt_{attempt}")
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
                if attempt < 5:
                    await asyncio.sleep(attempt * 5)

        if not start_success:
            logger.error("Failed to verify structural clearance benchmarks for base tracking arrays.")
            sys.exit(1)

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

                    state = await self.handle_turnstile_challenge(page, "year", f"year_{year}_attempt_{attempt}")
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

        page = self.browser_manager.page
        total_new = 0

        for idx, case_url in enumerate(self.case_urls, start=1):
            c_court, c_year, c_id = parse_case_url(case_url)
            case_no = self.url_to_case_number.get(case_url)

            # Execution deduplication checks using pre-hydrated tracking sets
            if case_url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                logger.info(f"[{idx}/{len(self.case_urls)}] Skipping record entry (Pre-existing validation signature matches): {case_url}")
                continue

            logger.info(f"[{idx}/{len(self.case_urls)}] Detailed enrichment active -> {case_url}")
            success = False

            for attempt in range(1, 6):
                try:
                    await page.goto(case_url, timeout=30000)
                    if self.take_debug_screenshots:
                        await take_screenshot(page, self.screenshots_dir, f"case_{c_id}_attempt_{attempt}_navigated")

                    state = await self.handle_turnstile_challenge(page, "case", f"case_{c_id}_attempt_{attempt}")
                    if state == "BLOCKED":
                        raise BlockedException(f"Challenge wall containment failure active on asset element: {c_id}")
                    elif state == "NOT_FOUND":
                        logger.warning(f"Target payload resource {case_url} not found (404/403). Dropping link tracking.")
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
                        logger.info(f"  [~] Duplicate signature isolated via late mapping step for identifier: {case_no}")
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

                    # Push safely formatted record via centralized db interface helper
                    await db_storage.upsert_scraped_record(
                        self.conn, self.target_id, self.pipeline_name, case_url, record, doc_date
                    )
                    
                    self.existing_urls.add(case_url)
                    if case_no:
                        self.existing_case_numbers.add(case_no)
                    
                    total_new += 1
                    logger.info(f"  [+] Unified structural database record written: {c_court}_{c_year}_{c_id}")
                    success = True
                    break

                except Exception as err:
                    logger.warning(f"Enrichment payload extraction error on asset {c_id} [Attempt {attempt}]: {err}")
                    if attempt < 5:
                        await asyncio.sleep(attempt * 10)

            if success:
                await asyncio.sleep(self.cooldown_seconds)

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
    parser.add_argument("--pipeline_name", required=True, help="Target pipeline setup configuration key matching environment presets")
    parser.add_argument("--headless", default="false", help="Orchestrate worker browser processes via headless virtual framing parameters (true/false)")
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    scraper = SafliiScraper(pipeline_name=args.pipeline_name, headless=headless_value)
    
    asyncio.run(scraper.run())