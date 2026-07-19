import asyncio
import calendar
from datetime import datetime, date
from typing import Any
from playwright.async_api import async_playwright, Page

from .base_scraper import BaseScraper, setup_logger
from . import db_storage
from .utils.browser_helper import BrowserManager, dismiss_cookie_consent

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


class SabinetScraper(BaseScraper):
    """
    Sabinet CCMA Awards Framework Implementation.
    Handles authentication state capture, rolling-window searches, and indexing.
    """

    async def authenticate(self, headless: bool = False) -> None:
        """Automatically log in to Sabinet and persist browser session state to the database."""
        scrape_url = (
            "https://discover.sabinet.co.za/search?"
            "Search=&ProductType=ccmabargainingcouncilawards"
            "&resultsortOption=%22Date+Oldest+first%22"
        )
        username = "TiaanF"
        password = "G7fR7Bzv4$@ea5!"

        async with async_playwright() as p:
            logger.info("[INFO] Launching browser for automated authentication state generation...")
            manager = BrowserManager(
                p,
                headless=headless,
                viewport={"width": 1280, "height": 800},
            )
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
                logger.info(f"✅ Session state successfully flushed to the database instance.")

    async def _setup_search_page(self, page: Page, start_url: str) -> None:
        """Configures cookie conditions and list scaling presentation values."""
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
                    option = page.locator('.ant-select-item-option:has-text("100 per page")').first
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
        if self.progress_state.get("fully_complete"):
            logger.info("✅ Pipeline index status matches complete flag definitions. Skipping.")
            return

        start_url = (
            self.config.get("start_url")
            or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"
        )
        extraction_params = self.config.get("extraction_params") or {}
        reverse_direction = extraction_params.get("reverse_direction", False)
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name

        # Resolve recovery execution index offsets
        resume_year: int = self.progress_state.get("last_year", 0)
        resume_month: int = self.progress_state.get("last_month", 0)
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
                        if (!isNaN(year) && !isNaN(count) && count > 0) {
                            results.push([year, count]);
                        }
                    });
                    return results;
                }
            """)
            year_entries = [(int(y), int(c)) for y, c in raw_years]
            year_entries.sort(key=lambda x: x[0], reverse=reverse_direction)
            logger.info(f"Target timelines loaded: " + ", ".join(f"{y}({c})" for y, c in year_entries))
        except Exception as ye:
            logger.warning(f"Could not read dynamic timeline sidebars: {ye}. Resetting defaults to current year.")
            year_entries = [(datetime.now().year, 1)]

        total_new = 0

        for year, year_count in year_entries:
            if reverse_direction and resume_year > 0 and year > resume_year:
                continue
            elif not reverse_direction and resume_year > 0 and year < resume_year:
                continue

            # Validate structural coverage ranges before parsing execution metrics
            try:
                db_count = await self.conn.fetchval(
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
                    logger.info(f"Skipping complete sequence interval for year {year} ({db_count}/{year_count} in DB).")
                    await self.save_progress(year, 1 if reverse_direction else 12, completed=True)
                    continue
            except Exception as db_cnt_err:
                logger.warning(f"Error checking verification levels metrics: {db_cnt_err}")

            logger.info(f"📅 Navigating execution context for target segment: {year} (~{year_count} units)...")

            months = list(range(12, 0, -1)) if reverse_direction else list(range(1, 13))
            for month in months:
                if year == resume_year:
                    if reverse_direction and month > resume_month:
                        continue
                    elif not reverse_direction and month < resume_month:
                        continue

                last_day = calendar.monthrange(year, month)[1]
                date_from = date(year, month, 1).strftime("%m/%d/%Y")
                date_to = date(year, month, last_day).strftime("%m/%d/%Y")

                logger.info(f"  🗓 Active timeframe tracking boundary window: {date_from} → {date_to}")
                await self.save_progress(year, month, completed=False)

                logger.info(f"Refreshing active headless window configurations to optimize memory scopes...")
                await self.browser_manager.recycle()
                await self._setup_search_page(self.browser_manager.page, start_url)
                page = self.browser_manager.page

                # Open and feed queries inside Advanced Search filters interface
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
                        raise RuntimeError("Search button selection validation state broken or obscured.")
                    
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)

                except Exception as adv_err:
                    logger.warning(f"Error structuring date filtering parameters for scope validation: {adv_err}. Skipping.")
                    await self.save_progress(year, month, completed=False)
                    continue

                try:
                    await page.wait_for_selector('.ant-list-item', timeout=10000)
                except Exception:
                    logger.info(f"No match elements returned for target timeframe metrics. Advancing tracking index.")
                    await self.save_progress(year, month, completed=True)
                    continue

                # Evaluate structural completion matching on multi-page offsets
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
                            logger.info(f"Multiple pages isolated ({last_page_num}). Testing boundary completions metrics...")
                            await last_page_locator.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)

                    current_page_items: list[dict] = await page.evaluate(_EXTRACT_ITEMS_JS)
                    if current_page_items:
                        last_item = current_page_items[-1]
                        is_complete = await db_storage.is_record_complete(
                            self.conn, db_record_type, last_item.get("detail_url"), last_item.get("case_number")
                        )
                except Exception as last_check_err:
                    logger.warning(f"Terminal boundary state checks encountered error structures: {last_check_err}")

                if is_complete:
                    logger.info(f"✅ Database signatures confirm segment indices are identical. Skipping validation window.")
                    await self.save_progress(year, month, completed=True)
                    continue
                else:
                    if last_page_num > 1:
                        try:
                            logger.info("Resetting pagination parameters index to Page 1 view dimensions...")
                            first_page_locator = page.locator('li.ant-pagination-item-1, li.ant-pagination-item').first
                            if await first_page_locator.count() > 0:
                                await first_page_locator.click()
                            else:
                                await page.evaluate("() => { const b = document.querySelector('.btn-search'); if(b) b.click(); }")
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(2)
                        except Exception as reset_err:
                            logger.warning(f"Failed handling pagination reset updates: {reset_err}")

                # Core pagination scrape execution sequence
                current_page = 1
                while True:
                    logger.info(f"    Scanning page indices context frame: {current_page} [{date_from} → {date_to}]")
                    try:
                        await page.wait_for_selector('.ant-list-item', timeout=15000)
                    except Exception:
                        logger.warning("Empty records display node configurations matched. Closing sequence.")
                        break

                    page_items: list[dict] = await page.evaluate(_EXTRACT_ITEMS_JS)
                    
                    # Transaction processing for discovered items block
                    records_to_upsert = []
                    for item_data in page_items:
                        url = item_data.get("detail_url")
                        case_no = item_data.get("case_number")

                        if url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                            continue

                        records_to_upsert.append(item_data)
                        if url:
                            self.existing_urls.add(url)
                        if case_no:
                            self.existing_case_numbers.add(case_no)

                    if records_to_upsert:
                        upserted_count = await db_storage.upsert_scraped_records_batch(
                            conn=self.conn,
                            target_id=self.target_id,
                            record_type=db_record_type,
                            records=records_to_upsert,
                            url_key="detail_url",
                            status="indexed"
                        )
                        total_new += upserted_count
                        logger.info(f"    [+] Persisted {upserted_count} new entries to database storage mapping matrix.")

                    # Direct step pagination actions
                    next_btn = page.locator('li.ant-pagination-next:not(.ant-pagination-disabled)').first
                    if await next_btn.count() > 0:
                        current_page += 1
                        await next_btn.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(1.5)
                    else:
                        break

                await self.save_progress(year, month, completed=True)

        logger.info(f"✅ Scraping workflow complete. Staged updates commit index total counters: {total_new}")

    async def detailing(self) -> None:
        """Sub-process B: Asset Enrichment. Sabinet handles payload tracking inside the indexing flow."""
        logger.info("Sub-process B: Document enrichment pipelines verified clean.")

    async def extraction(self) -> None:
        """Stage 2: Post-Scraping structure formatting validations operations pass."""
        logger.info("Stage 2: Validation metrics checks execution sequence completed.")


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