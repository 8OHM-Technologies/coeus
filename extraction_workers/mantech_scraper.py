import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(
    pipeline_name: str, search_keyword: str = "", category: str = ""
):
    # Fetch configuration from API or environment
    config = await fetch_pipeline_config(pipeline_name)
    start_url = config.get("start_url", "https://www.mantech.co.za/Categories.aspx")

    output_dir = os.path.join(
        "/app/data", pipeline_name, config["document_type"].lower()
    )
    os.makedirs(output_dir, exist_ok=True)

    extraction_params = config.get("extraction_params", {})
    # Use CLI search_keyword/category if provided, otherwise fallback to config
    if not search_keyword:
        search_keyword = extraction_params.get("search_keyword")

    if not category:
        category = extraction_params.get("category")

    # Determine execution targets based on priority
    # If search_keyword is provided, ignore all categories
    categories = []
    if search_keyword:
        loops = [{"search_keyword": search_keyword, "category": None}]
    else:
        if category:
            if isinstance(category, list):
                categories = [str(c).strip() for c in category if str(c).strip()]
            elif isinstance(category, str):
                # Try parsing as JSON list, fallback to splitting by comma
                stripped = category.strip()
                if (stripped.startswith("[") and stripped.endswith("]")) or (
                    stripped.startswith("{") and stripped.endswith("}")
                ):
                    try:
                        parsed = json.loads(category)
                        if isinstance(parsed, list):
                            categories = [
                                str(c).strip() for c in parsed if str(c).strip()
                            ]
                        elif isinstance(parsed, dict):
                            categories = [
                                str(c).strip() for c in parsed.values() if str(c).strip()
                            ]
                        else:
                            categories = [str(parsed).strip()]
                    except Exception:
                        categories = [
                            c.strip() for c in category.split(",") if c.strip()
                        ]
                else:
                    categories = [c.strip() for c in category.split(",") if c.strip()]
            else:
                categories = [str(category).strip()]

        if categories:
            loops = [{"search_keyword": None, "category": cat} for cat in categories]
        else:
            loops = [{"search_keyword": None, "category": None}]

    # Standardized Selectors from DB or fallback to defaults
    doc_selector = config.get("target_css_selector_documents") or "a[id*='HyperLink1_']"

    logger.info("==================================================")
    logger.info(f"🚀 COEUS MANTECH WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {start_url}")
    logger.info(f"Search Keyword: {search_keyword}")
    logger.info(f"Categories: {categories}")
    logger.info(f"Execution Loops: {len(loops)}")
    logger.info("==================================================")

    product_urls = []

    async with async_playwright() as p:
        # headless=True is faster, but keep False if you want to watch it work
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            ignore_https_errors=config.get("allow_insecure_https", False)
        )
        page = await context.new_page()

        try:
            for idx, loop in enumerate(loops):
                current_category = loop["category"]
                current_search = loop["search_keyword"]

                logger.info(
                    f"Processing target {idx + 1}/{len(loops)} (Category: {current_category}, Search: {current_search})"
                )
                try:
                    # 1. Navigate to the base URL
                    logger.info(f"Navigating to {start_url}...")
                    await page.goto(start_url)

                    # 2. Select Category (If provided)
                    if current_category:
                        logger.info(f"Selecting category '{current_category}'...")
                        await page.locator(
                            "#ContentPlaceHolder1_ListBox1"
                        ).select_option(current_category)
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(2)

                    # 3. Execute Search (If provided)
                    if current_search:
                        logger.info(f"Searching for '{current_search}'...")
                        await page.locator("input[name='ctl00$SearchTextBox']").fill(
                            current_search
                        )
                        await page.locator("input[name='ctl00$SearchButton']").click()
                        await page.wait_for_load_state("networkidle")

                    # 4. Change pagination to 100 items
                    logger.info("Setting pagination to 100 items...")
                    if (
                        await page.locator(
                            "#ContentPlaceHolder1_PagerDropDownList"
                        ).count()
                        > 0
                    ):
                        await page.locator(
                            "#ContentPlaceHolder1_PagerDropDownList"
                        ).select_option("100")
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(
                            2
                        )  # Give the WebForms grid time to re-render

                    # --- DYNAMIC PAGINATION CALCULATION ---
                    max_pages = 1
                    record_label = page.locator("#ContentPlaceHolder1_Label62")
                    if await record_label.count() > 0:
                        label_text = await record_label.inner_text()
                        # Expected format: "Displaying records 1 to 100 of 294"
                        logger.info(f"Record label found: '{label_text}'")
                        match = re.search(r"of\s+(\d+)", label_text)
                        if match:
                            total_records = int(match.group(1))
                            max_pages = math.ceil(total_records / 100)
                            logger.info(
                                f"Parsed {total_records} total records. Calculated {max_pages} pages."
                            )
                        else:
                            logger.warning(
                                f"Could not parse record count from: '{label_text}'. Defaulting to 1 page."
                            )
                    else:
                        logger.warning(
                            "Record count label (#ContentPlaceHolder1_Label62) not found. Defaulting to 1 page."
                        )

                    # 5. Extract URLs across pages
                    for current_page in range(max_pages):
                        logger.info(
                            f"Extracting links from table page {current_page + 1}..."
                        )

                        # Target the Stock Code links
                        links = await page.locator(doc_selector).all()

                        for link in links:
                            href = await link.get_attribute("href")
                            if href and "ProductInfo.aspx" in href:
                                full_url = urljoin("https://www.mantech.co.za/", href)
                                product_urls.append(full_url)

                        # Check if we need to click the "Next" button for more table pages
                        if current_page < max_pages - 1:
                            next_btn = page.locator("a:has-text('Next')").first
                            if await next_btn.is_visible():
                                await next_btn.click()
                                await page.wait_for_load_state("networkidle")
                                await asyncio.sleep(2)
                            else:
                                logger.info("No more pages available.")
                                break
                except Exception as e:
                    logger.error(
                        f"Error gathering URLs for category '{current_category}' / search '{current_search}': {e}"
                    )

            # Deduplicate URLs just in case
            product_urls = list(set(product_urls))
            logger.info(
                f"Successfully found {len(product_urls)} products. Starting deep scrape...\n"
            )

            # --- Helper functions for safe extraction ---
            async def get_text(selector):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return (await locator.first.inner_text()).strip()
                return None

            async def get_attr(selector, attribute):
                locator = page.locator(selector)
                if await locator.count() > 0:
                    return await locator.first.get_attribute(attribute)
                return None

            # 5. Visit each product page independently
            extracted_data = []
            base_url = "https://www.mantech.co.za/"

            for url in product_urls:
                logger.info(f"Scraping: {url}")
                try:
                    await page.goto(url)
                    await page.wait_for_load_state("domcontentloaded")

                    # Extract Flat Data fields based on their specific ASP.NET IDs
                    product_data = {
                        "source_url": url,
                        "stock_code": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label1"
                        ),
                        "part_number": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label2"
                        ),
                        "description": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label3"
                        ),
                        "manufacturer": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label5"
                        ),
                        "sold_in": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label7"
                        ),
                        "moq": await get_text("#ContentPlaceHolder1_FormView1_Label8"),
                        "alt_part_number": await get_text(
                            "#ContentPlaceHolder1_FormView1_Label4"
                        ),
                        "memo": await get_text("#ContentPlaceHolder1_FormView1_Label9"),
                    }

                    # Extract Media & Documents
                    img_href = await get_attr(
                        "#ContentPlaceHolder1_FormView1_ImageLink", "href"
                    )
                    product_data["image_url"] = (
                        urljoin(base_url, img_href) if img_href else None
                    )

                    datasheet_href = await get_attr(
                        "a[id*='GridView2_HyperLink4']", "href"
                    )
                    product_data["datasheet_url"] = (
                        urljoin(base_url, datasheet_href) if datasheet_href else None
                    )

                    # Extract Pricing Tiers dynamically
                    pricing_tiers = []
                    price_rows = await page.locator(
                        "#ContentPlaceHolder1_FormView1_Panel2 table tr"
                    ).all()

                    for row in price_rows:
                        cols = await row.locator("td").all()
                        if len(cols) >= 2:
                            qty_range = (
                                (await cols[0].inner_text()).replace("\n", " ").strip()
                            )
                            price = (
                                (await cols[1].inner_text()).replace("\n", "").strip()
                            )

                            if "to" in qty_range:
                                pricing_tiers.append({"qty": qty_range, "price": price})

                    product_data["pricing"] = pricing_tiers
                    extracted_data.append(product_data)

                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Error scraping {url}: {e}")

                await asyncio.sleep(1)

            # Output the results
            output_file = f"{output_dir}/{pipeline_name}.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(extracted_data, f, indent=4, ensure_ascii=False)

            logger.info(
                f"✅ Done! Extracted {len(extracted_data)} products to {output_file}"
            )

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("✅ Extraction Complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Mantech Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    parser.add_argument(
        "--search_keyword",
        help="The search keyword to use (overrides config)",
    )
    parser.add_argument(
        "--category",
        help="The category or categories (comma-separated) to select (overrides config)",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name, args.search_keyword, args.category))
