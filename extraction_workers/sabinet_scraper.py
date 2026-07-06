import argparse
import asyncio
import calendar
import json
import logging
import os
import sys
from datetime import datetime, date
from playwright.async_api import async_playwright
try:
    from .utils.utils import fetch_pipeline_config
except ImportError:
    # pyrefly: ignore [missing-import]
    from utils.utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Authentication / Session State Generation
# -----------------------------------------------------------------------------
async def generate_state():
    """Automatically log in to Sabinet and persist the browser session state.

    Uses Playwright to perform a fully automated login flow:
    1. Navigate to the CCMA Awards search page.
    2. Dismiss the cookie consent banner (if present).
    3. Click the "Sign in" button in the header.
    4. Fill in the username and password in the sidebar drawer.
    5. Click "Sign into Account" and wait for successful authentication.

    The function saves two copies of the storage state JSON:
    - ``data/state.json`` – a generic location used by the other scrapers.
    - ``data/sabinet_ccma/html/state.json`` – retained for backward‑compatibility.
    """
    scrape_url = (
        "https://discover.sabinet.co.za/search?"
        "Search=&ProductType=ccmabargainingcouncilawards"
        "&resultsortOption=%22Date+Oldest+first%22"
    )
    username = "TiaanF"
    password = "G7fR7Bzv4$@ea5!"

    # Target file paths
    save_path_root = os.path.join("data", "state.json")
    save_path_ccma = os.path.join("data", "sabinet_ccma", "html", "state.json")

    os.makedirs(os.path.join("data", "sabinet_ccma", "html"), exist_ok=True)

    async with async_playwright() as p:
        logger.info("[INFO] Launching Chromium browser for automated authentication...")
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # ------------------------------------------------------------------
        # Step 1: Navigate to the scrape URL
        # ------------------------------------------------------------------
        logger.info(f"💡 Navigating to: {scrape_url}")
        await page.goto(scrape_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle")

        # ------------------------------------------------------------------
        # Step 2: Dismiss cookie consent banner (if present)
        # ------------------------------------------------------------------
        try:
            cookie_btn = page.locator("button:has-text('Accept all cookies')").first
            await cookie_btn.wait_for(state="visible", timeout=5000)
            logger.info("🍪 Cookie consent modal detected. Accepting...")
            await cookie_btn.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
        except Exception:
            logger.info("No cookie consent modal detected or already dismissed.")

        # ------------------------------------------------------------------
        # Step 3: Click the "Sign in" button in the top-right header
        # ------------------------------------------------------------------
        logger.info("🔐 Clicking 'Sign in' button...")
        sign_in_btn = page.get_by_role("button", name="Sign in login")
        await sign_in_btn.wait_for(state="visible", timeout=15000)
        await sign_in_btn.click()
        await asyncio.sleep(1)

        # ------------------------------------------------------------------
        # Step 4: Fill in credentials in the sidebar drawer
        # ------------------------------------------------------------------
        logger.info("✏️  Entering credentials...")
        username_input = page.get_by_role("textbox", name="* Username")
        await username_input.wait_for(state="visible", timeout=10000)
        await username_input.fill(username)

        password_input = page.get_by_role("textbox", name="* Password")
        await password_input.fill(password)

        # ------------------------------------------------------------------
        # Step 5: Submit and wait for authenticated state
        # ------------------------------------------------------------------
        logger.info("🚀 Submitting login form...")
        await page.get_by_role("button", name="Sign into Account").click()

        # Wait until the "myDiscover" user menu appears – confirms auth success
        logger.info("⏳ Waiting for authentication to complete...")
        await page.get_by_role("button", name="user myDiscover down").wait_for(
            state="visible", timeout=30000
        )
        logger.info("✅ Authentication successful!")

        # ------------------------------------------------------------------
        # Persist the session state to both paths
        # ------------------------------------------------------------------
        await context.storage_state(path=save_path_root)
        await context.storage_state(path=save_path_ccma)
        logger.info("\n✅ Session state saved to:")
        logger.info(f"   - {save_path_root}")
        logger.info(f"   - {save_path_ccma}")

        await browser.close()
        logger.info("Browser closed.")

# -----------------------------------------------------------------------------
# Index Scraper – extracts the list of awards using 1-month rolling windows
#
# The Sabinet search interface has a hard limit of 10,000 entries in its list
# view. To capture all entries for years that have more than 10,000 records
# (some years have 30,000+), we use the Advanced Search date-range filter with
# a 1-month rolling window. Progress is persisted to a JSON state file so runs
# can be safely resumed after interruption.
# -----------------------------------------------------------------------------

# JS snippet that extracts card data from the current page – shared by all
# pagination iterations to avoid repetition.
_EXTRACT_ITEMS_JS = r"""
    () => {
        const cards = Array.from(document.querySelectorAll('li.ant-list-item'));
        if (cards.length === 0) return [];

        const firstContainer = document.querySelector('.discCardContainer');
        let rawHits = null;
        if (firstContainer) {
            const keys = Object.keys(firstContainer);
            const reactKey = keys.find(k =>
                k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
            );
            if (reactKey) {
                let curr = firstContainer[reactKey];
                while (curr) {
                    if (curr.memoizedProps) {
                        if (Array.isArray(curr.memoizedProps.hits)) {
                            rawHits = curr.memoizedProps.hits;
                            break;
                        }
                        if (
                            curr.memoizedProps.rawData &&
                            curr.memoizedProps.rawData.hits &&
                            Array.isArray(curr.memoizedProps.rawData.hits.hits)
                        ) {
                            rawHits = curr.memoizedProps.rawData.hits.hits;
                            break;
                        }
                    }
                    curr = curr.return;
                }
            }
        }

        const items = [];
        cards.forEach((card, idx) => {
            const titleEl = card.querySelector('.ant-list-item-meta-title');
            let titleText = '';
            if (titleEl) {
                const clone = titleEl.cloneNode(true);
                const icons = clone.querySelectorAll('.anticon, [aria-label="lock"]');
                icons.forEach(icon => icon.remove());
                titleText = clone.innerText.trim();
            }

            const tags = Array.from(card.querySelectorAll('.ant-list-item-meta-description .ant-tag'));
            const metadata = {};
            tags.forEach(tag => {
                const text = tag.innerText || '';
                if (text.includes(':')) {
                    const parts = text.split(':');
                    const key = parts[0].trim().toLowerCase().replace(/\s+/g, '_');
                    const val = parts.slice(1).join(':').trim();
                    metadata[key] = val;
                }
            });

            let id = null;
            if (rawHits && rawHits[idx]) {
                const hit = rawHits[idx];
                id = hit._id || hit.id || (hit._source ? hit._source.id : null);
            }

            items.push({
                title: titleText,
                ...metadata,
                detail_url: id ? `https://discover.sabinet.co.za/document/${id}` : null,
            });
        });
        return items;
    }
"""


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    start_url = (
        config.get("start_url")
        or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"
    )

    # Resolve an appropriate output directory – support both container and local layouts
    output_dir = os.path.join("/app/data", pipeline_name, config.get("document_type", "awards").lower())
    if not os.path.exists("/app/data") and not os.path.exists("/app"):
        output_dir = os.path.join("data", pipeline_name, config.get("document_type", "awards").lower())
    os.makedirs(output_dir, exist_ok=True)

    logger.info("==================================================")
    logger.info(f"🚀 COEUS SABINET WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {start_url}")
    logger.info("==================================================")

    output_file = os.path.join(output_dir, f"{pipeline_name}.json")
    progress_file = os.path.join(output_dir, f"{pipeline_name}_index_state.json")

    # -------------------------------------------------------------------------
    # Load previously scraped data – build a URL-based deduplication set so we
    # can skip records we already have regardless of pagination order.
    # -------------------------------------------------------------------------
    existing_data: list[dict] = []
    existing_urls: set[str] = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            for item in existing_data:
                if item.get("detail_url"):
                    existing_urls.add(item["detail_url"])
            logger.info(f"Loaded {len(existing_data)} existing records. {len(existing_urls)} unique URLs tracked.")
        except Exception as read_err:
            logger.warning(f"Could not load existing data from {output_file}: {read_err}. Performing full scrape.")

    # -------------------------------------------------------------------------
    # Load progress state for resumability
    # -------------------------------------------------------------------------
    progress_state: dict = {}
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                progress_state = json.load(f)
            logger.info(f"Resuming from progress state: {progress_state}")
        except Exception:
            logger.warning("Could not read progress state file. Starting from scratch.")

    def _save_progress(year: int, month: int, completed: bool = False) -> None:
        progress_state["last_year"] = year
        progress_state["last_month"] = month
        progress_state["last_completed"] = completed
        with open(progress_file, "w", encoding="utf-8") as pf:
            json.dump(progress_state, pf, indent=2)

    # -------------------------------------------------------------------------
    # Determine the resume point
    # -------------------------------------------------------------------------
    resume_year: int = progress_state.get("last_year", 0)
    resume_month: int = progress_state.get("last_month", 0)
    last_completed: bool = progress_state.get("last_completed", True)

    # If the last window completed cleanly, advance past it
    if last_completed and resume_year > 0:
        resume_month += 1
        if resume_month > 12:
            resume_month = 1
            resume_year += 1

    # -------------------------------------------------------------------------
    # Playwright session
    # -------------------------------------------------------------------------
    extracted_data: list[dict] = []

    async with async_playwright() as p:
        # Resolve storage state (cookies)
        storage_state_path = os.path.join(output_dir, "state.json")
        if not os.path.exists(storage_state_path):
            fallback_path = "/app/data/state.json" if os.path.exists("/app/data") else "data/state.json"
            storage_state_path = fallback_path if os.path.exists(fallback_path) else None
        if storage_state_path:
            logger.info(f"🔑 Loading active browser session state from: {storage_state_path}")

        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False),
            storage_state=storage_state_path,
        )
        page = await context.new_page()

        try:
            # ------------------------------------------------------------------
            # 1. Initial page load
            # ------------------------------------------------------------------
            logger.info(f"Navigating to {start_url}...")
            await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("networkidle")

            # Dismiss cookie consent if present
            try:
                cookie_btn = page.locator('button.accept-btn')
                await cookie_btn.wait_for(state="visible", timeout=5000)
                logger.info("Cookie consent modal detected. Dismissing it...")
                await cookie_btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)
            except Exception:
                logger.info("No cookie consent modal detected or it was auto‑dismissed.")

            # ------------------------------------------------------------------
            # 2. Set pagination to 100 items per page
            # ------------------------------------------------------------------
            try:
                logger.info("Configuring pagination to 100 items per page...")
                dropdown = page.locator('div.ant-select[aria-label="How many results to show in list"]').first
                if await dropdown.count() > 0:
                    await dropdown.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)

                    option = page.locator('.ant-select-item-option-content:has-text("100 per page")').first
                    if await option.count() == 0:
                        option = page.locator('.ant-select-item-option:has-text("100 per page")').first
                    if await option.count() == 0:
                        option = page.locator('[title="100 per page"]').first
                    if await option.count() > 0:
                        await option.click()
                        logger.info("Successfully selected '100 per page' option.")
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)
                    else:
                        logger.warning("Could not find '100 per page' option in the dropdown list.")
                else:
                    logger.warning("Could not locate the pagination dropdown on the page.")
            except Exception as pag_err:
                logger.warning(f"Could not configure pagination: {pag_err}. Proceeding with default pagination.")

            # ------------------------------------------------------------------
            # 3. Discover available years from the Publication Year sidebar
            #
            # The sidebar shows every year with a non-zero entry count; we use
            # these counts only for informational logging – the actual scraping
            # is driven by month windows regardless of year count.
            # ------------------------------------------------------------------
            logger.info("Discovering available years from the Publication Year sidebar...")
            year_entries: list[tuple[int, int]] = []
            try:
                raw_years = await page.evaluate(r"""
                    () => {
                        const listbox = document.querySelector('ul[aria-label="Year-items"]');
                        if (!listbox) return [];
                        const results = [];
                        listbox.querySelectorAll('li').forEach(li => {
                            const label = li.querySelector('label div');
                            if (!label) return;
                            // First text node is the year number
                            const yearText = (label.childNodes[0]?.textContent || '').trim();
                            const countSpan = label.querySelector('span');
                            const countText = countSpan
                                ? countSpan.textContent.replace(/[(),\s]/g, '')
                                : '0';
                            const year = parseInt(yearText, 10);
                            const count = parseInt(countText, 10);
                            if (!isNaN(year) && !isNaN(count) && count > 0) {
                                results.push([year, count]);
                            }
                        });
                        return results;
                    }
                """)
                year_entries = [(int(y), int(c)) for y, c in raw_years]
                year_entries.sort(key=lambda x: x[0])  # oldest first
                logger.info(
                    f"Found {len(year_entries)} years with data: "
                    + ", ".join(f"{y}({c})" for y, c in year_entries)
                )
            except Exception as ye:
                logger.warning(f"Could not discover years from sidebar: {ye}. Falling back to current year only.")
                year_entries = [(datetime.now().year, 1)]

            # ------------------------------------------------------------------
            # 4. Rolling window loop: year → month → pages
            # ------------------------------------------------------------------
            total_new = 0

            for year, year_count in year_entries:
                # Skip years we have already fully processed
                if year < resume_year:
                    logger.info(f"Skipping year {year} (already processed per progress state).")
                    continue

                logger.info(f"📅 Processing year {year} (~{year_count} entries)...")

                for month in range(1, 13):
                    # Skip months before the resume point within the first resumed year
                    if year == resume_year and month < resume_month:
                        logger.info(f"  Skipping {year}-{month:02d} (already processed).")
                        continue

                    # Compute date boundaries for this calendar month
                    last_day = calendar.monthrange(year, month)[1]
                    date_from = date(year, month, 1).strftime("%m/%d/%Y")
                    date_to = date(year, month, last_day).strftime("%m/%d/%Y")

                    logger.info(f"  🗓  Window: {date_from} → {date_to}")

                    # Mark this window as in-progress before we start
                    _save_progress(year, month, completed=False)

                    # --------------------------------------------------------
                    # 4a. Open Advanced Search panel and apply the date filter
                    # --------------------------------------------------------
                    try:
                        # Click the "Advanced Search" toggle button
                        adv_btn = page.locator(
                            'button.css-1d0rxin, #search-col-4 button, button:has-text("Advanced Search")'
                        ).first
                        if await adv_btn.count() > 0:
                            # Only click if the date inputs are not already visible
                            date_from_input = page.locator('input[placeholder="Date From"]').first
                            if not await date_from_input.is_visible():
                                await adv_btn.click()
                                await asyncio.sleep(1)
                        else:
                            logger.warning("  Advanced Search button not found – attempting to proceed anyway.")

                        # Fill "Date From"
                        date_from_input = page.locator('input[placeholder="Date From"]').first
                        await date_from_input.click()
                        await date_from_input.click(click_count=3)
                        await date_from_input.fill(date_from)
                        await asyncio.sleep(0.4)
                        # Tab away to close the calendar popover
                        await date_from_input.press("Tab")
                        await asyncio.sleep(0.5)

                        # Fill "Date To"
                        date_to_input = page.locator('input[placeholder="Date To"]').first
                        await date_to_input.click()
                        await date_to_input.click(click_count=3)
                        await date_to_input.fill(date_to)
                        await asyncio.sleep(0.4)
                        await date_to_input.press("Escape")  # dismiss any open calendar
                        await asyncio.sleep(0.3)

                        # Click the Search button inside the Advanced Search panel
                        search_btn = page.locator(
                            'button.btn-search, button.ant-btn.btn-search, button.ant-btn:has-text("Search")'
                        ).first
                        await search_btn.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)

                    except Exception as adv_err:
                        logger.warning(
                            f"  Could not apply date filter for {date_from}→{date_to}: {adv_err}. "
                            "Skipping window."
                        )
                        _save_progress(year, month, completed=True)
                        continue

                    # --------------------------------------------------------
                    # 4b. Check whether this window has any results at all
                    # --------------------------------------------------------
                    try:
                        await page.wait_for_selector('.ant-list-item', timeout=10000)
                    except Exception:
                        logger.info(f"  No results for window {date_from}→{date_to}. Moving on.")
                        _save_progress(year, month, completed=True)
                        continue

                    # --------------------------------------------------------
                    # 4c. Paginate through all pages within this window
                    # --------------------------------------------------------
                    current_page = 1
                    window_new = 0

                    while True:
                        logger.info(f"    Page {current_page} of window {date_from}→{date_to}...")

                        try:
                            await page.wait_for_selector('.ant-list-item', timeout=15000)
                        except Exception:
                            logger.warning(f"    No items on page {current_page}. Ending window.")
                            break

                        page_items: list[dict] = await page.evaluate(_EXTRACT_ITEMS_JS)
                        logger.info(f"    Extracted {len(page_items)} items.")

                        for item_data in page_items:
                            url = item_data.get("detail_url")
                            if url and url in existing_urls:
                                # Already have this record – skip silently
                                continue
                            extracted_data.append(item_data)
                            if url:
                                existing_urls.add(url)
                            window_new += 1

                        # Incremental checkpoint every 500 new records across all windows
                        total_so_far = total_new + window_new
                        if window_new > 0 and total_so_far % 500 < len(page_items):
                            combined = extracted_data + existing_data
                            with open(output_file, "w", encoding="utf-8") as f:
                                json.dump(combined, f, indent=4, ensure_ascii=False)
                            logger.info(f"    💾 Incremental save: {len(combined)} total records.")

                        # Navigate to the next page within this window
                        next_btn = page.locator('li.ant-pagination-next:not(.ant-pagination-disabled) a').first
                        if await next_btn.count() == 0:
                            next_btn = page.locator('a[rel="next"]').first
                        if await next_btn.count() == 0:
                            next_btn = page.locator('a:has-text("Next")').first

                        if await next_btn.count() > 0:
                            disabled = await next_btn.get_attribute("disabled")
                            link_classes = await next_btn.get_attribute("class") or ""
                            parent_classes = await next_btn.evaluate(
                                "el => el.closest('li')?.className || ''"
                            )
                            is_disabled = (
                                disabled is not None
                                or "disabled" in link_classes.lower()
                                or "disabled" in parent_classes.lower()
                            )
                            if is_disabled:
                                logger.info("    Pagination next button is disabled. Last page of window.")
                                break
                            # Brief long pause every 100 pages to avoid rate limiting
                            if current_page % 100 == 0:
                                logger.info(
                                    f"    Scraped {current_page} pages. Sleeping 60 s to avoid rate limiting..."
                                )
                                await asyncio.sleep(60)
                            await next_btn.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)
                            current_page += 1
                        else:
                            logger.info("    Next button not found. End of window.")
                            break

                    total_new += window_new
                    logger.info(
                        f"  ✅ Window {date_from}→{date_to}: {window_new} new records "
                        f"(running total: {total_new})."
                    )
                    _save_progress(year, month, completed=True)

                    # Short pause between windows to be polite to the server
                    await asyncio.sleep(3)

            # ------------------------------------------------------------------
            # 5. Final save
            # ------------------------------------------------------------------
            combined_data = extracted_data + existing_data
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(combined_data, f, indent=4, ensure_ascii=False)
            logger.info(
                f"✅ Extraction completed. Saved {len(combined_data)} total records "
                f"({total_new} new) to {output_file}."
            )

            # Mark the entire run as fully complete in the progress state
            progress_state["fully_complete"] = True
            progress_state["completed_at"] = datetime.now().isoformat()
            with open(progress_file, "w", encoding="utf-8") as pf:
                json.dump(progress_state, pf, indent=2)

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("Browser closed. Run complete.")

# -----------------------------------------------------------------------------
# Detail Scraper – enriches each record with full page content
# -----------------------------------------------------------------------------
async def run_detail_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)

    # Resolve base directory (container vs local)
    base_dir = "/app/data"
    if not os.path.exists(base_dir) and not os.path.exists("/app"):
        base_dir = "data"

    doc_type = config.get("document_type", "awards").lower()

    # Identify the index pipeline name (defaults to current name without _details suffix)
    import re
    extraction_params = config.get("extraction_params") or {}
    index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', pipeline_name)

    input_file = os.path.join(base_dir, index_pipeline_name, doc_type, f"{index_pipeline_name}.json")
    output_file = os.path.join(base_dir, index_pipeline_name, doc_type, f"{index_pipeline_name}_details.json")

    logger.info("==================================================")
    logger.info(f"🚀 SABINET DETAIL SCRAPER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Index Pipeline Source: {index_pipeline_name}")
    logger.info(f"Input Index File: {input_file}")
    logger.info(f"Output Details File: {output_file}")
    logger.info("==================================================")

    if not os.path.exists(input_file):
        logger.error(f"❌ Input index file {input_file} not found. Please run the index scraper first.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        cases = json.load(f)
    logger.info(f"Loaded {len(cases)} cases from index.")

    existing_details = []
    scraped_urls = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_details = json.load(f)
            for item in existing_details:
                if item.get("detail_url"):
                    scraped_urls.add(item["detail_url"])
            logger.info(f"Loaded {len(existing_details)} already scraped details. Skipping these on this run.")
        except Exception as e:
            logger.warning(f"Could not read existing details file: {e}. Starting fresh details scrape.")

    pending_cases = [c for c in cases if c.get("detail_url") and c["detail_url"] not in scraped_urls]
    logger.info(f"Found {len(pending_cases)} pending cases to scrape.")

    if not pending_cases:
        logger.info("✅ All cases are already scraped. Exiting.")
        return

    async with async_playwright() as p:
        # Resolve storage state for auth – same logic as the index scraper
        storage_state_path = os.path.join(base_dir, index_pipeline_name, doc_type, "state.json")
        if not os.path.exists(storage_state_path):
            fallback_path = os.path.join(base_dir, "state.json")
            storage_state_path = fallback_path if os.path.exists(fallback_path) else None
        if storage_state_path:
            logger.info(f"🔑 Loading active browser session state from: {storage_state_path}")

        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False),
            storage_state=storage_state_path,
        )
        page = await context.new_page()

        try:
            count = 0
            for case_item in pending_cases:
                url = case_item["detail_url"]
                logger.info(f"[{count + 1}/{len(pending_cases)}] Scraping details from: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_load_state("networkidle")

                    # Occasionally dismiss cookie consent
                    if count == 0 or count % 50 == 0:
                        try:
                            cookie_btn = page.locator('button.accept-btn').first
                            if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                                await cookie_btn.click()
                                await page.wait_for_load_state("networkidle")
                        except Exception:
                            pass

                    page_info = await page.evaluate(r'''
                        () => {
                            const h1El = document.querySelector('h1');
                            // Expand any hidden sections
                            const expandButtons = document.querySelectorAll('.ant-typography-expand');
                            expandButtons.forEach(btn => { try { btn.click(); } catch(e) {} });

                            const metadata = {};
                            const rows = document.querySelectorAll('.ant-row');
                            rows.forEach(row => {
                                const labelEl = row.querySelector('.metaDataLabel');
                                const valueEl = row.querySelector('.metaDataValue');
                                if (labelEl && valueEl) {
                                    const labelText = labelEl.innerText.trim();
                                    if (!labelText) return;
                                    let value = "";
                                    const listItems = Array.from(valueEl.querySelectorAll('.ant-list-items .ant-list-item'));
                                    if (listItems.length > 0) {
                                        value = listItems.map(li => li.innerText.trim()).filter(Boolean);
                                    } else {
                                        const ellipsisEl = valueEl.querySelector('.ant-typography-ellipsis[aria-label]');
                                        if (ellipsisEl) {
                                            value = ellipsisEl.getAttribute('aria-label').trim();
                                        } else {
                                            value = valueEl.innerText.trim();
                                        }
                                    }
                                    const key = labelText
                                        .toLowerCase()
                                        .replace(/[^a-z0-9_]/g, '_')
                                        .replace(/_+/g, '_')
                                        .trim()
                                        .replace(/^_+|_+$/g, '');
                                    if (key) {
                                        metadata[key] = value;
                                    }
                                }
                            });

                            const mainEl = document.querySelector('#main-content, .item-details, .item-content-loaded');
                            const bodyText = document.body ? document.body.innerText.trim().substring(0, 3000) : "";

                            return {
                                h1: h1El ? h1El.innerText.trim() : "",
                                main_content: mainEl ? mainEl.innerText.trim() : "",
                                raw_preview_text: bodyText,
                                metadata: metadata,
                            };
                        }
                    ''')

                    detail_record = {
                        **case_item,
                        "extracted_h1": page_info.get("h1"),
                        "extracted_main_content": page_info.get("main_content"),
                        "raw_preview_text": page_info.get("raw_preview_text"),
                        **page_info.get("metadata", {}),
                        "scraped_at": datetime.now(),
                    }
                    existing_details.append(detail_record)
                    count += 1

                    # Incremental checkpoint every 10 records
                    if count % 10 == 0:
                        with open(output_file, "w", encoding="utf-8") as f:
                            json.dump(existing_details, f, indent=4, ensure_ascii=False)
                        logger.info(f"Saved {count} records incrementally to {output_file}.")

                    await asyncio.sleep(2)  # basic throttling
                    if count % 100 == 0:
                        logger.info("Scraped 100 details pages. Throttling: sleeping for 60 seconds...")
                        await asyncio.sleep(60)
                except Exception as page_err:
                    logger.error(f"Failed to scrape detail page {url}: {page_err}")
                    await asyncio.sleep(5)

            # Final save
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(existing_details, f, indent=4, ensure_ascii=False)
            logger.info(f"✅ Finished! Successfully scraped {count} new details records. Saved to {output_file}.")
        except Exception as e:
            logger.error(f"❌ Detail scraper failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("Browser closed. Run complete.")

# -----------------------------------------------------------------------------
# CLI entry point – supports sub‑commands for each stage
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Sabinet Unified Scraper")
    parser.add_argument(
        "stage",
        nargs="?",
        choices=["auth", "index", "details"],
        help="Which part of the workflow to execute: auth generates session state, index scrapes the list, details enriches records.",
    )
    parser.add_argument(
        "--pipeline_name",
        required=False,
        help="Name of the pipeline configuration (required for index and details stages).",
    )

    args = parser.parse_args()

    stage = args.stage
    if not stage:
        pipeline_name = args.pipeline_name or ""
        if "detail" in pipeline_name.lower():
            stage = "details"
        else:
            stage = "index"
        logger.info(f"Auto-detected stage '{stage}' from pipeline name '{pipeline_name}'")

    if stage == "auth":
        asyncio.run(generate_state())
    elif stage == "index":
        if not args.pipeline_name:
            logger.error("--pipeline_name is required for the index stage.")
            sys.exit(1)
        asyncio.run(run_extraction(args.pipeline_name))
        logger.info("Index extraction complete. Starting detail extraction stage...")
        asyncio.run(run_detail_extraction(args.pipeline_name))
    elif stage == "details":
        if not args.pipeline_name:
            logger.error("--pipeline_name is required for the details stage.")
            sys.exit(1)
        asyncio.run(run_detail_extraction(args.pipeline_name))
