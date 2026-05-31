import argparse
import asyncio
import os
import sys
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from utils import download_pdf, fetch_pipeline_config, logger


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    target_url = config["start_url"]

    # Standardized extraction params
    extraction_params = config.get("extraction_params", {})
    filter_keyword = extraction_params.get("filter_keyword", "Johannesburg")
    doc_selector = config.get("target_css_selector_documents") or 'a[href*="download="]'

    logger.info("==================================================")
    logger.info(f"🚀 COEUS JUDICIARY WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {target_url}")
    logger.info(f"Filter: {filter_keyword}")
    logger.info("==================================================")

    # Setup output directory
    output_dir = os.path.join("/app/data/scraped_pdfs", config["document_type"].lower())
    os.makedirs(output_dir, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False),
        )
        page = await context.new_page()

        try:
            logger.info(f"Navigating to: {target_url}")
            # Use domcontentloaded for faster, more reliable initial load
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

            # 1. Set Limit to 'All'
            logger.info("Waiting for 'limit' select element...")
            limit_select = page.locator('select[name="limit"]')
            await limit_select.wait_for(state="visible", timeout=30000)

            logger.info("Setting 'limit' to 'All' (value=0)...")
            await limit_select.select_option(value="0")

            # Instead of networkidle, wait for the table or just a brief moment
            logger.info("Waiting for page to update with all items...")
            await page.wait_for_load_state("load", timeout=60000)
            await asyncio.sleep(
                2
            )  # Brief pause to allow JS to finish rendering table rows

            # 2. Extract and Download Links
            logger.info(f"Extracting links containing '{filter_keyword}'...")

            # Find all links using the configured selector
            all_links = await page.locator(doc_selector).all()

            found_items = []
            for link in all_links:
                href = await link.get_attribute("href")
                if not href:
                    continue

                text = (await link.inner_text()).strip()
                link_title = (await link.get_attribute("title") or "").strip()

                # Check if the text or parent text contains the filter keyword
                is_match = False
                if (
                    filter_keyword.lower() in text.lower()
                    or filter_keyword.lower() in href.lower()
                ):
                    is_match = True
                else:
                    # Check parent container text as a fallback
                    # Using optional chaining to prevent null reference errors
                    parent_text = await link.evaluate(
                        "el => el.parentElement?.innerText || ''"
                    )
                    if filter_keyword.lower() in parent_text.lower():
                        is_match = True

                if is_match:
                    # Determine the best document name
                    # Avoid generic "Download.pdf" or "Download" text
                    is_generic = text.lower() in [
                        "download.pdf",
                        "download",
                        "click here",
                        "view",
                    ]

                    if is_generic and link_title:
                        doc_name = link_title.strip().replace(" ", "_")
                        logger.info(
                            f"  [i] Generic text '{text}' detected, using title: {doc_name}"
                        )
                    elif is_generic:
                        # If title is also missing, try to get text from the table row (parent)
                        # Using optional chaining to prevent null reference errors
                        parent_text = await link.evaluate(
                            "el => el.closest('tr')?.innerText || ''"
                        )
                        # Clean parent text: take first line or first 50 chars
                        row_context = (
                            parent_text.split("\n")[0].strip()[:50]
                            if parent_text
                            else ""
                        )
                        if row_context and not any(
                            g in row_context.lower() for g in ["download", "view"]
                        ):
                            doc_name = row_context.replace(" ", "_")
                            logger.info(
                                f"  [i] Generic text detected and no title, using row context: {doc_name}"
                            )
                        else:
                            # Fallback to a part of the URL if everything else is generic
                            doc_name = (
                                href.split("download=")[-1]
                                .split("&")[0]
                                .replace(":", "_")
                            )
                            logger.info(
                                f"  [i] Generic text detected, no title/context, using URL ID: {doc_name}"
                            )
                    else:
                        doc_name = text or href.split("=")[-1]

                    found_items.append((urljoin(target_url, href), doc_name))

            # Unique links only
            found_items = list(set(found_items))

            if not found_items:
                logger.warning(f"No download links found matching '{filter_keyword}'.")
            else:
                logger.info(
                    f"✅ Found {len(found_items)} document links. Starting downloads..."
                )
                for url, name in found_items:
                    download_pdf(
                        url,
                        output_dir,
                        name,
                        allow_insecure_requests=config.get(
                            "allow_insecure_requests", False
                        ),
                    )

        except Exception as e:
            logger.error(f"❌ Scraping failed: {str(e)}")
            sys.exit(1)

        finally:
            await browser.close()
            logger.info("Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Judiciary Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
