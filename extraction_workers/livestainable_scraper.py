import argparse
import asyncio
import json
import logging
import os
import sys
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from .utils.utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(pipeline_name: str, search_keyword: str = ""):
    config = await fetch_pipeline_config(pipeline_name)
    start_url = config.get(
        "start_url",
        "https://livestainable.co.za/collections/vendors?sort_by=title-ascending&q=Keyestudio&filter.v.availability=1",
    )

    output_dir = os.path.join(
        "/app/data", pipeline_name, config["document_type"].lower()
    )
    os.makedirs(output_dir, exist_ok=True)

    extraction_params = config.get("extraction_params", {})
    if not search_keyword:
        search_keyword = extraction_params.get("search_keyword")

    # Default to 50 pages if not specified, to ensure we catch "all" products for most vendors
    max_pages = int(extraction_params.get("max_pages", 50))

    # Overrides config targeting if db has old fallback values
    doc_selector = ".product-item__title"

    logger.info("==================================================")
    logger.info(
        f"🚀 COEUS LIVESTAINABLE WORKER INITIALIZED (PIPELINE: {pipeline_name})"
    )
    logger.info(f"Target URL: {start_url}")
    logger.info(f"Search Keyword: {search_keyword}")
    logger.info("==================================================")

    product_urls = []
    base_url = "https://livestainable.co.za/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=config.get("allow_insecure_https", False)
        )
        page = await context.new_page()

        try:
            # 1. Navigate to base layout URL
            logger.info(f"Navigating to {start_url}...")
            await page.goto(start_url)
            await page.wait_for_load_state("domcontentloaded")

            # 2. Execute Search (Fallbacks on typical Shopify query identifiers if configured)
            if search_keyword:
                logger.info(f"Searching for '{search_keyword}'...")
                search_input = page.locator(
                    'input[name="q"], input[type="search"]'
                ).first
                if await search_input.is_visible():
                    await search_input.fill(search_keyword)
                    await search_input.press("Enter")
                    await page.wait_for_load_state("networkidle")
                else:
                    logger.warning(
                        "Search input element wasn't found on the starting landing page."
                    )

            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(1)

            # --- Helper to clear popups that block interactions ---
            async def clear_popups():
                await page.evaluate("""() => {
                    const selectors = [
                        '#omnisend-forms-wrapper',
                        '.omnisend-form-overlay',
                        '[id^="omnisend"]',
                        '#shopify-section-popup',
                        '.modal-overlay'
                    ];
                    selectors.forEach(s => {
                        document.querySelectorAll(s).forEach(el => el.remove());
                    });
                    document.body.classList.remove('no-scroll', 'modal-open');
                    document.documentElement.classList.remove('no-scroll');
                }""")

            # 3. Handle Product Collection Link Harvesting Loop
            for current_page in range(max_pages):
                logger.info(f"Extracting links from list page {current_page + 1}...")
                await clear_popups()

                links = await page.locator(doc_selector).all()
                for link in links:
                    href = await link.get_attribute("href")
                    if href and "/products/" in href:
                        full_url = urljoin(
                            base_url, href.split("?")[0]
                        )  # Strip tracking query strings
                        product_urls.append(full_url)

                if current_page < max_pages - 1:
                    # Specific selector for Livestainable/Shopify pagination
                    next_btn = page.locator("a.pagination__next, a[rel='next']")
                    if await next_btn.count() > 0 and await next_btn.first.is_visible():
                        logger.info(f"Navigating to page {current_page + 2}...")
                        try:
                            # Use force=True to bypass overlay interception
                            await next_btn.first.click(force=True, timeout=5000)
                        except Exception as click_err:
                            logger.warning(
                                f"Standard click failed, attempting JS click: {click_err}"
                            )
                            await next_btn.first.evaluate("el => el.click()")

                        await page.wait_for_load_state("domcontentloaded")
                        await asyncio.sleep(2)
                    else:
                        logger.info("No more pagination links available.")
                        break

            product_urls = list(set(product_urls))
            logger.info(
                f"Successfully tracked {len(product_urls)} target product records. Initiating page parsing...\n"
            )

            # --- Helper Extraction Logic ---
            async def get_text(selector):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return (
                        (await locator.first.inner_text())
                        .replace("Sale price", "")
                        .replace("Regular price", "")
                        .strip()
                    )
                return None

            async def get_attr(selector, attribute):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return await locator.first.get_attribute(attribute)
                return None

            # 4. Process deep data passes per item
            extracted_data = []

            for url in product_urls:
                logger.info(f"Scraping Item Spec: {url}")
                try:
                    # Optimization Step: Accessing Shopify json structures bypasses heavy UI layout elements entirely
                    json_endpoint = f"{url}.js" if not url.endswith(".js") else url
                    await page.goto(json_endpoint)

                    # Read directly from API payload
                    content_element = await page.locator("pre").inner_text()
                    raw_json = json.loads(content_element)

                    product_data = {
                        "source_url": url,
                        "title": raw_json.get("title"),
                        "part_number": raw_json.get("variants", [{}])[0].get("sku"),
                        "stock_code": raw_json.get("id"),
                        "description": raw_json.get("description"),
                        "manufacturer": raw_json.get("vendor"),
                        "image_url": urljoin(base_url, raw_json.get("featured_image")),
                        "pricing": [
                            {
                                "qty": "1+",
                                "price": f"R {raw_json.get('price', 0) / 100:.2f}",
                            }
                        ],
                        "compare_at_price": f"R {raw_json.get('compare_at_price', 0) / 100:.2f}"
                        if raw_json.get("compare_at_price")
                        else None,
                    }
                    extracted_data.append(product_data)

                except Exception as api_err:
                    logger.debug(
                        f"JSON endpoint fallback failed, trying legacy DOM parsing context: {api_err}"
                    )
                    # Fallback DOM Scraping Loop context if layout overrides the standard JSON route
                    try:
                        await page.goto(url)
                        await page.wait_for_load_state("domcontentloaded")

                        product_data = {
                            "source_url": url,
                            "title": await get_text("h1.product-meta__title"),
                            "part_number": await get_text(
                                ".product-meta__sku .product-meta__sku-number"
                            ),
                            "description": await get_text(".product-description"),
                            "manufacturer": await get_text(".product-meta__vendor"),
                            "image_url": await get_attr(
                                "img.product-gallery__image", "src"
                            ),
                            "pricing": [
                                {
                                    "qty": "1+",
                                    "price": await get_text(
                                        ".price--highlight, .price"
                                    ),
                                }
                            ],
                        }
                        extracted_data.append(product_data)
                    except Exception as e:
                        logger.error(
                            f"Failed handling DOM layout processing paths for execution url {url}: {e}"
                        )

                await asyncio.sleep(1)

            # 5. Flush results out to disk mount
            output_file = f"{output_dir}/{pipeline_name}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(extracted_data, f, indent=4, ensure_ascii=False)

            logger.info(
                f"✅ Data Extraction Complete! Saved {len(extracted_data)} outputs safely within storage matrix."
            )

        except Exception as e:
            logger.error(f"❌ Playwright engine fault encountered: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Livestainable Scraper Platform")
    parser.add_argument(
        "--pipeline_name", required=True, help="The pipeline deployment config token"
    )
    parser.add_argument(
        "--search_keyword", help="The target text lookup target filter execution values"
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name, args.search_keyword))
