import argparse
import asyncio
import json
import logging
import os
import sys

from playwright.async_api import async_playwright
from utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    start_url = config.get("start_url") or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"

    output_dir = os.path.join(
        "/app/data", pipeline_name, config.get("document_type", "awards").lower()
    )
    if not os.path.exists("/app/data") and not os.path.exists("/app"):
        output_dir = os.path.join(
            "data", pipeline_name, config.get("document_type", "awards").lower()
        )
    os.makedirs(output_dir, exist_ok=True)

    logger.info("==================================================")
    logger.info(f"🚀 COEUS SABINET WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {start_url}")
    logger.info("==================================================")

    output_file = os.path.join(output_dir, f"{pipeline_name}.json")
    existing_data = []
    existing_keys = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            for item in existing_data:
                key = (item.get("award_number"), item.get("title"))
                existing_keys.add(key)
            logger.info(f"Loaded {len(existing_data)} existing records from {output_file} for incremental checking.")
        except Exception as read_err:
            logger.warning(f"Could not load existing data from {output_file}: {read_err}. Performing full scrape.")

    extracted_data = []

    async with async_playwright() as p:
        # Check for storage state (cookies/session) to bypass login/SSO
        storage_state_path = os.path.join(output_dir, "state.json")
        if not os.path.exists(storage_state_path):
            # Fall back to root data state.json
            fallback_path = "/app/data/state.json" if os.path.exists("/app/data") else "data/state.json"
            if os.path.exists(fallback_path):
                storage_state_path = fallback_path
            else:
                storage_state_path = None

        if storage_state_path:
            logger.info(f"🔑 Loading active browser session state from: {storage_state_path}")

        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False),
            storage_state=storage_state_path
        )
        page = await context.new_page()

        try:
            logger.info(f"Navigating to {start_url}...")
            await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("networkidle")

            # Dismiss cookie consent modal if present
            try:
                cookie_btn = page.locator('button.accept-btn')
                await cookie_btn.wait_for(state="visible", timeout=5000)
                logger.info("Cookie consent modal detected. Dismissing it...")
                await cookie_btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(1)
            except Exception:
                logger.info("No cookie consent modal detected or it was auto-dismissed.")

            # Click pagination dropdown to select 100 items per page
            try:
                logger.info("Configuring pagination to 100 items per page...")
                dropdown = page.locator('div.ant-select[aria-label="How many results to show in list"]').first
                if await dropdown.count() > 0:
                    await dropdown.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(1)

                    # Locate option "100 per page"
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

            # Iterate pages
            current_page = 1
            while True:
                logger.info(f"Scraping items from page {current_page}...")
                
                try:
                    await page.wait_for_selector(".ant-list-item", timeout=15000)
                except Exception:
                    logger.warning(f"No list items found or load timed out on page {current_page}.")
                    break

                try:
                    # Evaluate in JS to get all page items including detail URLs from React Fiber
                    page_items = await page.evaluate('''
                        () => {
                            const cards = Array.from(document.querySelectorAll('li.ant-list-item'));
                            if (cards.length === 0) return [];

                            const firstContainer = document.querySelector('.discCardContainer');
                            let rawHits = null;
                            if (firstContainer) {
                                const keys = Object.keys(firstContainer);
                                const reactKey = keys.find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                                if (reactKey) {
                                    let curr = firstContainer[reactKey];
                                    while (curr) {
                                        if (curr.memoizedProps) {
                                            if (Array.isArray(curr.memoizedProps.hits)) {
                                                rawHits = curr.memoizedProps.hits;
                                                break;
                                            }
                                            if (curr.memoizedProps.rawData && curr.memoizedProps.rawData.hits && Array.isArray(curr.memoizedProps.rawData.hits.hits)) {
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
                                        const key = parts[0].trim().toLowerCase().replace(/\\s+/g, '_');
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
                                    detail_url: id ? `https://discover.sabinet.co.za/document/${id}` : null
                                });
                            });

                            return items;
                        }
                    ''')

                    logger.info(f"Extracted {len(page_items)} items on page {current_page}.")

                    stop_scraping = False
                    for item_data in page_items:
                        key = (item_data.get("award_number"), item_data.get("title"))
                        if key in existing_keys:
                            logger.info(f"Encountered already scraped item: '{item_data.get('title')}' (Award: {item_data.get('award_number')}). Stopping incremental scrape.")
                            stop_scraping = True
                            break

                        extracted_data.append(item_data)

                    if stop_scraping:
                        break
                except Exception as page_err:
                    logger.error(f"Error extracting items on page {current_page}: {page_err}. Stopping traversal to save progress.")
                    break

                # Go to next page
                next_btn = page.locator('a[rel="next"]').first
                if await next_btn.count() == 0:
                    next_btn = page.locator('a:has-text("Next")').first

                if await next_btn.count() > 0:
                    disabled = await next_btn.get_attribute("disabled")
                    classes = await next_btn.get_attribute("class") or ""
                    if disabled is not None or "disabled" in classes.lower():
                        logger.info("Pagination next button is disabled. Final page reached.")
                        break

                    if current_page % 100 == 0:
                        logger.info(f"Scraped {current_page} pages. Sleeping for 60 seconds to avoid detection...")
                        await asyncio.sleep(60)

                    logger.info("Navigating to next page...")
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    current_page += 1
                else:
                    logger.info("Pagination next button not found. Finishing.")
                    break

            # Combine new and old data
            combined_data = extracted_data + existing_data
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(combined_data, f, indent=4, ensure_ascii=False)

            logger.info(f"✅ Extraction completed successfully. Saved {len(combined_data)} total records ({len(extracted_data)} new) to {output_file}.")

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("Browser closed. Run complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Sabinet Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
