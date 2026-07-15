import argparse
import asyncio
import calendar
import json
import os
import sys
from datetime import datetime, date
from playwright.async_api import async_playwright

from .utils.utils import fetch_pipeline_config, resolve_data_dir
from .db import get_db_connection
from . import db_storage
from .utils.browser_helper import (
    setup_logger,
    dismiss_cookie_consent,
    BrowserManager,
)

logger = setup_logger(__name__)

# -----------------------------------------------------------------------------
# Authentication / Session State Generation
# -----------------------------------------------------------------------------
async def generate_state(pipeline_name: str = "sabinet_ccma", headless: bool = False):
    """Automatically log in to Sabinet and persist the browser session state to the database.

    Uses Playwright to perform a fully automated login flow:
    1. Navigate to the CCMA Awards search page.
    2. Dismiss the cookie consent banner (if present).
    3. Click the "Sign in" button in the header.
    4. Fill in the username and password in the sidebar drawer.
    5. Click "Sign into Account" and wait for successful authentication.

    The function saves the storage state JSON directly to the database.
    """
    scrape_url = (
        "https://discover.sabinet.co.za/search?"
        "Search=&ProductType=ccmabargainingcouncilawards"
        "&resultsortOption=%22Date+Oldest+first%22"
    )
    username = "TiaanF"
    password = "G7fR7Bzv4$@ea5!"

    async with async_playwright() as p:
        logger.info("[INFO] Launching Chromium browser for automated authentication...")
        manager = BrowserManager(
            p,
            headless=headless,
            viewport={"width": 1280, "height": 800},
        )
        async with manager:
            page = manager.page
            context = manager.context

            # ------------------------------------------------------------------
            # Step 1: Navigate to the scrape URL
            # ------------------------------------------------------------------
            logger.info(f"💡 Navigating to: {scrape_url}")
            await page.goto(scrape_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("networkidle")

            # ------------------------------------------------------------------
            # Step 2: Dismiss cookie consent banner (if present)
            # ------------------------------------------------------------------
            await dismiss_cookie_consent(page)

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
            # Persist the session state to the database
            # ------------------------------------------------------------------
            state_dict = await context.storage_state()
            conn = await get_db_connection()
            try:
                progress_state = await db_storage.load_pipeline_state(conn, pipeline_name)
                progress_state["storage_state"] = state_dict
                await db_storage.save_pipeline_state(conn, pipeline_name, progress_state)
                logger.info(f"✅ Session state saved to database for pipeline '{pipeline_name}'.")
            finally:
                await conn.close()

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

    output_dir = resolve_data_dir(pipeline_name, config.get("document_type", "awards"))

    logger.info("==================================================")
    logger.info(f"🚀 COEUS SABINET WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {start_url}")
    logger.info("==================================================")

    # Extract custom parameters from extraction_params
    extraction_params = config.get("extraction_params") or {}
    reverse_direction = extraction_params.get("reverse_direction", False)
    db_record_type = extraction_params.get("shared_record_type") or pipeline_name

    # -------------------------------------------------------------------------
    # Establish DB Connection and Resolve target/deduplication URLs/progress
    # -------------------------------------------------------------------------
    conn = await get_db_connection()
    entity_name = config.get("name") or pipeline_name
    target_name = config.get("subset") or "CCMA Awards"

    logger.info(f"Resolving entity='{entity_name}' and target='{target_name}'...")
    target_id = await db_storage.resolve_target_id(
        conn, entity_name, target_name, start_url
    )

    existing_urls = await db_storage.get_existing_urls(conn, db_record_type)
    logger.info(f"Loaded {len(existing_urls)} unique URLs from database.")

    existing_case_numbers = await db_storage.get_existing_case_numbers(conn, db_record_type)
    logger.info(f"Loaded {len(existing_case_numbers)} unique case numbers from database.")

    progress_state = await db_storage.load_pipeline_state(conn, pipeline_name)
    logger.info(f"Loaded progress state: {progress_state}")

    if progress_state.get("fully_complete"):
        logger.info("✅ Index stage was already marked as fully complete. Skipping index stage.")
        return

    async def save_progress(year: int, month: int, completed: bool = False) -> None:
        progress_state["last_year"] = year
        progress_state["last_month"] = month
        progress_state["last_completed"] = completed
        await db_storage.save_pipeline_state(conn, pipeline_name, progress_state)

    # Determine the resume point
    resume_year: int = progress_state.get("last_year", 0)
    resume_month: int = progress_state.get("last_month", 0)
    last_completed: bool = progress_state.get("last_completed", True)

    # If the last window completed cleanly, advance past it
    if last_completed and resume_year > 0:
        if reverse_direction:
            resume_month -= 1
            if resume_month < 1:
                resume_month = 12
                resume_year -= 1
        else:
            resume_month += 1
            if resume_month > 12:
                resume_month = 1
                resume_year += 1

    p = await async_playwright().start()
    db_storage_state = progress_state.get("storage_state")

    manager = BrowserManager(
        p,
        headless=True,
        ignore_https_errors=config.get("allow_insecure_https", False),
        storage_state=db_storage_state,
    )

    async def setup_search_page(page):
        logger.info(f"Navigating to {start_url}...")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle")

        # Dismiss cookie consent if present
        await dismiss_cookie_consent(page)

        # ------------------------------------------------------------------
        # Set pagination to 100 items per page
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

    page = await manager.start()
    try:
            # ------------------------------------------------------------------
            # 1. Initial page load and setup
            # ------------------------------------------------------------------
            await setup_search_page(page)

            # ------------------------------------------------------------------
            # 3. Discover available years from the Publication Year sidebar
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
                year_entries.sort(key=lambda x: x[0], reverse=reverse_direction)  # chronological direction
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
                # Skip years we have already fully processed according to progress_state
                if reverse_direction:
                    if resume_year > 0 and year > resume_year:
                        logger.info(f"Skipping year {year} (already processed per progress state).")
                        continue
                else:
                    if resume_year > 0 and year < resume_year:
                        logger.info(f"Skipping year {year} (already processed per progress state).")
                        continue

                # Database-backed check to see if the year is already indexed
                try:
                    db_count = await conn.fetchval(
                        """
                        SELECT COUNT(*) FROM extracted_records
                        WHERE record_type = $1
                          AND LEFT(COALESCE(data->>'award_date', data->>'date', data->>'publication_date', data->>'document_date'), 4) = $2
                        """,
                        db_record_type,
                        str(year),
                    )
                    is_fully_indexed = False
                    if db_count >= year_count:
                        is_fully_indexed = True
                    elif year_count > 10 and db_count >= year_count * 0.99:
                        is_fully_indexed = True
                    elif year_count <= 10 and db_count >= year_count - 1:
                        is_fully_indexed = True

                    if is_fully_indexed:
                        logger.info(f"Skipping year {year} (already fully indexed in DB: {db_count}/{year_count} records).")
                        await save_progress(year, 1 if reverse_direction else 12, completed=True)
                        continue
                except Exception as db_cnt_err:
                    logger.warning(f"Failed to check DB record count for year {year}: {db_cnt_err}")

                logger.info(f"📅 Processing year {year} (~{year_count} entries)...")

                months = list(range(12, 0, -1)) if reverse_direction else list(range(1, 13))
                for month in months:
                    # Skip months before/after the resume point within the first resumed year
                    if year == resume_year:
                        if reverse_direction:
                            if month > resume_month:
                                logger.info(f"  Skipping {year}-{month:02d} (already processed).")
                                continue
                        else:
                            if month < resume_month:
                                logger.info(f"  Skipping {year}-{month:02d} (already processed).")
                                continue

                    # Compute date boundaries for this calendar month
                    last_day = calendar.monthrange(year, month)[1]
                    date_from = date(year, month, 1).strftime("%m/%d/%Y")
                    date_to = date(year, month, last_day).strftime("%m/%d/%Y")

                    logger.info(f"  🗓  Window: {date_from} → {date_to}")

                    # Mark this window as in-progress before we start
                    await save_progress(year, month, completed=False)

                    # Recycle browser to prevent memory leaks / OOM
                    logger.info(f"Recycling browser for month {year}-{month:02d}...")
                    await manager.recycle()
                    await setup_search_page(manager.page)
                    page = manager.page  # Update local reference to the new page

                    # --------------------------------------------------------
                    # 4a. Open Advanced Search panel and apply the date filter
                    # --------------------------------------------------------
                    try:
                        # Ensure the Advanced Search panel is open each iteration.
                        # The panel can collapse after a search result is rendered,
                        # so we check for the Date From input visibility every time.
                        date_from_input = page.locator('input[placeholder="Date From"]').first
                        if not await date_from_input.is_visible():
                            # Try the toggle button – prefer text-based role over a
                            # brittle CSS hash class so it survives style recompiles.
                            adv_btn = page.locator(
                                'button:has-text("Advanced Search"), #search-col-4 button'
                            ).first
                            if await adv_btn.count() > 0:
                                await adv_btn.click()
                                # Wait until the date input actually appears
                                try:
                                    await date_from_input.wait_for(state="visible", timeout=8000)
                                except Exception:
                                    logger.warning("  Advanced Search panel did not open. Skipping window.")
                                    await save_progress(year, month, completed=True)
                                    continue
                            else:
                                logger.warning("  Advanced Search button not found – attempting to proceed anyway.")

                        # Fill "Date From"
                        await date_from_input.click()
                        await date_from_input.click(click_count=3)
                        await date_from_input.fill(date_from)
                        await asyncio.sleep(0.4)
                        # Tab away to close the calendar popover
                        await date_from_input.press("Tab")
                        await asyncio.sleep(0.5)

                        # Fill "Date To" – use Tab (not Escape) to dismiss the
                        # calendar popover without collapsing the Advanced Search panel.
                        date_to_input = page.locator('input[placeholder="Date To"]').first
                        await date_to_input.click()
                        await date_to_input.click(click_count=3)
                        await date_to_input.fill(date_to)
                        await asyncio.sleep(0.4)
                        await date_to_input.press("Tab")  # close calendar, keep panel open
                        await asyncio.sleep(0.3)

                        # Use JS to click the first *actually visible* .btn-search.
                        # Playwright's locator-based click fails when multiple .btn-search
                        # elements exist (one in the main bar, one in the Advanced Search
                        # panel) and the first match happens to be hidden/animating.
                        clicked = await page.evaluate("""
                            () => {
                                const btn = [...document.querySelectorAll('.btn-search')].find(el => {
                                    const style = window.getComputedStyle(el);
                                    const rect = el.getBoundingClientRect();
                                    return (
                                        style.display !== 'none' &&
                                        style.visibility !== 'hidden' &&
                                        parseFloat(style.opacity) > 0 &&
                                        rect.width > 0 && rect.height > 0
                                    );
                                });
                                if (btn) { btn.click(); return true; }
                                return false;
                            }
                        """)
                        if not clicked:
                            raise RuntimeError("No visible .btn-search found after filling dates")
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)

                    except Exception as adv_err:
                        logger.warning(
                            f"  Could not apply date filter for {date_from}→{date_to}: {adv_err}. "
                            "Skipping window."
                        )
                        # Do NOT mark as completed – leave completed=False so the
                        # next run retries this window instead of skipping past it.
                        await save_progress(year, month, completed=False)
                        continue

                    # --------------------------------------------------------
                    # 4b. Check whether this window has any results at all
                    # --------------------------------------------------------
                    try:
                        await page.wait_for_selector('.ant-list-item', timeout=10000)
                    except Exception:
                        logger.info(f"  No results for window {date_from}→{date_to}. Moving on.")
                        await save_progress(year, month, completed=True)
                        continue

                    # --------------------------------------------------------
                    # Check if the last entry in this window is already complete in DB
                    # --------------------------------------------------------
                    is_complete = False
                    last_page_num = 1
                    try:
                        last_page_locator = page.locator('li.ant-pagination-item').last
                        if await last_page_locator.count() > 0:
                            last_page_text = await last_page_locator.inner_text()
                            try:
                                last_page_num = int(last_page_text.strip())
                            except ValueError:
                                last_page_num = 1

                            if last_page_num > 1:
                                logger.info(f"    Multiple pages ({last_page_num}) detected. Checking the last page for completion...")
                                await last_page_locator.click()
                                await page.wait_for_load_state("networkidle")
                                await asyncio.sleep(2)

                        current_page_items: list[dict] = await page.evaluate(_EXTRACT_ITEMS_JS)
                        if current_page_items:
                            last_item = current_page_items[-1]
                            last_url = last_item.get("detail_url")
                            last_case_no = last_item.get("case_number")

                            is_complete = await db_storage.is_record_complete(
                                conn, db_record_type, last_url, last_case_no
                            )
                    except Exception as last_check_err:
                        logger.warning(f"    Failed last entry check: {last_check_err}. Proceeding with normal scrape.")

                    if is_complete:
                        logger.info(f"    ✅ Last entry is already complete. Skipping window {date_from}→{date_to}.")
                        await save_progress(year, month, completed=True)
                        continue
                    else:
                        # If we navigated to the last page and it's not complete, go back to Page 1
                        if last_page_num > 1:
                            try:
                                logger.info("    Navigating back to Page 1...")
                                jumped_back = False
                                first_page_locator = page.locator('li.ant-pagination-item-1, li.ant-pagination-item').first
                                if await first_page_locator.count() > 0:
                                    await first_page_locator.click()
                                    await page.wait_for_load_state("networkidle")
                                    await asyncio.sleep(2)
                                    jumped_back = True
                                
                                if not jumped_back:
                                    logger.info("    Could not click page 1 button, re-submitting search to reset to Page 1...")
                                    await page.evaluate("""
                                        () => {
                                            const btn = [...document.querySelectorAll('.btn-search')].find(el => {
                                                const style = window.getComputedStyle(el);
                                                const rect = el.getBoundingClientRect();
                                                return (
                                                    style.display !== 'none' &&
                                                    style.visibility !== 'hidden' &&
                                                    parseFloat(style.opacity) > 0 &&
                                                    rect.width > 0 && rect.height > 0
                                                );
                                            });
                                            if (btn) btn.click();
                                        }
                                    """)
                                    await page.wait_for_load_state("networkidle")
                                    await asyncio.sleep(2)
                            except Exception as reset_err:
                                logger.warning(f"    Failed to reset pagination to Page 1: {reset_err}. Proceeding anyway.")


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

                        new_items = []
                        for item_data in page_items:
                            url = item_data.get("detail_url")
                            case_no = item_data.get("case_number")

                            is_existing = False
                            if url and url in existing_urls:
                                is_existing = True
                            elif case_no and case_no in existing_case_numbers:
                                is_existing = True

                            if is_existing:
                                # Already have this record – skip silently
                                continue

                            item_data["index_scraped_at"] = datetime.now().isoformat()
                            new_items.append(item_data)
                            if url:
                                existing_urls.add(url)
                            if case_no:
                                existing_case_numbers.add(case_no)
                            window_new += 1

                        if new_items:
                            await db_storage.upsert_scraped_records_batch(
                                conn, target_id, db_record_type, new_items, "detail_url", status="indexed"
                            )
                            logger.info(f"    Saved {len(new_items)} new items to database.")

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
                    await save_progress(year, month, completed=True)

                    # Short pause between windows to be polite to the server
                    await asyncio.sleep(3)

            # ------------------------------------------------------------------
            # 5. Final save (Pipeline state only)
            # ------------------------------------------------------------------
            logger.info(
                f"✅ Extraction completed. Scraped {total_new} new records in total."
            )

            # Mark the entire run as fully complete in the progress state
            progress_state["fully_complete"] = True
            progress_state["completed_at"] = datetime.now().isoformat()
            await db_storage.save_pipeline_state(conn, pipeline_name, progress_state)

    except Exception as e:
        logger.error(f"❌ Playwright extraction failed: {str(e)}")
        sys.exit(1)
    finally:
        await manager.close()
        await p.stop()

# -----------------------------------------------------------------------------
# Detail Scraper – enriches each record with full page content
# -----------------------------------------------------------------------------
async def run_detail_extraction(pipeline_name: str):
    """Enrich each case in Postgres with detailed metadata from its detail page.

    This function reads the pending records for the given pipeline from the database,
    visits each case's ``detail_url``, extracts structured metadata from the
    ``paywall-content item-content-loaded`` div, and writes the enriched data back
    to the database.
    """
    config = await fetch_pipeline_config(pipeline_name)

    doc_type = config.get("document_type", "awards").lower()

    # Identify the index pipeline name (defaults to current name without _details suffix)
    import re
    extraction_params = config.get("extraction_params") or {}
    reverse_direction = extraction_params.get("reverse_direction", False)
    index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', pipeline_name)

    logger.info("==================================================")
    logger.info(f"🚀 SABINET DETAIL SCRAPER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Index Pipeline Source: {index_pipeline_name}")
    logger.info("==================================================")

    conn = await get_db_connection()

    # Get total and completed counts for logging progress
    total_count = await conn.fetchval(
        """
        SELECT COUNT(*) FROM extracted_records
        WHERE record_type = $1 AND source_url IS NOT NULL
        """,
        index_pipeline_name
    )
    completed_count = await conn.fetchval(
        """
        SELECT COUNT(*) FROM extracted_records
        WHERE record_type = $1
          AND source_url IS NOT NULL
          AND (status = 'detailed' OR (status IS NULL AND (data->>'details_scraped_at') IS NOT NULL))
        """,
        index_pipeline_name
    )

    cases = await db_storage.load_records_needing_detail(
        conn, index_pipeline_name, sort_desc=reverse_direction
    )
    logger.info(
        f"Loaded {len(cases)} pending cases from database "
        f"(total: {total_count}, completed: {completed_count})."
    )

    if not cases:
        logger.info("✅ No cases with detail URLs to enrich. Exiting.")
        return

    # Load progress state to retrieve the storage state
    progress_state = await db_storage.load_pipeline_state(conn, index_pipeline_name)

    # JS snippet that extracts structured metadata from the paywall-content div.
    # Returns an object with:
    #   - auth_ok: whether the paywall-content class is present (auth valid)
    #   - content_loaded: whether item-content-loaded is present at all
    #   - metadata: dict of label→value pairs from the metaDataLabel/metaDataValue rows
    _EXTRACT_DETAIL_JS = r'''
        () => {
            const result = { auth_ok: false, content_loaded: false, metadata: {} };

            // Look for the content div
            const paywallDiv = document.querySelector('div.paywall-content.item-content-loaded');
            const contentOnlyDiv = document.querySelector('div.item-content-loaded');

            if (paywallDiv) {
                // Auth is valid – paywall-content class is present
                result.auth_ok = true;
                result.content_loaded = true;
            } else if (contentOnlyDiv) {
                // item-content-loaded exists but WITHOUT paywall-content → auth expired
                result.auth_ok = false;
                result.content_loaded = true;
                return result;
            } else {
                // Neither div found – page may not have loaded properly
                result.auth_ok = false;
                result.content_loaded = false;
                return result;
            }

            // Expand any truncated text sections
            const expandButtons = paywallDiv.querySelectorAll('.ant-typography-expand');
            expandButtons.forEach(btn => { try { btn.click(); } catch(e) {} });

            // Extract the title from the h1 inside the paywall div
            const h1El = paywallDiv.querySelector('h1');
            if (h1El) {
                // Clone and remove icon elements to get clean title text
                const clone = h1El.cloneNode(true);
                const icons = clone.querySelectorAll('.anticon, [role="img"]');
                icons.forEach(icon => icon.remove());
                const titleText = clone.innerText.trim();
                if (titleText) {
                    result.metadata['detail_title'] = titleText;
                }
            }

            // Extract all label/value metadata pairs from ant-row elements
            const rows = paywallDiv.querySelectorAll('.ant-row');
            rows.forEach(row => {
                const labelEl = row.querySelector('.metaDataLabel');
                const valueEl = row.querySelector('.metaDataValue');
                if (!labelEl || !valueEl) return;

                const labelText = labelEl.innerText.trim();
                if (!labelText) return;

                let value = "";
                // Check for list-type values (e.g. Forum, Court Location)
                const listItems = Array.from(
                    valueEl.querySelectorAll('.ant-list-items .ant-list-item')
                );
                if (listItems.length > 0) {
                    value = listItems.map(li => li.innerText.trim()).filter(Boolean);
                    // If single-element list, unwrap to a plain string
                    if (value.length === 1) value = value[0];
                } else {
                    // Check for ellipsis elements with aria-label (full text)
                    const ellipsisEl = valueEl.querySelector(
                        '.ant-typography-ellipsis[aria-label]'
                    );
                    if (ellipsisEl) {
                        value = ellipsisEl.getAttribute('aria-label').trim();
                    } else {
                        value = valueEl.innerText.trim();
                    }
                }

                // Normalise the label into a snake_case key
                const key = labelText
                    .toLowerCase()
                    .replace(/[^a-z0-9_]/g, '_')
                    .replace(/_+/g, '_')
                    .replace(/^_+|_+$/g, '');

                if (key) {
                    result.metadata[key] = value;
                }
            });

            // Extract preview image URL if available
            const previewImg = paywallDiv.querySelector('.ant-image img');
            if (previewImg && previewImg.src) {
                result.metadata['preview_image_url'] = previewImg.src;
            }

            return result;
        }
    '''

    p = await async_playwright().start()
    db_storage_state = progress_state.get("storage_state")

    manager = BrowserManager(
        p,
        headless=True,
        ignore_https_errors=config.get("allow_insecure_https", False),
        storage_state=db_storage_state,
    )
    page = await manager.start()

    try:
            count = 0
            for progress_idx, case_item in enumerate(cases):
                # Recycle browser every 100 pages to avoid memory leaks / OOM
                if progress_idx > 0 and progress_idx % 100 == 0:
                    logger.info("Recycling browser to free memory...")
                    await manager.recycle()

                page = manager.page  # Update local reference to the new page

                record_id = case_item["id"]
                url = case_item["source_url"]
                data_payload = case_item["data"]

                # Quick DB check to see if this record was already enriched by the other worker in the meantime
                current_status = await conn.fetchval(
                    "SELECT status FROM extracted_records WHERE id = $1", record_id
                )
                if current_status == "detailed":
                    logger.info(f"  [-] Skipping (already enriched by another scraper instance): {url}")
                    continue

                logger.info(f"[{completed_count + progress_idx + 1}/{total_count}] Scraping details from: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_load_state("networkidle")

                    # Occasionally dismiss cookie consent
                    if count == 0 or count % 50 == 0:
                        await dismiss_cookie_consent(page)

                    # Wait briefly for the content div to render
                    try:
                        await page.wait_for_selector(
                            'div.item-content-loaded', timeout=15000
                        )
                    except Exception:
                        logger.warning(f"  Content div did not appear for {url}. Skipping.")
                        continue

                    detail_info = await page.evaluate(_EXTRACT_DETAIL_JS)

                    # ---- Auth expiry check ----
                    if detail_info.get("content_loaded") and not detail_info.get("auth_ok"):
                        logger.warning(
                            "🔒 Authentication has expired! Attempting to automatically refresh the session..."
                        )
                        # Run the auth stage in headless mode to refresh the session
                        await generate_state(pipeline_name=index_pipeline_name, headless=True)

                        # Re-load the progress state from the database
                        progress_state = await db_storage.load_pipeline_state(conn, index_pipeline_name)
                        db_storage_state = progress_state.get("storage_state")
                        manager.storage_state = db_storage_state

                        # Recycle browser to load the new session state
                        logger.info("Recycling browser to apply new authentication state...")
                        await manager.recycle()
                        page = manager.page  # Update local page reference

                        # Retry loading the detail page
                        logger.info(f"Retrying detail page: {url}")
                        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_load_state("networkidle")

                        try:
                            await page.wait_for_selector(
                                'div.item-content-loaded', timeout=15000
                            )
                        except Exception:
                            logger.warning(f"  Content div did not appear on retry for {url}. Skipping.")
                            continue

                        detail_info = await page.evaluate(_EXTRACT_DETAIL_JS)
                        if detail_info.get("content_loaded") and not detail_info.get("auth_ok"):
                            logger.error(
                                "🔒 Authentication is still expired after automatic refresh! "
                                "Detail page is missing 'paywall-content'. Exiting."
                            )
                            sys.exit(1)

                    if not detail_info.get("content_loaded"):
                        logger.warning(f"  No content div found on {url}. Skipping.")
                        continue

                    # Merge the extracted metadata into the existing case entry
                    metadata = detail_info.get("metadata", {})
                    for key, value in metadata.items():
                        data_payload[key] = value
                    data_payload["details_scraped_at"] = datetime.now().isoformat()

                    # Save update in-place in Postgres
                    await db_storage.update_record_data(conn, record_id, data_payload, status="detailed")

                    count += 1
                    logger.info(f"  ✅ Enriched with {len(metadata)} fields.")

                    await asyncio.sleep(2)  # basic throttling
                    if count % 100 == 0:
                        logger.info("Scraped 100 details pages. Throttling: sleeping for 60 seconds...")
                        await asyncio.sleep(60)
                except Exception as page_err:
                    logger.error(f"Failed to scrape detail page {url}: {page_err}")
                    await asyncio.sleep(5)

            logger.info(f"✅ Finished! Enriched {count} case records directly in Postgres.")
    except Exception as e:
        logger.error(f"❌ Detail scraper failed: {str(e)}")
        sys.exit(1)
    finally:
        await manager.close()
        await p.stop()

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
        pipeline_name = args.pipeline_name or "sabinet_ccma"
        asyncio.run(generate_state(pipeline_name=pipeline_name))
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
