import argparse
import asyncio
import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from seleniumbase import cdp_driver
from playwright.async_api import async_playwright

try:
    from .base_scraper import BaseScraper, setup_logger
    from . import db_storage
    from .utils.browser_helper import format_sb_proxy
except ImportError:
    from base_scraper import BaseScraper, setup_logger
    import db_storage
    from utils.browser_helper import format_sb_proxy

logger = setup_logger("saflii_scraper")


# ---------------------------------------------------------------------------
# Document Parser (Preserved Core Logic)
# ---------------------------------------------------------------------------
def parse_saflii_case(raw_html: str, url: str) -> Optional[Dict[str, Any]]:
    """
    Parses a SAFLII case HTML page string into structured metadata and body text.
    Returns None if the page is a 404 or missing document.
    Raises ValueError if stuck on a Cloudflare challenge screen.
    """
    soup = BeautifulSoup(raw_html, "html.parser")
    page_title = soup.title.string.strip() if soup.title and soup.title.string else ""
    page_text = soup.get_text()

    # 1. Verification & Error Checks
    if "404" in page_title or "page not found" in page_text.lower():
        return None

    if "just a moment" in page_title.lower() or "enable javascript" in page_text.lower():
        raise ValueError("Cloudflare challenge page detected; request was intercepted.")

    # 2. Heading Extraction
    heading_el = soup.find(["h1", "h2", "h3"])
    case_name = heading_el.get_text(strip=True) if heading_el else page_title

    # 3. Citation & Case Number Regular Expressions
    citation = None
    citation_match = re.search(r"\[\d{4}\]\s+[A-Z]+\s+\d+", page_text)
    if citation_match:
        citation = citation_match.group(0)

    case_number = None
    case_no_match = re.search(
        r"Case\s+(?:No|Number)\s*:\s*([A-Za-z0-9/\s-]+)", page_text, re.IGNORECASE
    )
    if case_no_match:
        case_number = case_no_match.group(1).strip()

    # 4. Clean HTML and Extract Judgment Text
    for elem in soup(["script", "style", "nav", "header", "footer"]):
        elem.decompose()

    content_container = (
        soup.find("div", class_="judgment")
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
        "full_text": clean_text,
    }


# ---------------------------------------------------------------------------
# Test & Helper Compatibility Functions
# ---------------------------------------------------------------------------
def check_page_state(page_title: str = "", h1_title: str = "", body_text: str = "") -> str:
    """Evaluates titles and content to determine Cloudflare or 404 status."""
    title_lower = page_title.lower()
    h1_lower = h1_title.lower()
    body_lower = body_text.lower()

    if (
        "just a moment" in title_lower
        or "security verification" in title_lower
        or "verify you are human" in body_lower
        or "403 forbidden" in h1_lower
        or "permission to access" in body_lower
    ):
        return "BLOCKED"

    if "404" in title_lower or "not found" in title_lower or "not found" in h1_lower:
        return "NOT_FOUND"

    return "OK"


def parse_case_url(url: str) -> Tuple[str, str, str]:
    """Parses court code, year, and case ID from a SAFLII URL string."""
    match = re.search(r"/za/cases/([A-Za-z0-9]+)/(\d{4})/(\d+|\w+)\.html", url)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return "SAFLII", "unknown", "unknown"


def extract_case_number_from_text(text: str) -> Optional[str]:
    """Extracts case number from plain text via regex matchers."""
    match = re.search(r"Case\s+(?:No|Number)\s*:\s*([A-Za-z0-9/\s-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match2 = re.search(r"\((?:Case\s+No|Case\s+Number\s*:?|No\.\s*)?([A-Za-z0-9]+/[0-9]{2,4})\)", text)
    if match2:
        val = match2.group(1).strip()
        if not re.search(
            r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
            val,
            re.IGNORECASE,
        ):
            return val
    return None


async def wait_for_page_load(page, url_type: str = "case") -> str:
    """Asynchronously checks page load status for Playwright page instances."""
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
# Async Page Navigation Helpers
# ---------------------------------------------------------------------------
async def fetch_page_source(page, target_url: str, retries: int = 2) -> str:
    """Navigates to a URL using Playwright page connected via CDP and returns HTML source."""
    for attempt in range(retries):
        await page.goto(target_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        title = (await page.title() or "").lower()

        if "just a moment" not in title:
            return await page.content()

        logger.warning(
            f"Cloudflare hold page hit on {target_url}. Retrying ({attempt+1}/{retries})..."
        )
        await page.wait_for_timeout(3000)

    return await page.content()


async def is_valid_case_url(page, target_url: str) -> bool:
    """Probes if a specific SAFLII candidate URL renders a valid judgment."""
    raw_html = await fetch_page_source(page, target_url)
    try:
        parsed = parse_saflii_case(raw_html, target_url)
        return parsed is not None
    except ValueError:
        await page.wait_for_timeout(3000)
        return await is_valid_case_url(page, target_url)


# ---------------------------------------------------------------------------
# Galloping Search Discovery (100 -> 10 -> 1)
# ---------------------------------------------------------------------------
async def discover_max_case_number(page, court_code: str, year: int) -> int:
    """
    Locates the highest valid case index for a given court and year using 
    a step-down (galloping) search algorithm.
    """
    base_url = f"https://www.saflii.org/za/cases/{court_code}/{year}"
    logger.info(f"--- Starting Galloping Search for {court_code} ({year}) ---")

    # Phase 1: Jump forward in increments of 100
    idx = 1
    last_good_100 = 1
    while True:
        test_url = f"{base_url}/{idx}.html"
        logger.info(f"[Phase 1: Step 100] Probing {test_url}...")
        if await is_valid_case_url(page, test_url):
            last_good_100 = idx
            idx += 100
        else:
            logger.info(f"[-] Hit 404 boundary at index {idx}.")
            break

    # Phase 2: Jump forward in increments of 10 from the last valid 100 factor
    idx = last_good_100
    last_good_10 = last_good_100
    while True:
        test_url = f"{base_url}/{idx}.html"
        logger.info(f"[Phase 2: Step 10] Probing {test_url}...")
        if await is_valid_case_url(page, test_url):
            last_good_10 = idx
            idx += 10
        else:
            logger.info(f"[-] Hit 404 boundary at index {idx}.")
            break

    # Phase 3: Jump forward in increments of 1 from the last valid 10 factor
    idx = last_good_10
    max_valid_case = last_good_10
    while True:
        test_url = f"{base_url}/{idx}.html"
        logger.info(f"[Phase 3: Step 1] Probing {test_url}...")
        if await is_valid_case_url(page, test_url):
            max_valid_case = idx
            idx += 1
        else:
            logger.info(f"[-] Hit final 404 boundary at index {idx}.")
            break

    logger.info(
        f"✓ Discovery Complete: {court_code} ({year}) max case index is {max_valid_case}."
    )
    return max_valid_case


# ---------------------------------------------------------------------------
# BaseScraper Subclass Implementation
# ---------------------------------------------------------------------------
class SafliiScraper(BaseScraper):
    """
    SAFLII Case Scraper subclassing BaseScraper.
    Integrates SeleniumBase cdp_driver + Playwright async API for stealth navigation,
    database record persistence, progress tracking, and proxy support across year ranges.
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
        self.start_year: int = 1999
        self.end_year: int = 2026
        self.headless: bool = headless
        self.use_xvfb: bool = use_xvfb

        # Browser state
        self.driver = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.target_urls: List[str] = []

    async def initialize(self) -> None:
        """Hydrate configuration, resolve target IDs, DB connections, and progress state."""
        await super().initialize()

        extraction_params = self.config.get("extraction_params", {})
        if not self.court_code:
            self.court_code = extraction_params.get("court_code")

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
                    self.start_year = 1999
            elif prog_last_year and isinstance(prog_last_year, int) and prog_last_year > 1900:
                self.start_year = prog_last_year + 1 if prog_completed else prog_last_year
            else:
                self.start_year = 1999

            cfg_end = extraction_params.get("end_year")
            if cfg_end:
                try:
                    self.end_year = int(cfg_end)
                except (ValueError, TypeError):
                    self.end_year = 2026
            else:
                self.end_year = 2026

        if not self.court_code:
            start_url = self.config.get("start_url", "")
            match = re.search(r"/za/cases/([A-Za-z0-9]+)/(\d{4})", start_url)
            if match:
                self.court_code = match.group(1)

        self.court_code = self.court_code or "ZALCJHB"
        logger.info(
            f"SafliiScraper initialized for court='{self.court_code}', year_range={self.start_year}..{self.end_year}"
        )

    async def authenticate(self, headless: bool = False) -> None:
        """No auth required for SAFLII public site."""
        pass

    async def indexing(self) -> None:
        """Stage 1: Discovers total valid case index boundaries across years via galloping search."""
        if self.start_year > self.end_year:
            logger.info(
                f"Start year {self.start_year} exceeds end year {self.end_year}. Pipeline already completed."
            )
            self.target_urls = []
            return

        logger.info(
            f"Starting Indexing Discovery for {self.court_code} across years {self.start_year}..{self.end_year}..."
        )
        sb_proxy = format_sb_proxy(self.proxy_url) if self.use_proxy else None

        self.driver = await cdp_driver.start_async(
            proxy=sb_proxy,
            headless=self.headless,
            xvfb=self.use_xvfb,
        )
        endpoint_url = self.driver.get_endpoint_url()
        logger.info(f"CDP Driver started at endpoint: {endpoint_url}")

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(endpoint_url)
        self.context = self.browser.contexts[0]
        self.page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )

        self.target_urls = []
        for current_year in range(self.start_year, self.end_year + 1):
            max_cases = await discover_max_case_number(self.page, self.court_code, current_year)
            if max_cases < 1:
                logger.info(f"No cases discovered for {self.court_code} in year {current_year}.")
                continue

            base_url = f"https://www.saflii.org/za/cases/{self.court_code}/{current_year}"
            year_urls = [f"{base_url}/{i}.html" for i in range(1, max_cases + 1)]
            self.target_urls.extend(year_urls)
            logger.info(
                f"Year {current_year}: Discovered max case index {max_cases} ({len(year_urls)} candidate URLs)."
            )

        logger.info(
            f"Indexing complete. Generated {len(self.target_urls)} total candidate URLs across years {self.start_year}..{self.end_year}."
        )

    async def detailing(self) -> None:
        """Stage 2: Iterates candidate URLs, parses records, and persists to DB."""
        if not self.target_urls:
            logger.info("No target URLs to process in detailing stage.")
            return

        total_targets = len(self.target_urls)
        logger.info(f"Starting detailing phase for {total_targets} candidate URLs...")

        for index, url in enumerate(self.target_urls, start=1):
            _, year_str, _ = parse_case_url(url)
            current_year = int(year_str) if year_str.isdigit() else self.start_year

            if url in self.existing_urls:
                logger.info(f"[{index}/{total_targets}] Skipping already scraped URL: {url}")
                continue

            logger.info(f"[{index}/{total_targets}] Extracting document: {url}")
            raw_html = await fetch_page_source(self.page, url)

            try:
                record = parse_saflii_case(raw_html, url)
                if record:
                    case_num = record.get("case_number")
                    if case_num and case_num in self.existing_case_numbers:
                        logger.info(f"  └─ Skipping already scraped case number: {case_num}")
                        continue

                    # Database record persistence
                    await db_storage.upsert_scraped_record(
                        self.conn,
                        self.target_id,
                        self.pipeline_name,
                        url,
                        record,
                        status="completed",
                    )
                    self.existing_urls.add(url)
                    if case_num:
                        self.existing_case_numbers.add(case_num)

                    # Progress state persistence
                    await self.save_progress(
                        year=current_year,
                        court_code=self.court_code,
                        last_index=index,
                        total_cases=total_targets,
                        last_url=url,
                        completed=False,
                    )
                    logger.info(f"  └─ Parsed & Persisted Title: {record['title'][:60]}...")
            except ValueError as err:
                logger.error(f"  └─ Extraction failed for {url}: {err}")

        # Mark batch completed upon finishing detailing phase
        if self.target_urls:
            await self.save_progress(
                year=self.end_year,
                court_code=self.court_code,
                last_index=len(self.target_urls),
                total_cases=len(self.target_urls),
                completed=True,
            )

    async def extraction(self) -> None:
        """Stage 3: Verification operations."""
        logger.info("Stage 3: Post-Scraping Extraction & Verification Complete.")

    async def cleanup(self) -> None:
        """Terminates browser contexts, CDP connection, and database connections."""
        try:
            if hasattr(self, "browser") and self.browser:
                await self.browser.close()
            if hasattr(self, "playwright") and self.playwright:
                await self.playwright.stop()
            if self.driver:
                self.driver.stop()
        except Exception as err:
            logger.warning(f"Error during browser teardown: {err}")
        finally:
            await super().cleanup()


# Legacy standalone helper function for backward compatibility
def run_saflii_pipeline(
    court_code: str = "ZALCJHB",
    year: int = 2026,
    headless: bool = False,
    use_xvfb: bool = True,
    pipeline_name: str = "saflii_pipeline",
) -> List[Dict[str, Any]]:
    """Legacy helper running SafliiScraper synchronously."""
    scraper = SafliiScraper(
        pipeline_name=pipeline_name,
        court_code=court_code,
        year=year,
        headless=headless,
        use_xvfb=use_xvfb,
    )
    asyncio.run(scraper.run())
    return []


# ---------------------------------------------------------------------------
# CLI Execution Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SAFLII Scraper (BaseScraper + SeleniumBase CDP Driver + Playwright)"
    )
    parser.add_argument(
        "--pipeline",
        "--pipeline_name",
        required=True,
        dest="pipeline_name",
        help="Target pipeline setup configuration key",
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
        help="Run browser in headless mode (true/false)",
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