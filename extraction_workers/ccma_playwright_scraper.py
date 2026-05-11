import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from utils import download_pdf, fetch_pipeline_config

logger = logging.getLogger(__name__)

async def run_extraction(pipeline_name: str):
    # Fetch configuration from API
    config = await fetch_pipeline_config(pipeline_name)
    base_search_url = config["start_url"]
    cat_selector = config["target_css_selector_categories"]
    doc_selector = config["target_css_selector_documents"]
    
    # Standardized extraction params
    extraction_params = config.get("extraction_params", {})
    max_retries = int(extraction_params.get("max_retries", 3))
    retry_delay = int(extraction_params.get("retry_delay", 5))

    logger.info("==================================================")
    logger.info(f"🚀 COEUS PLAYWRIGHT WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Target URL: {base_search_url}")
    logger.info("==================================================")

    output_dir = os.path.join("/app/data/scraped_pdfs", config["document_type"].lower())
    os.makedirs(output_dir, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        context = await browser.new_context(
            ignore_https_errors=config.get("allow_insecure_https", False)
        )
        page = await context.new_page()

        try:
            for attempt in range(max_retries):
                try:
                    logger.info("Loading main page to extract categories...")
                    await page.goto(base_search_url, wait_until="networkidle")
                    break  # Exit retry loop if successful
                except Exception as e:
                    logger.warning(
                        f"Attempt {attempt + 1}/{max_retries}: Failed to navigate to {base_search_url}: {e}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                    else:
                        raise  # Re-raise if all retries fail

            await page.wait_for_selector(cat_selector, state="attached", timeout=15000)

            category_elements = await page.locator(cat_selector).all()
            categories = []

            for el in category_elements:
                cat_id = await el.get_attribute("data-value")
                cat_name = await el.inner_text()
                if cat_id:
                    categories.append((cat_id, cat_name.strip()))

            logger.info(f"Found {len(categories)} categories. Processing...")

            for cat_id, cat_name in categories:
                logger.info(f"Scraping Category: {cat_name} (ID: {cat_id})")
                cat_url = f"{base_search_url}?custom_p_type=resources&cat={cat_id}"

                for attempt in range(max_retries):
                    try:
                        # Navigate to the filtered category page and wait for JS to render the table
                        await page.goto(cat_url, wait_until="networkidle")
                        break  # Exit retry loop if successful
                    except Exception as e:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries}: Failed to navigate to {cat_url}: {e}"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                        else:
                            raise  # Re-raise if all retries fail

                # Try to find the document links. If none load after 5 seconds, it's likely empty.
                try:
                    await page.wait_for_selector(doc_selector, timeout=5000)
                except Exception:
                    logger.warning(
                        f"  No documents found for {cat_name} or table failed to load."
                    )
                    continue

                links = await page.locator(doc_selector).all()
                for link in links:
                    pdf_url = await link.get_attribute("data-path")
                    doc_name = await link.get_attribute("data-name")

                    if pdf_url:
                        absolute_url = urljoin(base_search_url, pdf_url)
                        # We hand the URL off to the requests function to actually download it
                        download_pdf(
                            absolute_url,
                            output_dir,
                            doc_name or "Unknown_Document",
                            allow_insecure_requests=config.get(
                                "allow_insecure_requests", False
                            ),
                        )

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("✅ Extraction Complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus CCMA Playwright Worker")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
