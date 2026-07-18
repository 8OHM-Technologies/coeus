import argparse
import asyncio
import base64
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
import time

import requests
import db_storage

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from utils.utils import fetch_pipeline_config, resolve_data_dir, take_screenshot
from db import get_db_connection
from misstcha import TurnstileSolver
from utils.debug_helper import log_browser_proxy_ip
from utils.browser_helper import (
    setup_logger,
    BrowserManager,
)

logger = setup_logger(__name__)

turnstile_solver = TurnstileSolver()


class BlockedException(Exception):
    pass


def extract_case_number_from_text(text: str) -> str | None:
    """Extract case reference/number from a text snippet, usually in parentheses.
    E.g. "S v Zuma (1/2026)" -> "1/2026"
    """
    matches = re.findall(r'\(([^()]+/[^()]+)\)', text)
    if matches:
        return matches[0].strip()
    return None


def check_page_state(page_title: str, h1_title: str, body_text: str) -> str:
    """
    Determine the logical state of the page.
    Returns:
        "BLOCKED": if it's a Cloudflare/Turnstile challenge page.
        "NOT_FOUND": if it's a SAFLII Apache 403 Forbidden, 404 Not Found, or similar non-existent page.
        "OK": if it's a valid page.
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


async def wait_for_page_load(page, url_type="page"):
    """
    Wait for the page to settle after a Turnstile challenge or navigation.
    url_type can be "start", "year", or "case".
    """
    year_pattern = re.compile(r"^\d{4}$")
    for poll_sec in range(30):
        await asyncio.sleep(1)
        try:
            title = await page.title()
        except Exception:
            continue
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

        state = check_page_state(title, h1, body)
        if state == "NOT_FOUND":
            return "NOT_FOUND"
        if state == "BLOCKED":
            continue

        # Page is in OK state. Let's make sure the specific elements are present.
        if url_type == "start":
            anchors = await page.locator("a").all()
            has_year = False
            for a in anchors:
                try:
                    text = (await a.inner_text()).strip()
                    if year_pattern.match(text):
                        has_year = True
                        break
                except Exception:
                    pass
            if has_year:
                return "OK"
        elif url_type == "year":
            anchors = await page.locator("a").all()
            has_case = False
            for a in anchors:
                try:
                    href = await a.get_attribute("href")
                    if href and href.endswith(".html") and not ("toc-" in href or "index.html" in href):
                        has_case = True
                        break
                except Exception:
                    pass
            if has_case:
                return "OK"
        elif url_type == "case":
            if len(body.strip()) > 500:
                return "OK"
        else:
            if not await turnstile_solver.find_turnstile_frame(page):
                return "OK"

    return "TIMEOUT"


def parse_case_url(case_url, default_court="SAFLII"):
    parsed = urllib.parse.urlparse(case_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3:
        for i in range(len(parts) - 2, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 4:
                year = parts[i]
                case_id = os.path.splitext(parts[i + 1])[0]
                court = parts[i - 1]
                return court, year, case_id
    return default_court, "unknown", "unknown"


async def run_extraction(pipeline_name: str, headless: bool = False):
    config = await fetch_pipeline_config(pipeline_name)
    start_url = config.get("start_url")
    if not start_url:
        logger.error("No start_url configured in pipeline config.")
        sys.exit(1)

    allow_insecure_requests = config.get("allow_insecure_requests", False)
    extraction_params = config.get("extraction_params", {})
    start_year = int(extraction_params.get("start_year", 2000))
    current_year = datetime.now().year
    end_year = int(extraction_params.get("end_year", current_year))
    cooldown_seconds = float(extraction_params.get("cooldown_seconds", 1.5))

    proxy_url = None
    use_proxy = False
    WEBSHARE_PROXY = "http://ooumozlx-rotate:aud9ea66yrrq@p.webshare.io:80/"

#    if use_proxy:
#        masked_proxy = WEBSHARE_PROXY
#        if "@" in WEBSHARE_PROXY:
#            parts = WEBSHARE_PROXY.split("@")
#            creds_part = parts[0].split("://")
#            scheme = creds_part[0]
#            user = creds_part[1].split(":")[0]
#            host_part = parts[1]
#            masked_proxy = f"{scheme}://{user}:****@{host_part}"
#        logger.info(f"Using rotating proxy configuration: {masked_proxy}")

#        logger.info(
#            "Proxy is ENABLED — verifying connectivity via Webshare rotating proxy..."
#        )
#        try:
#            ip = requests.get(
#                "https://ipv4.webshare.io/",
#                proxies={"http": WEBSHARE_PROXY, "https": WEBSHARE_PROXY},
#                timeout=15,
#            ).text.strip()
#            logger.info(f"Proxy active. Outbound IP: {ip}")
#        except Exception as proxy_err:
#            logger.error(f"Proxy connectivity check failed: {proxy_err}")
#            sys.exit(1)
#        proxy_url = WEBSHARE_PROXY

    logger.info("==================================================")
    logger.info(
        f"🚀 COEUS NEW SAFLII WORKER INITIALIZED (PIPELINE: {pipeline_name})"
    )
    logger.info(f"Target URL: {start_url}")
    logger.info(f"Year Range: {start_year} - {end_year}")
    logger.info("==================================================")

    parsed = urllib.parse.urlparse(start_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    court_name = path_parts[-1] if path_parts else "SAFLII"

    document_type = config.get("document_type", "awards").lower()
    output_dir = resolve_data_dir(pipeline_name, document_type)
    logger.info(f"Output directory: {os.path.abspath(output_dir)}")

    take_debug_screenshots = config.get("take_debug_screenshots", False)
    screenshots_dir = os.path.join(os.path.dirname(output_dir), "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    logger.info(f"Screenshots directory: {os.path.abspath(screenshots_dir)}")

    # Establish DB Connection and Resolve target/deduplication URLs
    conn = await get_db_connection()
    entity_name = config.get("name") or pipeline_name
    target_name = config.get("subset") or court_name

    logger.info(f"Resolving entity='{entity_name}' and target='{target_name}'...")
    target_id = await db_storage.resolve_target_id(
        conn, entity_name, target_name, start_url
    )

    existing_urls = await db_storage.get_existing_urls(conn, pipeline_name)
    logger.info(f"Loaded {len(existing_urls)} unique URLs from database.")

    existing_case_numbers = await db_storage.get_existing_case_numbers(conn, pipeline_name)
    logger.info(f"Loaded {len(existing_case_numbers)} unique case numbers from database.")

    p = await async_playwright().start()
    logger.info(f"Launching Playwright browser via CDP (headless={headless})...")
    manager = BrowserManager(
        p,
        headless=headless,
        ignore_https_errors=allow_insecure_requests,
        proxy_url=proxy_url,
        viewport={"width": 1280, "height": 720},
    )
    try:
        page = await manager.start()

        await log_browser_proxy_ip(page, "Startup", use_proxy)

        # Step 1: Navigating to Start URL and Collecting Years
        logger.info(f"Navigating to start URL: {start_url}")

        max_start_attempts = 5
        start_success = False
        year_links = []
        for attempt in range(1, max_start_attempts + 1):
            try:
                await page.goto(start_url, timeout=30000)
                if take_debug_screenshots:
                    await take_screenshot(page, screenshots_dir, f"start_url_attempt_{attempt}_navigated")

                title = await page.title()
                h1 = (
                    await page.locator("h1").first.inner_text()
                    if await page.locator("h1").count() > 0
                    else ""
                )
                body = (
                    await page.locator("body").inner_text()
                    if await page.locator("body").count() > 0
                    else ""
                )
                state = check_page_state(title, h1, body)

                if state == "BLOCKED":
                    solve_res = await turnstile_solver.solve(page, screenshot_dir=screenshots_dir)
                    if solve_res["success"]:
                        await wait_for_page_load(page, "start")
                        if take_debug_screenshots:
                            await take_screenshot(page, screenshots_dir, f"start_url_attempt_{attempt}_post_solve")
                    else:
                        await asyncio.sleep(2)

                    title = await page.title()
                    h1 = (
                        await page.locator("h1").first.inner_text()
                        if await page.locator("h1").count() > 0
                        else ""
                    )
                    body = (
                        await page.locator("body").inner_text()
                        if await page.locator("body").count() > 0
                        else ""
                    )
                    state = check_page_state(title, h1, body)

                if state == "BLOCKED":
                    raise BlockedException("Blocked by Turnstile on the start URL")
                elif state == "NOT_FOUND":
                    raise Exception(f"Start URL returned 404/403 (Title: {title}, H1: {h1})")

                logger.info("Extracting year links...")
                year_pattern = re.compile(r"^\d{4}$")
                temp_year_links = []
                anchors = await page.locator("a").all()
                for anchor in anchors:
                    href = await anchor.get_attribute("href")
                    text = (await anchor.inner_text()).strip()
                    if href and year_pattern.match(text):
                        year = int(text)
                        if start_year <= year <= end_year:
                            abs_url = urllib.parse.urljoin(start_url, href)
                            temp_year_links.append((year, abs_url))

                if temp_year_links:
                    year_links = sorted(list(set(temp_year_links)), key=lambda x: x[0])
                    start_success = True
                    break
                else:
                    raise Exception("No year links found on the start page.")
            except Exception as e:
                logger.warning(
                    f"Attempt {attempt}/{max_start_attempts} failed on start URL: {e}"
                )
                if take_debug_screenshots:
                    await take_screenshot(page, screenshots_dir, f"start_url_attempt_{attempt}_error")
                if attempt < max_start_attempts:
                    await asyncio.sleep(attempt * 5)

        if not start_success:
            logger.error("Failed to load and bypass Turnstile on the start URL. Exiting.")
            await browser.close()
            sys.exit(1)

        logger.info(
            f"Found {len(year_links)} years to process: {[y for y, _ in year_links]}"
        )

        # Step 2: Collecting Case Links from Each Year URL
        case_urls = []
        url_to_case_number = {}
        for year, year_url in year_links:
            logger.info(f"Navigating to year {year} URL: {year_url}")
            await log_browser_proxy_ip(page, f"Year {year}", use_proxy)

            max_attempts = 5
            success = False
            for attempt in range(1, max_attempts + 1):
                try:
                    await page.goto(year_url, timeout=30000)
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"year_{year}_attempt_{attempt}_navigated")

                    t_title = await page.title()
                    t_h1 = (
                        await page.locator("h1").first.inner_text()
                        if await page.locator("h1").count() > 0
                        else ""
                    )
                    t_body = (
                        await page.locator("body").inner_text()
                        if await page.locator("body").count() > 0
                        else ""
                    )
                    t_state = check_page_state(t_title, t_h1, t_body)

                    if t_state == "BLOCKED":
                        solve_res = await turnstile_solver.solve(page, screenshot_dir=screenshots_dir)
                        if solve_res["success"]:
                            await wait_for_page_load(page, "year")
                            if take_debug_screenshots:
                                await take_screenshot(page, screenshots_dir, f"year_{year}_attempt_{attempt}_post_solve")
                        else:
                            await asyncio.sleep(2)

                        t_title = await page.title()
                        t_h1 = (
                            await page.locator("h1").first.inner_text()
                            if await page.locator("h1").count() > 0
                            else ""
                        )
                        t_body = (
                            await page.locator("body").inner_text()
                            if await page.locator("body").count() > 0
                            else ""
                        )
                        t_state = check_page_state(t_title, t_h1, t_body)

                    if t_state == "BLOCKED":
                        raise BlockedException("Blocked by Turnstile on year page")
                    elif t_state == "NOT_FOUND":
                        logger.warning(
                            f"Year page {year_url} returned 404/403. Skipping this year."
                        )
                        success = True
                        break

                    y_anchors = await page.locator("a").all()
                    year_case_urls = []
                    for y_anchor in y_anchors:
                        href = await y_anchor.get_attribute("href")
                        if not href:
                            continue
                        abs_url = urllib.parse.urljoin(year_url, href)

                        parsed_url = urllib.parse.urlparse(abs_url)
                        parts = [p for p in parsed_url.path.split("/") if p]
                        if len(parts) >= 2:
                            parent_dir = parts[-2]
                            filename = parts[-1]
                            if (
                                parent_dir.isdigit()
                                and len(parent_dir) == 4
                                and filename.endswith(".html")
                            ):
                                case_year_val = int(parent_dir)
                                if start_year <= case_year_val <= end_year:
                                    if not (
                                        "toc-" in filename
                                        or filename == "index.html"
                                    ):
                                        text = await y_anchor.inner_text()
                                        case_no = extract_case_number_from_text(text)
                                        if case_no:
                                            url_to_case_number[abs_url] = case_no
                                        year_case_urls.append(abs_url)

                    year_case_urls = list(set(year_case_urls))
                    logger.info(
                        f"Found {len(year_case_urls)} cases for year {year}."
                    )
                    case_urls.extend(year_case_urls)
                    success = True
                    break
                except BlockedException as be:
                    logger.warning(
                        f"Blocked on year page {year_url} (attempt {attempt}/{max_attempts}): {be}"
                    )
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"year_{year}_attempt_{attempt}_blocked_error")
                    if attempt < max_attempts:
                        await asyncio.sleep(attempt * 10)
                except Exception as e:
                    logger.warning(
                        f"Error loading year page {year_url} (attempt {attempt}/{max_attempts}): {e}"
                    )
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"year_{year}_attempt_{attempt}_error")
                    if attempt < max_attempts:
                        await asyncio.sleep(attempt * 10)
                    else:
                        logger.error(
                            f"Failed to load year page {year_url} after {max_attempts} attempts."
                        )

        # Step 3: Scraping Loop for Case URLs
        case_urls = sorted(list(set(case_urls)))
        logger.info(f"Total unique case URLs to process: {len(case_urls)}")

        total_new = 0

        for idx, case_url in enumerate(case_urls, start=1):
            c_court, c_year, c_id = parse_case_url(case_url)
            case_no = url_to_case_number.get(case_url)

            is_existing = False
            if case_url in existing_urls:
                is_existing = True
            elif case_no and case_no in existing_case_numbers:
                is_existing = True

            if is_existing:
                logger.info(
                    f"[{idx}/{len(case_urls)}] Skipping (already scraped): {case_url}"
                )
                continue

            logger.info(
                f"[{idx}/{len(case_urls)}] Scraping case: {case_url}"
            )

            max_attempts = 5
            success = False
            for attempt in range(1, max_attempts + 1):
                try:
                    await page.goto(case_url, timeout=30000)
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"case_{c_id}_attempt_{attempt}_navigated")

                    c_title = await page.title()
                    c_h1 = (
                        await page.locator("h1").first.inner_text()
                        if await page.locator("h1").count() > 0
                        else ""
                    )
                    c_body = (
                        await page.locator("body").inner_text()
                        if await page.locator("body").count() > 0
                        else ""
                    )
                    c_state = check_page_state(c_title, c_h1, c_body)

                    if c_state == "BLOCKED":
                        solve_res = await turnstile_solver.solve(page, screenshot_dir=screenshots_dir)
                        if solve_res["success"]:
                            await wait_for_page_load(page, "case")
                            if take_debug_screenshots:
                                await take_screenshot(page, screenshots_dir, f"case_{c_id}_attempt_{attempt}_post_solve")
                        else:
                            await asyncio.sleep(2)

                        c_title = await page.title()
                        c_h1 = (
                            await page.locator("h1").first.inner_text()
                            if await page.locator("h1").count() > 0
                            else ""
                        )
                        c_body = (
                            await page.locator("body").inner_text()
                            if await page.locator("body").count() > 0
                            else ""
                        )
                        c_state = check_page_state(c_title, c_h1, c_body)

                    if c_state == "BLOCKED":
                        raise BlockedException(
                            "Blocked by Turnstile on case page"
                        )
                    elif c_state == "NOT_FOUND":
                        logger.warning(
                            f"Case URL {case_url} returned 404/403. Skipping."
                        )
                        success = True
                        break

                    html_content = await page.content()

                    # Parse and extract targeted content from div#center
                    soup = BeautifulSoup(html_content, "lxml")
                    center_div = soup.find("div", id="center")
                    center_html = str(center_div) if center_div else ""

                    # Extract the page title (h2 inside center contains the case title)
                    h2_el = center_div.find("h2") if center_div else None
                    title = h2_el.get_text(strip=True) if h2_el else c_title

                    # Attempt to extract case number from title if not already known
                    if not case_no:
                        case_no = extract_case_number_from_text(title)

                    if case_no and case_no in existing_case_numbers:
                        logger.info(
                            f"  [~] Skipping (case number {case_no} already scraped): {case_url}"
                        )
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
                        doc_date = date(int(c_year), 1, 1)
                    except Exception:
                        from datetime import date as dt_date
                        doc_date = dt_date.today()

                    await db_storage.upsert_scraped_record(
                        conn, target_id, pipeline_name, case_url, record, doc_date
                    )
                    existing_urls.add(case_url)
                    if case_no:
                        existing_case_numbers.add(case_no)
                    total_new += 1
                    logger.info(f"  [+] Extracted record: {c_court}_{c_year}_{c_id}")

                    success = True
                    break
                except BlockedException as be:
                    logger.warning(
                        f"Blocked on case page {case_url} (attempt {attempt}/{max_attempts}): {be}"
                    )
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"case_{c_id}_attempt_{attempt}_blocked_error")
                    if attempt < max_attempts:
                        await asyncio.sleep(attempt * 10)
                except Exception as e:
                    logger.warning(
                        f"Error scraping case page {case_url} (attempt {attempt}/{max_attempts}): {e}"
                    )
                    if take_debug_screenshots:
                        await take_screenshot(page, screenshots_dir, f"case_{c_id}_attempt_{attempt}_error")
                    if attempt < max_attempts:
                        await asyncio.sleep(attempt * 10)
                    else:
                        logger.error(
                            f"Failed to scrape case page {case_url} after {max_attempts} attempts."
                        )

            if success:
                await asyncio.sleep(cooldown_seconds)

        logger.info(
            f"✅ Extraction completed. Saved {total_new} new records directly to database."
        )

    finally:
        await manager.close()
        await p.stop()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus New SAFLII Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    parser.add_argument(
        "--headless",
        default="false",
        help="Run browser in headless mode (true or false, defaults to false)",
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    asyncio.run(
        run_extraction(args.pipeline_name, headless=headless_value)
    )