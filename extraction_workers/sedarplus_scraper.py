import argparse
import asyncio
import logging
import os
import sys

from playwright.async_api import TimeoutError, async_playwright
from misstcha import HCaptchaSolver
from .utils.utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    target_url = config["start_url"]
    
    extraction_params = config.get("extraction_params", {})
    entity_identifier = extraction_params.get("entity_identifier", config["pipeline_id"])
    document_type = config.get("document_type", "Technical report (NI 43-101)")
    
    # Standardized Selectors from DB or extraction_params
    doc_type_selector = extraction_params.get("doc_type_selector", '#W926-fieldset textarea[type="search"]')
    profile_input_selector = extraction_params.get("profile_input_selector", 'Profile name or number')
    results_table_selector = "table tbody tr"

    logger.info("==================================================")
    logger.info(f"🚀 COEUS SEDARPLUS WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Entity: {entity_identifier}")
    logger.info(f"Doc Type: {document_type}")
    logger.info("==================================================")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=150)

        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            ignore_https_errors=config.get("allow_insecure_https", False),
        )
        page = await context.new_page()

        try:
            logger.info(f"Navigating to: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded")

            # 1. SOLVE HCAPTCHA
            logger.info("Solving hcaptcha...")
            captcha_solver = HCaptchaSolver()
            solve_result = await captcha_solver.solve_captcha(page, max_retries=3)

            if not solve_result["success"]:
                logger.error(f"Failed to solve hCaptcha: {solve_result['error']}")
                sys.exit(1)

            logger.info(f"Captcha bypassed in {solve_result['attempts']} attempts.")

            # 2. Select Document Type
            logger.info(f"Setting Document Type: {document_type}...")
            doc_type_field = page.locator(doc_type_selector)
            await doc_type_field.click()
            await page.get_by_role("option", name=document_type, exact=True).click()

            # 3. Handle Entity Profile
            logger.info(f"Entering Entity Identifier: {entity_identifier}...")
            profile_input = page.get_by_role("textbox", name=profile_input_selector)
            await profile_input.click()
            await profile_input.fill(entity_identifier)

            logger.info("Waiting for autocomplete dropdown...")
            try:
                autocomplete_item = page.locator("[id^='ui-id-']").first
                await autocomplete_item.wait_for(state="visible", timeout=15000)
                await autocomplete_item.click()
                logger.info("Successfully selected entity from autocomplete.")
            except TimeoutError:
                logger.warning(
                    "Autocomplete didn't appear. Attempting to proceed regardless..."
                )

            # 4. Execute Search
            logger.info("Clicking Search...")
            await page.get_by_role("button", name="Search").click()

            # 5. Extract Results
            logger.info(f"Waiting for results table to populate (selector: {results_table_selector})...")
            await page.wait_for_selector(results_table_selector, timeout=45000)

            document_rows = await page.locator(results_table_selector).all()
            pdf_urls = []

            for row in document_rows:
                links = await row.locator("a").all()
                for link in links:
                    href = await link.get_attribute("href")
                    if href and "document.html?id=" in href:
                        pdf_urls.append(href)

            if not pdf_urls:
                logger.warning("No PDF links found in the search results.")
            else:
                logger.info(f"✅ Found {len(pdf_urls)} document links:")
                for url in pdf_urls:
                    full_url = (
                        url
                        if url.startswith("http")
                        else f"https://www.sedarplus.ca{url}"
                    )
                    print(full_url)

        except Exception as e:
            logger.error(f"❌ Scraping failed: {str(e)}")
            sys.exit(1)

        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Sedarplus Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
