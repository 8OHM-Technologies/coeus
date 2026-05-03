# /extraction_worker/ccma_playwright_scraper.py
import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import urljoin

import requests
import urllib3
from playwright.async_api import async_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}


def download_pdf(url: str, save_dir: str, file_name: str):
    """Downloads a PDF using requests to bypass SSL issues."""
    try:
        response = requests.get(
            url, headers=HEADERS, stream=True, timeout=30, verify=False
        )
        response.raise_for_status()

        safe_name = "".join(
            [c for c in file_name if c.isalpha() or c.isdigit() or c == " "]
        ).rstrip()
        safe_name = safe_name.replace(" ", "_") + ".pdf"
        file_path = os.path.join(save_dir, safe_name)

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"  [+] Downloaded: {safe_name}")
    except Exception as e:
        logger.error(f"  [!] Failed to download {file_name}: {e}")


async def run_extraction(target_url: str):
    logger.info("==================================================")
    logger.info("🚀 COEUS PLAYWRIGHT WORKER INITIALIZED (CCMA)")
    logger.info(f"Target: {target_url}")
    logger.info("==================================================")

    output_dir = "/app/data/scraped_pdfs/ccma_reports"
    os.makedirs(output_dir, exist_ok=True)

    base_search_url = "https://www.ccma.org.za/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            logger.info("Loading main page to extract categories...")
            await page.goto(base_search_url, wait_until="networkidle")

            await page.wait_for_selector(
                ".search-form-submenu a.select_value", state="attached", timeout=15000
            )

            category_elements = await page.locator(
                ".search-form-submenu a.select_value"
            ).all()
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

                # Navigate to the filtered category page and wait for JS to render the table
                await page.goto(cat_url, wait_until="networkidle")

                # Try to find the document links. If none load after 5 seconds, it's likely empty.
                try:
                    await page.wait_for_selector("a.click-counter-link", timeout=5000)
                except Exception:
                    logger.warning(
                        f"  No documents found for {cat_name} or table failed to load."
                    )
                    continue

                links = await page.locator("a.click-counter-link").all()
                for link in links:
                    pdf_url = await link.get_attribute("data-path")
                    doc_name = await link.get_attribute("data-name")

                    if pdf_url:
                        absolute_url = urljoin(base_search_url, pdf_url)
                        # We hand the URL off to the requests function to actually download it
                        download_pdf(
                            absolute_url, output_dir, doc_name or "Unknown_Document"
                        )

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("✅ Extraction Complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus CCMA Playwright Worker")
    parser.add_argument("--url", required=True, help="The target URL to scrape")
    args = parser.parse_args()

    asyncio.run(run_extraction(args.url))
