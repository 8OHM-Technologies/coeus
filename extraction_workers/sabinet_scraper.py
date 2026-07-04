import argparse
import asyncio
import json
import logging
import os
import sys
from playwright.async_api import async_playwright
from .utils.utils import fetch_pipeline_config

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
    """Interactively log in to Sabinet via Google and persist the browser session.

    The function saves two copies of the storage state JSON:
    - ``data/state.json`` – a generic location used by the other scrapers.
    - ``data/sabinet_ccma/html/state.json`` – retained for backward‑compatibility.
    """
    # Target file paths
    save_path_root = os.path.join("data", "state.json")
    save_path_ccma = os.path.join("data", "sabinet_ccma", "html", "state.json")

    os.makedirs(os.path.join("data", "sabinet_ccma", "html"), exist_ok=True)

    async with async_playwright() as p:
        logger.info("\n[INFO] Launching headed Chromium browser for authentication...")
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        logger.info("💡 Navigating to Sabinet CCMA Awards page...")
        await page.goto("https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards")

        logger.info("\n=================== ACTION REQUIRED ===================")
        logger.info("1. Click the 'Sign in' link at the top‑right of the page.")
        logger.info("2. Choose 'Sign in with Google' and complete the auth flow.")
        logger.info("3. Once logged in and redirected back, verify you can see the content.")
        logger.info("=======================================================\n")
        input("\n👉 Press ENTER here in the terminal once you've successfully logged in to save cookies...")

        # Persist the session state
        await context.storage_state(path=save_path_root)
        await context.storage_state(path=save_path_ccma)
        logger.info("\n✅ Success! Session state saved to:")
        logger.info(f"   - {save_path_root}")
        logger.info(f"   - {save_path_ccma}")

        await browser.close()
        logger.info("Browser closed.")

# -----------------------------------------------------------------------------
# Index Scraper – extracts the list of awards (or other documents)
# -----------------------------------------------------------------------------
async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    start_url = config.get("start_url") or "https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards"

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
        # Resolve storage state (cookies) – look for a file inside the output dir first, then fall back.
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

            # Set pagination to 100 items per page where possible
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

            # Page traversal
            current_page = 1
            while True:
                logger.info(f"Scraping items from page {current_page}...")
                try:
                    await page.wait_for_selector('.ant-list-item', timeout=15000)
                except Exception:
                    logger.warning(f"No list items found or load timed out on page {current_page}.")
                    break

                # Extract items via a JS evaluation – mirrors original logic
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

                # Pagination – next button handling
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

            # Merge with any previously existing data and write out
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

                    page_info = await page.evaluate('''
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
                        "scraped_at": str(asyncio.get_event_loop().time()),
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
        choices=["auth", "index", "details"],
        help="Which part of the workflow to execute: auth generates session state, index scrapes the list, details enriches records.",
    )
    parser.add_argument(
        "--pipeline_name",
        required=False,
        help="Name of the pipeline configuration (required for index and details stages).",
    )

    args = parser.parse_args()

    if args.stage == "auth":
        asyncio.run(generate_state())
    elif args.stage == "index":
        if not args.pipeline_name:
            logger.error("--pipeline_name is required for the index stage.")
            sys.exit(1)
        asyncio.run(run_extraction(args.pipeline_name))
    elif args.stage == "details":
        if not args.pipeline_name:
            logger.error("--pipeline_name is required for the details stage.")
            sys.exit(1)
        asyncio.run(run_detail_extraction(args.pipeline_name))
