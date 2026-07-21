import asyncio
import calendar
import re
import sys
from datetime import datetime, date
from typing import Any
from playwright.async_api import async_playwright, Page

from .base_scraper import BaseScraper, setup_logger
from . import db_storage

logger = setup_logger(__name__)

# -----------------------------------------------------------------------------
# JavaScript DOM Extractor Injection
# -----------------------------------------------------------------------------
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

_EXTRACT_DETAIL_JS = r'''
    () => {
        const result = { auth_ok: false, content_loaded: false, metadata: {} };

        const paywallDiv = document.querySelector('div.paywall-content.item-content-loaded');
        const contentOnlyDiv = document.querySelector('div.item-content-loaded');

        if (paywallDiv) {
            result.auth_ok = true;
            result.content_loaded = true;
        } else if (contentOnlyDiv) {
            result.auth_ok = false;
            result.content_loaded = true;
            return result;
        } else {
            result.auth_ok = false;
            result.content_loaded = false;
            return result;
        }

        const expandButtons = paywallDiv.querySelectorAll('.ant-typography-expand');
        expandButtons.forEach(btn => { try { btn.click(); } catch(e) {} });

        const h1El = paywallDiv.querySelector('h1');
        if (h1El) {
            const clone = h1El.cloneNode(true);
            const icons = clone.querySelectorAll('.anticon, [role="img"]');
            icons.forEach(icon => icon.remove());
            const titleText = clone.innerText.trim();
            if (titleText) {
                result.metadata['detail_title'] = titleText;
            }
        }

        const rows = paywallDiv.querySelectorAll('.ant-row');
        rows.forEach(row => {
            const labelEl = row.querySelector('.metaDataLabel');
            const valueEl = row.querySelector('.metaDataValue');
            if (!labelEl || !valueEl) return;

            const labelText = labelEl.innerText.trim();
            if (!labelText) return;

            let value = "";
            const listItems = Array.from(
                valueEl.querySelectorAll('.ant-list-items .ant-list-item')
            );
            if (listItems.length > 0) {
                value = listItems.map(li => li.innerText.trim()).filter(Boolean);
                if (value.length === 1) value = value[0];
            } else {
                const ellipsisEl = valueEl.querySelector(
                    '.ant-typography-ellipsis[aria-label]'
                );
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
                .replace(/^_+|_+$/g, '');

            if (key) {
                result.metadata[key] = value;
            }
        });

        const previewImg = paywallDiv.querySelector('.ant-image img');
        if (previewImg && previewImg.src) {
            result.metadata['preview_image_url'] = previewImg.src;
        }

        return result;
    }
'''


class SabinetScraper(BaseScraper):
    """
    Sabinet CCMA Awards Framework Implementation.
    Handles authentication state capture, rolling-window searches, and indexing.
    """

    def __init__(self, pipeline_name: str):
        super().__init__(pipeline_name)
        self.auth_lock = asyncio.Lock()

    async def authenticate(self, headless: bool = False) -> None:
        """Automatically log in to Sabinet and persist browser session state to the database."""
        scrape_url = (
            "https://discover.sabinet.co.za/search?"
            "Search=&ProductType=ccmabargainingcouncilawards"
            "&resultsortOption=%22Date+Oldest+first%22"
        )
        username = "TiaanF"
        password = "G7fR7Bzv4$@ea5!"

        # Create temporary isolated playwright orchestration framework to flush state down
        async with async_playwright() as p:
            from utils.browser_helper import BrowserManager, dismiss_cookie_consent
            logger.info("[INFO] Launching browser for automated authentication state generation...")
            manager = BrowserManager(p, headless=headless, viewport={"width": 1280, "height": 800})
            async with manager:
                page = manager.page
                context = manager.context

                logger.info(f"💡 Navigating to login gate: {scrape_url}")
                await page.goto(scrape_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_load_state("networkidle")

                await dismiss_cookie_consent(page)

                logger.info("🔐 Triggering security workflow context drawer...")
                sign_in_btn = page.get_by_role("button", name="Sign in login")
                await sign_in_btn.wait_for(state="visible", timeout=15000)
                await sign_in_btn.click()
                await asyncio.sleep(1)

                logger.info("✏️ Filling credentials fields...")
                username_input = page.get_by_role("textbox", name="* Username")
                await username_input.wait_for(state="visible", timeout=10000)
                await username_input.fill(username)

                password_input = page.get_by_role("textbox", name="* Password")
                await password_input.fill(password)

                logger.info("🚀 Submitting authentication payload tokens...")
                await page.get_by_role("button", name="Sign into Account").click()

                logger.info("⏳ Waiting for identity authorization response...")
                await page.get_by_role("button", name="user myDiscover down").wait_for(
                    state="visible", timeout=30000
                )
                logger.info("✅ Authentication validated successfully.")

                state_dict = await context.storage_state()
                self.progress_state["storage_state"] = state_dict
                await db_storage.save_pipeline_state(self.conn, self.pipeline_name, self.progress_state)
                logger.info("✅ Session state successfully flushed to the database instance.")

    async def _setup_search_page(self, page: Page, start_url: str) -> None:
        """Configures cookie conditions and list scaling presentation values."""
        from utils.browser_helper import dismiss_cookie_consent
        logger.info(f"Navigating browser window pointer to footprint: {start_url}")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_load_state("networkidle")

        await dismiss_cookie_consent(page)

        try:
            logger.info("Configuring layout adjustments to 100 entries per page...")
            dropdown = page.locator('div.ant-select[aria-label="How many results to show in list"]').first
            if await dropdown.count() > 0:
                await dropdown.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)

                option = page.locator('.ant-select-item-option-content:has-text("100 per page")').first
                if await option.count() == 0:
                    option = page.locator('.ant-select-item-option-content:has-text("100 per page")').first
                if await option.count() == 0:
                    option = page.locator('[title="100 per page"]').first
                
                if await option.count() > 0:
                    await option.click()
                    logger.info("Successfully selected '100 per page' option.")
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                else:
                    logger.warning("Could not find '100 per page' option in selection lists.")
            else:
                logger.warning("Pagination scaling target options not located on standard viewport.")
        except Exception as pag_err:
            logger.warning(f"Could not configure pagination parameters: {pag_err}. Defaulting scaling.")

    async def indexing(self) -> None:
        """Sub-process A: Extracts records utilizing programmatic rolling timeframe parameters."""
        from utils.browser_helper import BrowserManager
        start_url = (
            self.config.get("start_url")
            or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"
        )
        extraction_params = self.config.get("extraction_params") or {}
        is_fully_complete = self.progress_state.get("fully_complete", False)
        incremental = extraction_params.get("incremental", False) or is_fully_complete

        if incremental:
            logger.info("🔄 Incremental mode active (automatically enabled because full scrape is complete).")

        reverse_direction = extraction_params.get("reverse_direction", False)
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name

        resume_year: int = 0 if incremental else self.progress_state.get("last_year", 0)
        resume_month: int = 0 if incremental else self.progress_state.get("last_month", 0)
        last_completed: bool = self.progress_state.get("last_completed", True)

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

        self.playwright_instance = await async_playwright().start()
        db_storage_state = self.progress_state.get("storage_state")

        self.browser_manager = BrowserManager(
            self.playwright_instance,
            headless=True,
            ignore_https_errors=self.config.get("allow_insecure_https", False),
            storage_state=db_storage_state,
            proxy_url=self.proxy_url if self.use_proxy else None,
        )

        page = await self.browser_manager.start()
        await self._setup_search_page(page, start_url)

        logger.info("Discovering total tracking distributions via sidebar configuration indexes...")
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
                        const yearText = (label.childNodes[0]?.textContent || '').trim();
                        const countSpan = label.querySelector('span');
                        const countText = countSpan ? countSpan.textContent.replace(/[(),\s]/g, '') : '0';
                        const year = parseInt(yearText, 10);
                        const count = parseInt(countText, 10);
                        if (!isNaN(year) && !isNaN(count) && count > 0) results.push([year, count]);
                    });
                    return results;
                }
            """)
            year_entries = [(int(y), int(c)) for y, c in raw_years]
            if incremental:
                current_year = datetime.now().year
                year_entries = [entry for entry in year_entries if entry[0] == current_year]
                if not year_entries:
                    year_entries = [(current_year, 1)]
                logger.info(f"🔄 Incremental mode: Limit scanning to current year {current_year}")
            else:
                year_entries.sort(key=lambda x: x[0], reverse=reverse_direction)
            logger.info("Target timelines loaded: " + ", ".join(f"{y}({c})" for y, c in year_entries))
        except Exception as ye:
            logger.warning(f"Could not read dynamic timeline sidebars: {ye}. Resetting defaults to current year.")
            year_entries = [(datetime.now().year, 1)]

        total_new = 0

        for year, year_count in year_entries:
            if reverse_direction and resume_year > 0 and year > resume_year:
                continue
            elif not reverse_direction and resume_year > 0 and year < resume_year:
                continue

            try:
                db_count = await self.conn.fetchval(
                    """
                    SELECT COUNT(*) FROM extracted_records
                    WHERE record_type = $1
                      AND LEFT(COALESCE(data->>'award_date', data->>'date', data->>'publication_date', data->>'document_date'), 4) = $2
                    """,
                    db_record_type, str(year),
                )
                is_fully_indexed = False
                if db_count >= year_count: is_fully_indexed = True
                elif year_count > 10 and db_count >= year_count * 0.99: is_fully_indexed = True
                elif year_count <= 10 and db_count >= year_count - 1: is_fully_indexed = True

                if is_fully_indexed:
                    logger.info(f"Skipping complete sequence interval for year {year} ({db_count}/{year_count} in DB).")
                    await self.save_progress(year, 1 if reverse_direction else 12, completed=True)
                    continue
            except Exception as db_cnt_err:
                logger.warning(f"Error checking verification levels metrics: {db_cnt_err}")

            logger.info(f"📅 Processing year {year} (~{year_count} entries)...")

            months = list(range(12, 0, -1)) if reverse_direction else list(range(1, 13))
            for month in months:
                if year == resume_year:
                    if reverse_direction and month > resume_month: continue
                    elif not reverse_direction and month < resume_month: continue

                last_day = calendar.monthrange(year, month)[1]
                date_from = date(year, month, 1).strftime("%m/%d/%Y")
                date_to = date(year, month, last_day).strftime("%m/%d/%Y")

                logger.info(f"  🗓 Active timeframe tracking boundary window: {date_from} → {date_to}")
                await self.save_progress(year, month, completed=False)

                await self.browser_manager.recycle()
                await self._setup_search_page(self.browser_manager.page, start_url)
                page = self.browser_manager.page

                try:
                    date_from_input = page.locator('input[placeholder="Date From"]').first
                    if not await date_from_input.is_visible():
                        adv_btn = page.locator('button:has-text("Advanced Search"), #search-col-4 button').first
                        if await adv_btn.count() > 0:
                            await adv_btn.click()
                            await date_from_input.wait_for(state="visible", timeout=8000)

                    await date_from_input.click(click_count=3)
                    await date_from_input.fill(date_from)
                    await date_from_input.press("Tab")
                    await asyncio.sleep(0.4)

                    date_to_input = page.locator('input[placeholder="Date To"]').first
                    await date_to_input.click(click_count=3)
                    await date_to_input.fill(date_to)
                    await date_to_input.press("Tab")
                    await asyncio.sleep(0.3)

                    clicked = await page.evaluate("""
                        () => {
                            const btn = [...document.querySelectorAll('.btn-search')].find(el => {
                                const style = window.getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return (style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity) > 0 && rect.width > 0 && rect.height > 0);
                            });
                            if (btn) { btn.click(); return true; } return false;
                        }
                    """)
                    if not clicked: raise RuntimeError("Search button selection validation state broken or obscured.")
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                except Exception as adv_err:
                    logger.warning(f"Error structuring date filtering parameters: {adv_err}. Skipping window.")
                    await self.save_progress(year, month, completed=False)
                    continue

                try:
                    await page.wait_for_selector('.ant-list-item', timeout=10000)
                except Exception:
                    await self.save_progress(year, month, completed=True)
                    continue

                is_complete = False
                last_page_num = 1
                try:
                    last_page_locator = page.locator('li.ant-pagination-item').last
                    if await last_page_locator.count() > 0:
                        last_page_text = await last_page_locator.inner_text()
                        try: last_page_num = int(last_page_text.strip())
                        except ValueError: last_page_num = 1

                        if last_page_num > 1:
                            await last_page_locator.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)

                    current_page_items = await page.evaluate(_EXTRACT_ITEMS_JS)
                    if current_page_items:
                        last_item = current_page_items[-1]
                        is_complete = await db_storage.is_record_complete(
                            self.conn, db_record_type, last_item.get("detail_url"), last_item.get("case_number")
                        )
                except Exception as last_check_err:
                    logger.warning(f"Terminal boundary state checks error: {last_check_err}")

                if is_complete:
                    logger.info(f"    ✅ Database signatures confirm segment indices match. Skipping validation window.")
                    await self.save_progress(year, month, completed=True)
                    continue
                else:
                    if last_page_num > 1:
                        try:
                            first_page_locator = page.locator('li.ant-pagination-item-1, li.ant-pagination-item').first
                            if await first_page_locator.count() > 0:
                                await first_page_locator.click()
                            else:
                                await page.evaluate("() => { const b = document.querySelector('.btn-search'); if(b) b.click(); }")
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)
                        except Exception as reset_err:
                            logger.warning(f"Failed handling pagination reset: {reset_err}")

                current_page = 1
                window_new = 0
                while True:
                    try:
                        await page.wait_for_selector('.ant-list-item', timeout=15000)
                    except Exception:
                        break

                    page_items = await page.evaluate(_EXTRACT_ITEMS_JS)
                    records_to_upsert = []
                    
                    for item_data in page_items:
                        url = item_data.get("detail_url")
                        case_no = item_data.get("case_number")

                        if url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                            continue

                        item_data["index_scraped_at"] = datetime.now().isoformat()
                        records_to_upsert.append(item_data)
                        if url: self.existing_urls.add(url)
                        if case_no: self.existing_case_numbers.add(case_no)
                        window_new += 1

                    if records_to_upsert:
                        upserted_count = await db_storage.upsert_scraped_records_batch(
                            conn=self.conn, target_id=self.target_id, record_type=db_record_type,
                            records=records_to_upsert, url_key="detail_url", status="indexed"
                        )

                    next_btn = page.locator('li.ant-pagination-next:not(.ant-pagination-disabled) a').first
                    if await next_btn.count() == 0:
                        next_btn = page.locator('a[rel="next"]').first
                    if await next_btn.count() == 0:
                        next_btn = page.locator('a:has-text("Next")').first

                    if await next_btn.count() > 0:
                        disabled = await next_btn.get_attribute("disabled")
                        link_classes = await next_btn.get_attribute("class") or ""
                        parent_classes = await next_btn.evaluate("el => el.closest('li')?.className || ''")
                        is_disabled = (
                            disabled is not None
                            or "disabled" in link_classes.lower()
                            or "disabled" in parent_classes.lower()
                        )
                        if is_disabled:
                            break
                        
                        if current_page % 100 == 0:
                            logger.info(f"    Scraped {current_page} pages. Sleeping 60 s to avoid rate limiting...")
                            await asyncio.sleep(60)
                            
                        await next_btn.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)
                        current_page += 1
                    else:
                        break

                total_new += window_new
                await self.save_progress(year, month, completed=True)
                await asyncio.sleep(3)

        logger.info(f"✅ Indexing complete. Scraped index updates total: {total_new}")
        self.progress_state["fully_complete"] = True
        self.progress_state["completed_at"] = datetime.now().isoformat()
        await db_storage.save_pipeline_state(self.conn, self.pipeline_name, self.progress_state)

    async def detailing(self) -> None:
        """Sub-process B: Asset Enrichment Layer.
        
        Fetches pending document rows from Postgres, checks for auth status, expands layouts,
        and saves complete detail structures directly back into the operational rows.
        """
        from utils.browser_helper import dismiss_cookie_consent, BrowserManager

        # Bootstrap browser if indexing was skipped (e.g. fully_complete state)
        if not self.browser_manager:
            if not self.playwright_instance:
                self.playwright_instance = await async_playwright().start()
            db_storage_state = self.progress_state.get("storage_state")
            self.browser_manager = BrowserManager(
                self.playwright_instance,
                headless=True,
                ignore_https_errors=self.config.get("allow_insecure_https", False),
                storage_state=db_storage_state,
            )
            await self.browser_manager.start()

        extraction_params = self.config.get("extraction_params") or {}
        reverse_direction = extraction_params.get("reverse_direction", False)
        index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', self.pipeline_name)
        db_record_type = extraction_params.get("shared_record_type") or index_pipeline_name

        total_count = await self.conn.fetchval(
            "SELECT COUNT(*) FROM extracted_records WHERE record_type = $1 AND source_url IS NOT NULL", db_record_type
        )
        completed_count = await self.conn.fetchval(
            """
            SELECT COUNT(*) FROM extracted_records 
            WHERE record_type = $1 AND source_url IS NOT NULL 
              AND (status = 'detailed' OR (status IS NULL AND (data->>'details_scraped_at') IS NOT NULL))
            """, db_record_type
        )

        cases = await db_storage.load_records_needing_detail(self.conn, db_record_type, sort_desc=reverse_direction)

        max_records = extraction_params.get("max_records")
        if max_records:
            try:
                limit_val = int(max_records)
                if len(cases) > limit_val:
                    cases = cases[:limit_val]
            except (ValueError, TypeError):
                pass

        logger.info(f"Loaded {len(cases)} pending items for detailing (Total metrics pending: {total_count}, completed: {completed_count})")
        if not cases:
            logger.info("✅ No structural rows require detailed asset parsing updates.")
            return

        # Close the initial indexing page to free resources
        if self.browser_manager.page:
            try:
                await self.browser_manager.page.close()
            except Exception:
                pass

        concurrency = int(extraction_params.get("concurrency", 8))
        logger.info(f"Starting concurrent detailing with {concurrency} workers...")

        queue = asyncio.Queue()
        for progress_idx, case_item in enumerate(cases):
            await queue.put((progress_idx, case_item))

        count = 0
        count_lock = asyncio.Lock()

        async def worker(worker_id: int):
            nonlocal count
            logger.info(f"Worker {worker_id} started.")
            
            # Create a dedicated context and page for this worker
            from utils.browser_helper import create_browser_context
            context, page = await create_browser_context(
                self.browser_manager.browser,
                ignore_https_errors=self.config.get("allow_insecure_https", False),
                storage_state=self.progress_state.get("storage_state"),
                proxy_url=self.proxy_url if self.use_proxy else None,
                viewport={"width": 1280, "height": 800},
            )
            
            try:
                consecutive_crashes = 0
                while not queue.empty():
                    try:
                        progress_idx, case_item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    record_id = case_item["id"]
                    url = case_item["source_url"]
                    data_payload = case_item["data"]

                    async with self.db_lock:
                        current_status = await self.conn.fetchval("SELECT status FROM extracted_records WHERE id = $1", record_id)
                    
                    if current_status == "detailed":
                        queue.task_done()
                        continue

                    logger.info(f"[Worker {worker_id}][{completed_count + progress_idx + 1}/{total_count}] Scraping details from: {url}")
                    
                    for attempt in range(1, 4):
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            await page.wait_for_load_state("networkidle")

                            if progress_idx % 50 == 0:
                                await dismiss_cookie_consent(page)

                            try:
                                await page.wait_for_selector('div.item-content-loaded', timeout=15000)
                            except Exception:
                                logger.warning(f"[Worker {worker_id}] Content div did not appear for {url} (attempt {attempt}/3).")
                                continue

                            detail_info = await page.evaluate(_EXTRACT_DETAIL_JS)

                            if detail_info.get("content_loaded") and not detail_info.get("auth_ok"):
                                logger.warning(f"[Worker {worker_id}] 🔒 Session signature invalidated! Refreshing security token states...")
                                await self.authenticate(headless=True)
                                
                                self.progress_state = await db_storage.load_pipeline_state(self.conn, index_pipeline_name)
                                self.browser_manager.storage_state = self.progress_state.get("storage_state")
                                
                                await page.close()
                                await context.close()
                                context, page = await create_browser_context(
                                    self.browser_manager.browser,
                                    ignore_https_errors=self.config.get("allow_insecure_https", False),
                                    storage_state=self.progress_state.get("storage_state"),
                                    proxy_url=self.proxy_url if self.use_proxy else None,
                                    viewport={"width": 1280, "height": 800},
                                )
                                
                                logger.info(f"[Worker {worker_id}] Retrying target detail payload location path -> {url}")
                                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                                await page.wait_for_load_state("networkidle")
                                
                                try:
                                    await page.wait_for_selector('div.item-content-loaded', timeout=15000)
                                except Exception:
                                    continue

                                detail_info = await page.evaluate(_EXTRACT_DETAIL_JS)
                                if detail_info.get("content_loaded") and not detail_info.get("auth_ok"):
                                    logger.error("🔒 Authentication token deployment failed completely. Exiting engine session execution loops.")
                                    sys.exit(1)

                            if not detail_info.get("content_loaded"):
                                continue

                            metadata = detail_info.get("metadata", {})
                            if len(metadata) < 9:
                                logger.warning(
                                    f"[Worker {worker_id}] Incomplete detail payload extracted for {url} ({len(metadata)} fields found). "
                                    f"Likely loaded in unauthenticated state. Skipping status update to keep it 'indexed'."
                                )
                                break

                            for key, value in metadata.items():
                                data_payload[key] = value
                            data_payload["details_scraped_at"] = datetime.now().isoformat()
                            data_payload["worker_id"] = worker_id

                            async with self.db_lock:
                                await db_storage.update_record_data(self.conn, record_id, data_payload, status="detailed")
                            
                            async with count_lock:
                                count += 1
                            consecutive_crashes = 0
                            logger.info(f"[Worker {worker_id}] [+] Enriched tracking map row with {len(metadata)} fields.")
                            break

                        except Exception as page_err:
                            err_str = str(page_err)
                            is_closed_err = "closed" in err_str.lower() or "connection" in err_str.lower()
                            is_crash = "crash" in err_str.lower() or is_closed_err

                            if is_crash:
                                consecutive_crashes += 1
                                logger.error(
                                    f"[Worker {worker_id}] 💥 Page crashed/closed on {url} (attempt {attempt}/3, "
                                    f"consecutive crashes: {consecutive_crashes}). Recreating..."
                                )
                                
                                if is_closed_err:
                                    logger.error(f"[Worker {worker_id}] Browser connection lost. Attempting self-healing recovery...")
                                    async with self.db_lock:
                                        if not self.browser_manager.browser or not self.browser_manager.browser.is_connected():
                                            logger.warning(f"[Worker {worker_id}] Browser process is disconnected. Restarting BrowserManager...")
                                            try:
                                                await self.browser_manager.recycle()
                                            except Exception as recycle_err:
                                                logger.error(f"[Worker {worker_id}] Failed to recycle browser manager: {recycle_err}")

                                try:
                                    try:
                                        await page.close()
                                    except Exception:
                                        pass
                                    try:
                                        await context.close()
                                    except Exception:
                                        pass
                                    
                                    context, page = await create_browser_context(
                                        self.browser_manager.browser,
                                        ignore_https_errors=self.config.get("allow_insecure_https", False),
                                        storage_state=self.progress_state.get("storage_state"),
                                        proxy_url=self.proxy_url if self.use_proxy else None,
                                        viewport={"width": 1280, "height": 800},
                                    )
                                except Exception as recreate_err:
                                    logger.error(f"[Worker {worker_id}] Failed to recreate page/context: {recreate_err}")
                                    await asyncio.sleep(5)
                                    continue

                                if consecutive_crashes >= 5:
                                    logger.error(f"[Worker {worker_id}] Too many consecutive crashes/disconnects ({consecutive_crashes}). Stopping worker.")
                                    return

                                await asyncio.sleep(3)
                            else:
                                logger.error(f"[Worker {worker_id}] Failed parsing detail target row at path {url} (attempt {attempt}/3): {page_err}")
                                await asyncio.sleep(2)

                    cooldown_seconds = float(extraction_params.get("cooldown_seconds", 2.0))
                    await asyncio.sleep(cooldown_seconds)
                    queue.task_done()
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                logger.info(f"Worker {worker_id} stopped.")

        workers = [asyncio.create_task(worker(i)) for i in range(1, concurrency + 1)]
        await queue.join()

        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(f"✅ Detailing layer completed execution workflows. Enriched {count} records directly.")

    async def extraction(self) -> None:
        """Stage 2: Validation metrics checks execution sequence."""
        extraction_params = self.config.get("extraction_params") or {}
        index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', self.pipeline_name)
        db_record_type = extraction_params.get("shared_record_type") or index_pipeline_name

        total = await self.conn.fetchval("SELECT COUNT(*) FROM extracted_records WHERE record_type = $1", db_record_type)
        detailed = await self.conn.fetchval("SELECT COUNT(*) FROM extracted_records WHERE record_type = $1 AND status = 'detailed'", db_record_type)
        logger.info(f"📊 Final Data Pipeline Audit Log -> Total Records: {total} | Fully Detailed/Enriched: {detailed}")


# -----------------------------------------------------------------------------
# CLI Entrypoint Execution
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Coeus Sabinet Scraper Refactored Model Engine")
    parser.add_argument(
        "--pipeline", "--pipeline_name",
        type=str, 
        dest="pipeline",
        default="sabinet_ccma", 
        help="Pipeline configurations parameters key mapping identifier"
    )
    parser.add_argument("--auth-only", action="store_true", help="Execute authentication workflows passes only")
    parser.add_argument("--headless", action="store_true", help="Orchestrate processes inside hidden graphical frame views")
    args = parser.parse_args()

    scraper = SabinetScraper(pipeline_name=args.pipeline)
    if args.auth_only:
        asyncio.run(scraper.initialize())
        asyncio.run(scraper.authenticate(headless=args.headless))
        asyncio.run(scraper.cleanup())
    else:
        asyncio.run(scraper.run())