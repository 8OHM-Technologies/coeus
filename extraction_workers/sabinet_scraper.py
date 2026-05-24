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

    extracted_data = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False)
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

            # Iterate pages
            current_page = 1
            while True:
                logger.info(f"Scraping items from page {current_page}...")
                
                try:
                    await page.wait_for_selector(".ant-list-item", timeout=15000)
                except Exception:
                    logger.warning(f"No list items found or load timed out on page {current_page}.")
                    break

                cards = await page.locator("li.ant-list-item").all()
                logger.info(f"Found {len(cards)} items on page {current_page}.")

                for card in cards:
                    try:
                        title_text = await card.evaluate('''
                            (el) => {
                                const titleEl = el.querySelector('.ant-list-item-meta-title');
                                if (!titleEl) return '';
                                const clone = titleEl.cloneNode(true);
                                const icons = clone.querySelectorAll('.anticon, [aria-label="lock"]');
                                icons.forEach(icon => icon.remove());
                                return clone.innerText.trim();
                            }
                        ''')

                        metadata = await card.evaluate('''
                            (el) => {
                                const tags = Array.from(el.querySelectorAll('.ant-list-item-meta-description .ant-tag'));
                                const data = {};
                                tags.forEach(tag => {
                                    const text = tag.innerText || '';
                                    if (text.includes(':')) {
                                        const parts = text.split(':');
                                        const key = parts[0].trim().toLowerCase().replace(/\\s+/g, '_');
                                        const val = parts.slice(1).join(':').trim();
                                        data[key] = val;
                                    }
                                });
                                return data;
                            }
                        ''')

                        extracted_data.append({
                            "title": title_text,
                            **metadata
                        })
                    except Exception as parse_err:
                        logger.error(f"Error parsing card details: {parse_err}")

                # Go to next page
                next_btn = page.locator("li.ant-pagination-next").first
                if await next_btn.count() > 0:
                    classes = await next_btn.get_attribute("class") or ""
                    aria_disabled = await next_btn.get_attribute("aria-disabled") or "false"
                    if "ant-pagination-disabled" in classes or aria_disabled == "true":
                        logger.info("Pagination next button is disabled. Final page reached.")
                        break

                    logger.info("Navigating to next page...")
                    await next_btn.click()
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(2)
                    current_page += 1
                else:
                    logger.info("Pagination next button not found. Finishing.")
                    break

            # Save the scraped data to file
            output_file = os.path.join(output_dir, f"{pipeline_name}.json")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(extracted_data, f, indent=4, ensure_ascii=False)

            logger.info(f"✅ Extraction completed successfully. Saved {len(extracted_data)} records to {output_file}.")

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
