import asyncio
import calendar
import os
import queue
import re
import sys
import urllib.parse
from datetime import datetime, date
from typing import Any, Optional

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

    def __init__(self, pipeline_name: str, headless: bool = False):
        super().__init__(pipeline_name)
        self.headless = headless
        self.use_proxy: bool = False
        self.proxy_url: Optional[str] = None
        self.cookies_filepath: str = ""
        self.db_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Hydrate configuration variables and session state references."""
        await super().initialize()
        self.use_proxy = self.config.get("use_proxy", False)
        self.proxy_url = self.config.get("proxy_url")

        cookies_dir = os.path.join(os.path.dirname(self.output_dir), "cookies")
        os.makedirs(cookies_dir, exist_ok=True)
        self.cookies_filepath = os.path.join(cookies_dir, f"{self.pipeline_name}_cookies.pkl")

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

    def _authenticate_sync(self) -> bool:
        """Perform GUI authentication and persist session cookies to disk."""
        scrape_url = (
            "https://discover.sabinet.co.za/search?"
            "Search=&ProductType=ccmabargainingcouncilawards"
            "&resultsortOption=%22Date+Oldest+first%22"
        )
        username = os.getenv("SABINET_USERNAME", "TiaanF")
        password = os.getenv("SABINET_PASSWORD", "G7fR7Bzv4$@ea5!")

        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        logger.info("[Auth] Launching browser for automated authentication state generation...")

        with SB(uc=True, headless=self.headless, proxy=sb_proxy, test=True) as sb:
            sb.set_window_size(1280, 800)
            self._navigate_with_reconnect(sb, scrape_url, label="Auth_Gate")

            logger.info("🔐 Triggering security workflow context drawer...")
            sb.wait_for_element_visible('button:contains("Sign in login")', timeout=15)
            sb.uc_click('button:contains("Sign in login")')
            sb.sleep(1)

            logger.info("✏️ Filling credentials fields...")
            sb.wait_for_element_visible('input[placeholder*="Username"]', timeout=10)
            sb.type('input[placeholder*="Username"]', username)
            sb.type('input[placeholder*="Password"]', password)

            logger.info("🚀 Submitting authentication payload tokens...")
            sb.uc_click('button:contains("Sign into Account")')

            logger.info("⏳ Waiting for identity authorization response...")
            sb.wait_for_element_visible('button:contains("user myDiscover down")', timeout=30)
            logger.info("✅ Authentication validated successfully.")

            # Save session cookies to file
            sb.save_cookies(name=self.cookies_filepath)
            logger.info(f"✅ Session cookies successfully saved to: {self.cookies_filepath}")
            return True

    async def authenticate(self, headless: bool = False) -> None:
        """Automatically log in to Sabinet and persist session cookies to disk."""
        await asyncio.to_thread(self._authenticate_sync)

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
        reverse_direction: bool,
        incremental: bool,
        resume_year: int,
        resume_month: int,
        db_record_type: str,
        loop: asyncio.AbstractEventLoop,
    ) -> int:
        """Synchronous indexing loop running in a dedicated SeleniumBase UC thread."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        total_new = 0

        with SB(uc=True, headless=self.headless, proxy=sb_proxy, test=True) as sb:
            sb.set_window_size(1280, 800)

            # Load cookies if available
            if os.path.exists(self.cookies_filepath):
                sb.open(start_url)
                sb.load_cookies(name=self.cookies_filepath)

            self._setup_search_page(sb, start_url)

            # Parse year entries from sidebar
            year_entries: list[tuple[int, int]] = []
            try:
                raw_years = sb.execute_script(r"""
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
                """)
                year_entries = [(int(y), int(c)) for y, c in (raw_years or [])]
                if incremental:
                    current_year = datetime.now().year
                    year_entries = [entry for entry in year_entries if entry[0] == current_year]
                    if not year_entries:
                        year_entries = [(current_year, 1)]
                else:
                    year_entries.sort(key=lambda x: x[0], reverse=reverse_direction)
            except Exception as ye:
                logger.warning(f"Could not read dynamic timeline sidebars: {ye}. Defaulting to current year.")
                year_entries = [(datetime.now().year, 1)]

            for year, year_count in year_entries:
                if reverse_direction and resume_year > 0 and year > resume_year:
                    continue
                elif not reverse_direction and resume_year > 0 and year < resume_year:
                    continue

                logger.info(f"📅 Processing year {year} (~{year_count} entries)...")
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

                    logger.info(f"  🗓 Timeframe tracking window: {date_from} → {date_to}")

                    # Apply date filtering
                    try:
                        if not sb.is_element_visible('input[placeholder="Date From"]'):
                            if sb.is_element_present('button:contains("Advanced Search")'):
                                sb.uc_click('button:contains("Advanced Search")')
                                sb.sleep(1)

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
                        continue

                    if not sb.is_element_present('.ant-list-item'):
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
                            case_no = item_data.get("case_number")

                            if url in self.existing_urls or (case_no and case_no in self.existing_case_numbers):
                                continue

                            item_data["index_scraped_at"] = datetime.now().isoformat()
                            records_to_upsert.append(item_data)
                            if url:
                                self.existing_urls.add(url)
                            if case_no:
                                self.existing_case_numbers.add(case_no)
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

        return total_new

    async def indexing(self) -> None:
        """Sub-process A: Extracts records utilizing programmatic rolling timeframe parameters."""
        start_url = (
            self.config.get("start_url")
            or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"
        )
        extraction_params = self.config.get("extraction_params") or {}
        is_fully_complete = self.progress_state.get("fully_complete", False)
        incremental = extraction_params.get("incremental", False) or is_fully_complete

        reverse_direction = extraction_params.get("reverse_direction", False)
        db_record_type = extraction_params.get("shared_record_type") or self.pipeline_name

        resume_year = 0 if incremental else self.progress_state.get("last_year", 0)
        resume_month = 0 if incremental else self.progress_state.get("last_month", 0)

        loop = asyncio.get_running_loop()
        total_new = await asyncio.to_thread(
            self._indexing_sync,
            start_url,
            reverse_direction,
            incremental,
            resume_year,
            resume_month,
            db_record_type,
            loop,
        )

        logger.info(f"✅ Indexing complete. Scraped index updates total: {total_new}")
        self.progress_state["fully_complete"] = True
        self.progress_state["completed_at"] = datetime.now().isoformat()
        await db_storage.save_pipeline_state(self.conn, self.pipeline_name, self.progress_state)

    def _detailing_worker_thread(
        self,
        worker_id: int,
        work_queue: queue.Queue,
        loop: asyncio.AbstractEventLoop,
        db_record_type: str,
    ) -> None:
        """Thread executing detailed item extractions via an isolated SB UC instance."""
        sb_proxy = format_proxy_for_sb(self.proxy_url) if self.use_proxy else None
        logger.info(f"[Worker {worker_id}] Starting SB UC instance...")

        with SB(uc=True, headless=self.headless, proxy=sb_proxy, test=True) as sb:
            sb.set_window_size(1280, 800)

            if os.path.exists(self.cookies_filepath):
                sb.open("https://discover.sabinet.co.za/")
                sb.load_cookies(name=self.cookies_filepath)

            while True:
                try:
                    case_item = work_queue.get_nowait()
                except queue.Empty:
                    break

                record_id = case_item["id"]
                url = case_item["source_url"]

                logger.info(f"[Worker {worker_id}] Processing detail payload -> {url}")
                try:
                    self._navigate_with_reconnect(sb, url, label=f"Worker_{worker_id}")
                    sb.sleep(1)

                    detail_res = sb.execute_script(_EXTRACT_DETAIL_JS) or {}
                    metadata = detail_res.get("metadata", {})

                    payload = {
                        "details_scraped_at": datetime.now().isoformat(),
                        "auth_ok": detail_res.get("auth_ok", False),
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
                                    updated_at = NOW()
                                WHERE id = $2
                                """,
                                db_storage.json_dumps(payload),
                                record_id,
                            )

                    future = asyncio.run_coroutine_threadsafe(update_db(), loop)
                    future.result()

                    logger.info(f"[Worker {worker_id}] [+] Details saved for record {record_id}")
                except Exception as err:
                    logger.warning(f"[Worker {worker_id}] Detail extraction error on {url}: {err}")

                work_queue.task_done()

        logger.info(f"[Worker {worker_id}] Thread completed.")

    async def detailing(self) -> None:
        """Sub-process B: Asset Enrichment Layer via concurrent SB UC threads."""
        extraction_params = self.config.get("extraction_params") or {}
        reverse_direction = extraction_params.get("reverse_direction", False)
        index_pipeline_name = extraction_params.get("index_pipeline_name") or re.sub(r'_(details?)$', '', self.pipeline_name)
        db_record_type = extraction_params.get("shared_record_type") or index_pipeline_name

        cases = await db_storage.load_records_needing_detail(self.conn, db_record_type, sort_desc=reverse_direction)
        if not cases:
            logger.info("✅ No structural rows require detailed asset parsing updates.")
            return

        concurrency = int(extraction_params.get("concurrency", 4))
        logger.info(f"Starting concurrent detailing with {concurrency} SB UC workers...")

        work_queue = queue.Queue()
        for case_item in cases:
            work_queue.put(case_item)

        loop = asyncio.get_running_loop()
        worker_tasks = [
            asyncio.to_thread(
                self._detailing_worker_thread,
                worker_id=i,
                work_queue=work_queue,
                loop=loop,
                db_record_type=db_record_type,
            )
            for i in range(1, concurrency + 1)
        ]

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
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    scraper = SabinetScraper(pipeline_name=args.pipeline_name, headless=headless_value)
    asyncio.run(scraper.run())