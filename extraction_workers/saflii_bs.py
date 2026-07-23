import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
from seleniumbase import SB

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("saflii_scraper")


# ---------------------------------------------------------------------------
# Document Parser
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
# Browser Navigation Helper
# ---------------------------------------------------------------------------
def fetch_page_source(sb: SB, target_url: str, retries: int = 2) -> str:
    """Navigates to a URL using UC stealth reconnect and returns the raw HTML source."""
    for attempt in range(retries):
        sb.uc_open_with_reconnect(target_url, reconnect_time=2)
        try:
            sb.uc_gui_handle_captcha()
        except Exception:
            pass

        time.sleep(2)  # Buffer for Cloudflare token redirect
        raw_html = sb.get_page_source()

        if "just a moment" not in (sb.get_page_title() or "").lower():
            return raw_html

        logger.warning(f"Cloudflare hold page hit on {target_url}. Retrying ({attempt+1}/{retries})...")
        time.sleep(3)

    return sb.get_page_source()


def is_valid_case_url(sb: SB, target_url: str) -> bool:
    """Probes if a specific SAFLII candidate URL renders a valid judgment."""
    raw_html = fetch_page_source(sb, target_url)
    try:
        parsed = parse_saflii_case(raw_html, target_url)
        return parsed is not None
    except ValueError:
        # Re-verify once if intercepted by Cloudflare
        time.sleep(3)
        return is_valid_case_url(sb, target_url)


# ---------------------------------------------------------------------------
# Galloping Search Discovery (100 -> 10 -> 1)
# ---------------------------------------------------------------------------
def discover_max_case_number(sb: SB, court_code: str, year: int) -> int:
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
        if is_valid_case_url(sb, test_url):
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
        if is_valid_case_url(sb, test_url):
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
        if is_valid_case_url(sb, test_url):
            max_valid_case = idx
            idx += 1
        else:
            logger.info(f"[-] Hit final 404 boundary at index {idx}.")
            break

    logger.info(f"✓ Discovery Complete: {court_code} ({year}) max case index is {max_valid_case}.")
    return max_valid_case


# ---------------------------------------------------------------------------
# Main Scraping Orchestrator
# ---------------------------------------------------------------------------
def run_saflii_pipeline(
    court_code: str = "ZALCJHB",
    year: int = 2026,
    headless: bool = False,
    use_xvfb: bool = True,
) -> List[Dict[str, Any]]:
    """
    Main driver executing discovery, candidate array generation, 
    and bulk extraction.
    """
    scraped_documents = []

    # Initialize Stealth SeleniumBase Browser
    with SB(uc=True, headless=headless, xvfb=use_xvfb) as sb:
        # Step 1: Discover upper index limit
        max_cases = discover_max_case_number(sb, court_code, year)
        
        if max_cases < 1:
            logger.warning(f"No cases found for {court_code} in year {year}.")
            return []

        # Step 2: Construct direct target URL sequence
        base_url = f"https://www.saflii.org/za/cases/{court_code}/{year}"
        target_urls = [f"{base_url}/{i}.html" for i in range(1, max_cases + 1)]
        logger.info(f"Generated {len(target_urls)} candidate URLs for extraction.")

        # Step 3: Sequential bulk extraction
        for index, url in enumerate(target_urls, start=1):
            logger.info(f"[{index}/{max_cases}] Extracting document: {url}")
            raw_html = fetch_page_source(sb, url)
            
            try:
                record = parse_saflii_case(raw_html, url)
                if record:
                    scraped_documents.append(record)
                    logger.info(f"  └─ Parsed Title: {record['title'][:60]}...")
            except ValueError as err:
                logger.error(f"  └─ Extraction failed for {url}: {err}")

    logger.info(f"Pipeline finished. Successfully scraped {len(scraped_documents)} documents.")
    return scraped_documents


# ---------------------------------------------------------------------------
# Execution Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Example test run for Labour Court Johannesburg (2026)
    results = run_saflii_pipeline(
        court_code="ZALCJHB",
        year=2026,
        headless=False,
        use_xvfb=True,
    )

    print(f"\n--- Output Summary ---")
    print(f"Total Records Extracted: {len(results)}")
    if results:
        print("Sample Record:", results[0]["title"], "| Citation:", results[0]["citation"])