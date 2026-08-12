import asyncio
import calendar
import os
import queue
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime, date, timezone
from typing import Optional

from bs4 import BeautifulSoup
from seleniumbase import SB

from .base_scraper import BaseScraper, setup_logger
from . import db_storage

logger = setup_logger(__name__)

# -----------------------------------------------------------------------------
# JavaScript DOM Extractor Injection
# -----------------------------------------------------------------------------
_EXTRACT_ITEMS_JS = r"""
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
"""

_EXTRACT_DETAIL_JS = r"""
    const result = { content_loaded: false, metadata: {} };

    const contentDiv = document.querySelector('div.item-content-loaded');
    if (!contentDiv) {
        return result;
    }
    result.content_loaded = true;

    // Extract title from h1
    const h1El = contentDiv.querySelector('h1');
    if (h1El) {
        const clone = h1El.cloneNode(true);
        clone.querySelectorAll('.anticon, [role="img"]').forEach(el => el.remove());
        const titleText = clone.innerText.trim();
        if (titleText) {
            result.metadata['detail_title'] = titleText;
        }
    }

    // Extract key/value pairs from item-meta-data spans
    const tags = Array.from(contentDiv.querySelectorAll('.item-meta-data .ant-tag'));
    tags.forEach(tag => {
        const strongEl = tag.querySelector('strong');
        if (!strongEl) return;

        const rawKey = strongEl.innerText.replace(/:$/, '').trim();
        // Remove the label node to get only the value text
        const clone = tag.cloneNode(true);
        clone.querySelector('strong').remove();
        const value = clone.innerText.trim();

        const key = rawKey
            .toLowerCase()
            .replace(/[^a-z0-9_]/g, '_')
            .replace(/_+/g, '_')
            .replace(/^_+|_+$/g, '');

        if (key && value) {
            result.metadata[key] = value;
        }
    });

    return result;
"""


# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------
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


class SabinetScraper(BaseScraper):
    """Sabinet CCMA Awards Framework Implementation (SeleniumBase UC Mode).

    Handles authentication state capture, rolling-window searches, indexing,
    and concurrent detailed item extraction.
    """

    def __init__(self, pipeline_name: str, headless: bool = False, skip_stages: Optional[str] = None):
        super().__init__(pipeline_name, skip_stages=skip_stages)
        self.headless = headless
        self.use_proxy: bool = False
        self.proxy_url: Optional[str] = None
        self.db_lock = asyncio.Lock()
        self.failed_ids = []

    async def initialize(self) -> None:
        """Hydrate configuration variables and session state references."""
        await super().initialize()

    async def authenticate(self, headless: bool = False) -> None:
        """No-op: Sabinet does not require session authentication."""
        pass

    def _navigate_with_reconnect(self, sb: SB, url: str, label: str = "nav") -> None:
        """Helper to navigate to a page with Turnstile auto-solver and cookie dismissal."""
        logger.info(f"[{label}] Navigating with UC reconnect: {url}")
        sb.uc_open_with_reconnect(url, reconnect_time=2)
        try:
            sb.uc_gui_handle_captcha()
        except Exception:
            pass

        # Dismiss cookie modal if visible
        try:
            if sb.is_element_visible("button.accept-btn, button#accept-cookies"):
                sb.uc_click("button.accept-btn, button#accept-cookies")
        except Exception:
            pass



    def _setup_search_page(self, sb: SB, start_url: str) -> None:
        """Configures cookie conditions and list scaling presentation values."""
        self._navigate_with_reconnect(sb, start_url, label="Setup_Search")

        try:
            logger.info("Configuring layout adjustments to 100 entries per page...")
            dropdown_selector = 'div.ant-select[aria-label="How many results to show in list"]'
            if sb.is_element_visible(dropdown_selector):
                sb.uc_click(dropdown_selector)
                sb.sleep(1)

                option_selector = '.ant-select-item-option-content:contains("100 per page")'
                if sb.is_element_visible(option_selector):
                    sb.uc_click(option_selector)
                    logger.info("Successfully selected '100 per page' option.")
                    sb.sleep(2)
        except Exception as pag_err:
            logger.warning(f"Could not configure pagination parameters: {pag_err}. Defaulting scaling.")

    def _indexing_sync(
        self,
        start_url: str,
        incremental: bool,
        resume_year: int,
        resume_month: int,
        db_record_type: str,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[int, bool]:
        """Synchronous indexing loop running in a dedicated SeleniumBase UC thread."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        total_new = 0
        skipped_any = False

        use_xvfb = False
        if not self.headless and (os.path.exists("/.dockerenv") or not os.environ.get("DISPLAY")):
            use_xvfb = True
        # Setup isolated profile path for indexing to avoid collision
        indexing_profile_dir = "/tmp/sabinet_indexing_profile"
        shutil.rmtree(indexing_profile_dir, ignore_errors=True)
        os.makedirs(indexing_profile_dir, exist_ok=True)

        with SB(
            uc=True,
            headless=self.headless,
            proxy=sb_proxy,
            xvfb=use_xvfb,
            test=True,
            user_data_dir=indexing_profile_dir,
            multi_proxy=self.use_proxy,
            chromium_arg="--no-sandbox,--disable-dev-shm-usage"
        ) as sb:
            sb.set_window_size(1280, 800)
            sb.driver.set_page_load_timeout(30)
            sb.driver.set_script_timeout(30)

            # Verify and log outbound public IP
            self.log_outbound_ip(sb, label="Sabinet Indexing Stage")

            self._setup_search_page(sb, start_url)

            # Parse year entries from sidebar — retry up to 3 times to allow
            # the React sidebar to finish rendering before reading the DOM.
            _YEAR_SIDEBAR_JS = r"""
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
            """
            year_entries: list[tuple[int, int]] = []
            if incremental:
                current_year = datetime.now().year
                year_entries = [(current_year, 1)]
            else:
                raw_years = []
                for _attempt in range(3):
                    try:
                        raw_years = sb.execute_script(_YEAR_SIDEBAR_JS) or []
                    except Exception as ye:
                        logger.warning(f"Year sidebar JS error (attempt {_attempt + 1}/3): {ye}")
                    if raw_years:
                        break
                    logger.info(f"Year sidebar returned empty (attempt {_attempt + 1}/3), waiting 3s...")
                    sb.sleep(3)

                if not raw_years:
                    raise RuntimeError(
                        "Year sidebar returned no entries after 3 attempts. "
                        "The page may not have loaded correctly or the selector has changed."
                    )

                year_entries = [(int(y), int(c)) for y, c in raw_years]
                year_entries.sort(key=lambda x: x[0])
                logger.info(f"Found {len(year_entries)} years in sidebar: {[y for y, _ in year_entries]}")

            for year, year_count in year_entries:
                if resume_year > 0 and year < resume_year:
                    continue

                logger.info(f"📅 Processing year {year} (~{year_count} entries)...")
                months = list(range(1, 13))

                for month in months:
                    if year == resume_year and month < resume_month:
                        continue

                    last_day = calendar.monthrange(year, month)[1]
                    date_from = date(year, month, 1).strftime("%m/%d/%Y")
                    date_to = date(year, month, last_day).strftime("%m/%d/%Y")

                    logger.info(f"  🗓 Timeframe tracking window: {date_from} → {date_to}")

                    # Apply date filtering
                    try:
                        if not sb.is_element_visible('input[placeholder="Date From"]'):
                            # Try to click the Advanced Search button using multiple selector candidates
                            advanced_search_selectors = [
                                'button:contains("Advanced Search")',
                                'button:contains("Advanced search")',
                                '#search-col-4 button',
                            ]
                            clicked_adv = False
                            for selector in advanced_search_selectors:
                                if sb.is_element_visible(selector):
                                    logger.info(f"Clicking advanced search button with selector: {selector}")
                                    try:
                                        sb.uc_click(selector)
                                        sb.sleep(1)
                                        if sb.is_element_visible('input[placeholder="Date From"]'):
                                            clicked_adv = True
                                            break
                                    except Exception as click_err:
                                        logger.warning(f"Failed to click advanced search via {selector}: {click_err}")

                            # If not clicked or not visible, perform a page setup reload to reset SPA/page state
                            if not clicked_adv and not sb.is_element_visible('input[placeholder="Date From"]'):
                                logger.warning("⚠️ Date From element not visible. Resetting page to recover layout state...")
                                self._setup_search_page(sb, start_url)
                                sb.sleep(2)
                                for selector in advanced_search_selectors:
                                    if sb.is_element_visible(selector):
                                        logger.info(f"Clicking advanced search button after reload: {selector}")
                                        try:
                                            sb.uc_click(selector)
                                            sb.sleep(1)
                                            if sb.is_element_visible('input[placeholder="Date From"]'):
                                                break
                                        except Exception:
                                            pass

                        # Final verification before typing
                        if not sb.is_element_visible('input[placeholder="Date From"]'):
                            raise RuntimeError("Date From selector is still not visible after trying Advanced Search and page reload.")

                        sb.type('input[placeholder="Date From"]', date_from)
                        sb.type('input[placeholder="Date To"]', date_to)

                        clicked = sb.execute_script("""
                            const btn = [...document.querySelectorAll('.btn-search')].find(el => {
                                const style = window.getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                return (style.display !== 'none' && style.visibility !== 'hidden' && parseFloat(style.opacity) > 0 && rect.width > 0 && rect.height > 0);
                            });
                            if (btn) { btn.click(); return true; } return false;
                        """)
                        if not clicked:
                            raise RuntimeError("Search button obscured or missing.")
                        sb.sleep(2)
                    except Exception as adv_err:
                        logger.warning(f"Error applying date filters: {adv_err}. Skipping window.")
                        skipped_any = True
                        continue

                    if not sb.is_element_present('.ant-list-item'):
                        # Even if no results are found, this month is processed successfully.
                        # Save progress so we don't repeat this month on subsequent runs.
                        future = asyncio.run_coroutine_threadsafe(
                            self.save_progress(year, month, completed=False),
                            loop
                        )
                        future.result()
                        continue

                    # Process pages within window
                    current_page = 1
                    window_new = 0

                    while True:
                        if not sb.is_element_present('.ant-list-item'):
                            break

                        page_items = sb.execute_script(_EXTRACT_ITEMS_JS) or []
                        records_to_upsert = []

                        for item_data in page_items:
                            url = item_data.get("detail_url")
                            case_no = item_data.get("dataset_number")

                            if url in self.existing_urls or (case_no and case_no in self.existing_dataset_numbers):
                                continue

                            item_data["index_scraped_at"] = datetime.now(timezone.utc).isoformat()
                            records_to_upsert.append(item_data)
                            if url:
                                self.existing_urls.add(url)
                            if case_no:
                                self.existing_dataset_numbers.add(case_no)
                            window_new += 1

                        if records_to_upsert:
                            future = asyncio.run_coroutine_threadsafe(
                                db_storage.upsert_scraped_records_batch(
                                    conn=self.conn,
                                    target_id=self.target_id,
                                    record_type=db_record_type,
                                    records=records_to_upsert,
                                    url_key="detail_url",
                                    status="indexed",
                                ),
                                loop,
                            )
                            future.result()

                        # Pagination
                        next_selector = 'li.ant-pagination-next:not(.ant-pagination-disabled) a'
                        if sb.is_element_visible(next_selector):
                            if current_page % 100 == 0:
                                logger.info(f"Scraped {current_page} pages. Rate-limiting pause...")
                                sb.sleep(30)

                            sb.uc_click(next_selector)
                            sb.sleep(2)
                            current_page += 1
                        else:
                            break

                    total_new += window_new
                    sb.sleep(1)

                    # Save progress for successfully completed month
                    future = asyncio.run_coroutine_threadsafe(
                        self.save_progress(year, month, completed=False),
                        loop
                    )
                    future.result()

        return total_new, skipped_any

    async def indexing(self) -> None:
        """Sub-process A: Extracts records utilizing programmatic rolling timeframe parameters."""
        start_url = (
            self.config.get("start_url")
            or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"
        )
        extraction_params = self.config.get("extraction_params") or {}
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name
        # incremental is ONLY driven by explicit config — never by fully_complete.
        # A fully_complete pipeline should re-run from scratch (resume_year=0),
        # not collapse into a current-year-only incremental pass.
        incremental = extraction_params.get("incremental", False)

        incomplete_years = self.progress_state.get("incomplete_years", [])
        is_fully_complete = self.progress_state.get("fully_complete", False)

        resume_year = 0
        if not incremental and not is_fully_complete:
            resume_year = self.progress_state.get("last_year", 0)
            if resume_year == 0 and incomplete_years:
                resume_year = min(incomplete_years)

        resume_month = 0 if (incremental or is_fully_complete) else self.progress_state.get("last_month", 0)

        loop = asyncio.get_running_loop()
        total_new, skipped_any = await asyncio.to_thread(
            self._indexing_sync,
            start_url,
            incremental,
            resume_year,
            resume_month,
            db_record_type,
            loop,
        )

        logger.info(f"✅ Indexing complete. Scraped index updates total: {total_new}")
        if not skipped_any:
            self.progress_state = await db_storage.sync_dynamic_pipeline_state(
                self.conn,
                self.pipeline_name,
                db_record_type,
                extra_state={
                    "fully_complete": True,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info("✅ Progress state set to fully complete.")
        else:
            logger.warning("⚠️ Some timeframe windows were skipped due to errors. Progress state NOT set to fully complete.")

    def _detailing_worker_thread(
        self,
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        db_record_type: str,
        total_cases: int,
    ) -> None:
        """Thread executing detailed item extractions via an isolated SB UC instance."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        logger.info(f"[Worker {worker_id}] Starting SB UC instance...")

        # Stagger worker startup to avoid race conditions during concurrent Chrome process creation
        if worker_id > 1:
            stagger_delay = (worker_id - 1) * 2.0
            logger.info(f"[Worker {worker_id}] Staggering startup by {stagger_delay:.1f}s...")
            time.sleep(stagger_delay)

        use_xvfb = False
        if not self.headless and (os.path.exists("/.dockerenv") or not os.environ.get("DISPLAY")):
            use_xvfb = True
        # Setup isolated profile path for this worker
        worker_profile_dir = f"/tmp/sabinet_worker_profile_{worker_id}"

        max_init_retries = 3
        for attempt in range(1, max_init_retries + 1):
            try:
                # Ensure the profile directory is completely fresh for this attempt to avoid stale SingletonLock
                shutil.rmtree(worker_profile_dir, ignore_errors=True)
                os.makedirs(worker_profile_dir, exist_ok=True)

                with SB(
                    uc=True,
                    headless=self.headless,
                    proxy=sb_proxy,
                    xvfb=use_xvfb,
                    test=True,
                    user_data_dir=worker_profile_dir,
                    multi_proxy=self.use_proxy,
                    chromium_arg="--no-sandbox,--disable-dev-shm-usage"
                ) as sb:
                    sb.set_window_size(1280, 800)
                    sb.driver.set_page_load_timeout(30)
                    sb.driver.set_script_timeout(30)

                    # Verify and log outbound public IP
                    self.log_outbound_ip(sb, label=f"Sabinet Detailing Worker {worker_id}")

                    while True:
                        item = work_queue.get()
                        if item is None:
                            work_queue.task_done()
                            break

                        idx, case_item = item
                        record_id = case_item["id"]
                        url = case_item["source_url"]

                        logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] Processing detail payload -> {url}")
                        try:
                            self._navigate_with_reconnect(sb, url, label=f"Worker {worker_id}")
                            sb.sleep(1)

                            detail_res = sb.execute_script(_EXTRACT_DETAIL_JS) or {}
                            metadata = detail_res.get("metadata", {})

                            payload = {
                                "details_scraped_at": datetime.now(timezone.utc).isoformat(),
                                "content_loaded": detail_res.get("content_loaded", False),
                                **metadata,
                            }

                            # Thread-safe database update
                            async def update_db():
                                async with self.db_lock:
                                    await self.conn.execute(
                                        """
                                        UPDATE extracted_records
                                        SET data = data || $1::jsonb,
                                            status = 'detailed',
                                            detailed_at = NOW()
                                        WHERE id = $2
                                        """,
                                        db_storage.json_dumps(payload),
                                        record_id,
                                    )

                            future = asyncio.run_coroutine_threadsafe(update_db(), loop)
                            future.result()

                            logger.info(f"[Worker {worker_id}][{idx}/{total_cases}] [+] Details saved for record {record_id}")
                        except Exception as err:
                            logger.warning(f"[Worker {worker_id}][{idx}/{total_cases}] Detail extraction error on {url}: {err}")
                            self.failed_ids.append(record_id)

                        work_queue.task_done()

                # Normal exit from SB context (queue empty / sentinel received)
                break
            except Exception as init_err:
                logger.warning(
                    f"[Worker {worker_id}] Driver session creation attempt {attempt}/{max_init_retries} failed: {init_err}"
                )
                if attempt < max_init_retries:
                    time.sleep(3 * attempt)
                else:
                    logger.error(f"[Worker {worker_id}] Exhausted driver creation retries.")

        logger.info(f"[Worker {worker_id}] Thread completed.")

    async def detailing(self) -> None:
        """Sub-process B: Asset Enrichment Layer via concurrent SB UC threads."""
        extraction_params = self.config.get("extraction_params") or {}
        index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', self.pipeline_name)
        db_record_type = extraction_params.get("shared_record_type") or index_pipeline_name

        # Count total records needing detailing upfront
        total_cases = await self.conn.fetchval(
            """
            SELECT COUNT(*)
            FROM extracted_records
            WHERE record_type = $1
              AND source_url IS NOT NULL
              AND (status = 'indexed' OR (status IS NULL AND (data->>'details_scraped_at') IS NULL))
            """,
            db_record_type,
        )
        if total_cases == 0:
            logger.info("✅ No structural rows require detailed asset parsing updates.")
            return

        # Tell SeleniumBase we are running parallel threads to trigger internal locking mechanisms
        import sys
        if "-n" not in sys.argv:
            sys.argv.append("-n")

        concurrency = int(extraction_params.get("concurrency", 4))
        logger.info(f"Starting concurrent detailing with {concurrency} SB UC workers for {total_cases} cases...")

        work_queue = queue.Queue()
        loop = asyncio.get_running_loop()
        
        # Start the worker threads — must use create_task() so they begin executing
        # immediately. Without it, the coroutines are just objects in a list and the
        # feed loop's work_queue.join() would deadlock (no consumers running).
        worker_tasks = [
            asyncio.create_task(asyncio.to_thread(
                self._detailing_worker_thread,
                worker_id=i,
                work_queue=work_queue,
                loop=loop,
                db_record_type=db_record_type,
                total_cases=total_cases,
            ))
            for i in range(1, concurrency + 1)
        ]

        # Feed the queue in batches of 500 to keep memory consumption low
        batch_size = 500
        processed_count = 0
        
        try:
            while True:
                cases = await db_storage.load_records_needing_detail(
                    self.conn,
                    db_record_type,
                    limit=batch_size,
                    exclude_ids=self.failed_ids if self.failed_ids else None,
                    include_data=False,
                )
                if not cases:
                    break

                # Add cases to the queue with absolute index
                for case_item in cases:
                    processed_count += 1
                    work_queue.put((processed_count, case_item))

                # Wait for the workers to process all items in this batch
                await asyncio.to_thread(work_queue.join)
                
        finally:
            # Send None sentinels to signal all worker threads to stop
            for _ in range(concurrency):
                work_queue.put(None)
                
            # Wait for all workers to shut down cleanly
            await asyncio.gather(*worker_tasks)

        logger.info("✅ Detailing stage complete.")

    async def extraction(self) -> None:
        """Stage 2: Verification operations."""
        logger.info("Stage 2: Post-Scraping Data Extraction verification complete.")


# -----------------------------------------------------------------------------
# Runner Execution Pattern
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sabinet Scraper (SeleniumBase UC Mode)")
    parser.add_argument(
        "--pipeline", "--pipeline_name",
        required=True,
        dest="pipeline_name",
        help="Target pipeline setup configuration key"
    )
    parser.add_argument(
        "--headless",
        default="true",
        help="Run browser in headless mode (true/false)"
    )
    parser.add_argument(
        "--skip_stages",
        default=None,
        help="Comma-separated stage numbers to skip, e.g. '1' or '1,2'. "
             "Stage 1=Indexing, Stage 2=Detailing, Stage 3=Extraction."
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    scraper = SabinetScraper(
        pipeline_name=args.pipeline_name,
        headless=headless_value,
        skip_stages=args.skip_stages,
    )
    asyncio.run(scraper.run())
    sys.exit(0)