# /extraction_worker/ccma_scraper.py
import argparse
import logging
import os
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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
    """Downloads a PDF from a given URL and saves it to the specified directory."""
    try:
        response = requests.get(url, headers=HEADERS, stream=True, timeout=30)
        response.raise_for_status()

        # Ensure filename is safe and ends in .pdf
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


def run_extraction(target_url: str):
    logger.info("==================================================")
    logger.info("🚀 COEUS BS4 WORKER INITIALIZED (LIGHTWEIGHT)")
    logger.info(f"Target: {target_url}")
    logger.info("==================================================")

    # COEUS standard mount path
    output_dir = "/app/scraped_pdfs/ccma_reports"
    os.makedirs(output_dir, exist_ok=True)

    try:
        logger.info("Fetching categories...")
        response = requests.get(target_url, headers=HEADERS, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        cat_list = soup.find("ul", attrs={"name": "cat"})

        if not cat_list:
            logger.error("Could not find the category list in the HTML.")
            sys.exit(1)

        categories = {}
        for a_tag in cat_list.find_all("a", class_="select_value"):
            cat_id = a_tag.get("data-value")
            cat_name = a_tag.text.strip()
            if cat_id:
                categories[cat_id] = cat_name

        logger.info(f"Found {len(categories)} categories. Beginning extraction...")

        for cat_id, cat_name in categories.items():
            logger.info(f"Scraping Category: {cat_name} (ID: {cat_id})")
            cat_url = f"{target_url}?custom_p_type=resources&cat={cat_id}"
            cat_resp = requests.get(cat_url, headers=HEADERS, timeout=15)

            if cat_resp.status_code != 200:
                logger.warning(f"Failed to fetch category {cat_name}. Skipping.")
                continue

            cat_soup = BeautifulSoup(cat_resp.text, "html.parser")

            for a_tag in cat_soup.find_all("a", class_="click-counter-link"):
                pdf_url = a_tag.get("data-path")
                doc_name = a_tag.get("data-name", "Unknown Document")

                if pdf_url:
                    # Resolve relative URLs if necessary
                    absolute_url = urljoin(target_url, pdf_url)
                    download_pdf(absolute_url, output_dir, doc_name)

            time.sleep(1)  # Polite delay

    except Exception as e:
        logger.error(f"❌ Scraping failed: {str(e)}")
        sys.exit(1)

    logger.info("✅ Extraction Complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Lightweight BS4 Worker")
    parser.add_argument("--url", required=True, help="The target URL to scrape")
    args = parser.parse_args()

    run_extraction(args.url)
