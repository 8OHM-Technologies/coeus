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


async def run_detail_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    
    # Paths resolution
    base_dir = "/app/data"
    if not os.path.exists(base_dir) and not os.path.exists("/app"):
        base_dir = "data"
        
    doc_type = config.get("document_type", "awards").lower()
    
    # Resolve the index pipeline name from config parameters or strip suffix conventions
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

    # 1. Load cases list index
    if not os.path.exists(input_file):
        logger.error(f"❌ Input index file {input_file} not found. Please run the index scraper first.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        cases = json.load(f)

    logger.info(f"Loaded {len(cases)} cases from index.")

    # 2. Load existing details if present (for incremental scraping)
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

    # Filter out already scraped cases
    pending_cases = [c for c in cases if c.get("detail_url") and c["detail_url"] not in scraped_urls]
    logger.info(f"Found {len(pending_cases)} pending cases to scrape.")

    if not pending_cases:
        logger.info("✅ All cases are already scraped. Exiting.")
        return

    # 3. Playwright execution
    async with async_playwright() as p:
        # Check for storage state (cookies/session) to bypass login/SSO
        storage_state_path = os.path.join(base_dir, index_pipeline_name, doc_type, "state.json")
        if not os.path.exists(storage_state_path):
            # Fall back to root data state.json
            fallback_path = os.path.join(base_dir, "state.json")
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
            count = 0
            for case_item in pending_cases:
                url = case_item["detail_url"]
                logger.info(f"[{count + 1}/{len(pending_cases)}] Scraping details from: {url}")

                try:
                    # Navigate to detail page
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_load_state("networkidle")

                    # Dismiss cookie consent on first load / if modal pops up
                    if count == 0 or count % 50 == 0:
                        try:
                            cookie_btn = page.locator('button.accept-btn').first
                            if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                                await cookie_btn.click()
                                await page.wait_for_load_state("networkidle")
                        except Exception:
                            pass

                    # Extract page elements
                    page_info = await page.evaluate('''
                        () => {
                            const h1El = document.querySelector('h1');
                            
                            // 1. Try to click any expand buttons to ensure full text is loaded in DOM if not in aria-label
                            const expandButtons = document.querySelectorAll('.ant-typography-expand');
                            expandButtons.forEach(btn => {
                                try { btn.click(); } catch(e) {}
                            });
                            
                            // 2. Extract all metadata rows
                            const metadata = {};
                            const rows = document.querySelectorAll('.ant-row');
                            rows.forEach(row => {
                                const labelEl = row.querySelector('.metaDataLabel');
                                const valueEl = row.querySelector('.metaDataValue');
                                if (labelEl && valueEl) {
                                    const labelText = labelEl.innerText.trim();
                                    if (!labelText) return;
                                    
                                    let value = "";
                                    
                                    // Check for list items
                                    const listItems = Array.from(valueEl.querySelectorAll('.ant-list-items .ant-list-item'));
                                    if (listItems.length > 0) {
                                        value = listItems.map(li => li.innerText.trim()).filter(Boolean);
                                    } else {
                                        // Check for ellipsis with aria-label
                                        const ellipsisEl = valueEl.querySelector('.ant-typography-ellipsis[aria-label]');
                                        if (ellipsisEl) {
                                            value = ellipsisEl.getAttribute('aria-label').trim();
                                        } else {
                                            value = valueEl.innerText.trim();
                                        }
                                    }
                                    
                                    // Map label text to snake_case key
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
                                metadata: metadata
                            };
                        }
                    ''')

                    detail_record = {
                        **case_item,
                        "extracted_h1": page_info.get("h1"),
                        "extracted_main_content": page_info.get("main_content"),
                        "raw_preview_text": page_info.get("raw_preview_text"),
                        **page_info.get("metadata", {}),
                        "scraped_at": str(asyncio.get_event_loop().time())  # Timestamp helper
                    }

                    existing_details.append(detail_record)
                    count += 1

                    # Save incrementally every 10 records to safeguard against crashes
                    if count % 10 == 0:
                        with open(output_file, "w", encoding="utf-8") as f:
                            json.dump(existing_details, f, indent=4, ensure_ascii=False)
                        logger.info(f"Saved {count} records incrementally to {output_file}.")

                    # Throttling to respect rate limits
                    await asyncio.sleep(2)
                    
                    if count % 100 == 0:
                        logger.info("Scraped 100 details pages. Throttling: sleeping for 60 seconds...")
                        await asyncio.sleep(60)

                except Exception as page_err:
                    logger.error(f"Failed to scrape detail page {url}: {page_err}")
                    await asyncio.sleep(5)  # Pause longer on error before next attempt

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Sabinet Detail Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_detail_extraction(args.pipeline_name))
