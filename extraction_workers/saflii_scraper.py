import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

import requests
from utils import HEADERS, fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    target_url = config.get("start_url")
    if not target_url:
        logger.error("No start_url configured in pipeline config.")
        sys.exit(1)

    allow_insecure_requests = config.get("allow_insecure_requests", False)
    extraction_params = config.get("extraction_params", {})
    start_year = int(extraction_params.get("start_year", 2000))
    current_year = datetime.now().year
    end_year = int(extraction_params.get("end_year", current_year))

    # Configure optional proxy support
    proxies = {}
    proxy_url = os.getenv("SCRAPING_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if proxy_url:
        logger.info(f"Using proxy for requests: {proxy_url}")
        proxies = {"http": proxy_url, "https": proxy_url}

    logger.info("==================================================")
    logger.info(f"COEUS SAFLII WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Base URL: {target_url}")
    logger.info(f"Year Range: {start_year} - {end_year}")
    logger.info("==================================================")

    # Determine court/folder name from the URL path
    parsed = urlparse(target_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    court_name = path_parts[-1] if path_parts else "SAFLII"

    # Setup output directory
    document_type = config.get("document_type", "saflii")
    output_dir = os.path.join("/app/data/scraped_pdfs", document_type.lower())
    if not os.path.exists("/app/data") and not os.path.exists("/app"):
        output_dir = os.path.join("data", "scraped_pdfs", document_type.lower())
    os.makedirs(output_dir, exist_ok=True)

    for year in range(start_year, end_year + 1):
        logger.info(f"Processing Year: {year}")
        seq = 1
        while True:
            # Build clean filename and path
            file_name = f"{court_name}_{year}_{seq}.pdf"
            file_path = os.path.join(output_dir, file_name)

            # Optimisation: If file exists, skip request and move to next sequence
            if os.path.exists(file_path):
                logger.info(f"  [-] Skipping (already exists): {file_name}")
                seq += 1
                continue

            # Target URL
            pdf_url = f"{target_url.rstrip('/')}/{year}/{seq}.pdf"
            logger.info(f"Checking URL: {pdf_url}")

            try:
                # Use stream=True to only fetch headers first
                response = requests.get(
                    pdf_url,
                    headers=HEADERS,
                    stream=True,
                    timeout=30,
                    verify=not allow_insecure_requests,
                    proxies=proxies,
                )

                if response.status_code == 404:
                    logger.info(f"Received 404 for {pdf_url}. Moving to next year.")
                    break

                if response.status_code == 403:
                    logger.error(
                        f"Received 403 Forbidden for {pdf_url}. This is likely a Cloudflare Turnstile block. "
                        "Please configure SCRAPING_PROXY, HTTP_PROXY, or user cookies in the environment."
                    )
                    break

                response.raise_for_status()

                # Verify if it's actually a PDF by checking content type
                content_type = response.headers.get("Content-Type", "")
                if "html" in content_type.lower():
                    logger.warning(
                        f"URL {pdf_url} returned HTML content instead of PDF. Assuming end of sequence for the year."
                    )
                    break

                # Download the PDF file in chunks
                with open(file_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                logger.info(f"  [+] Downloaded: {file_name}")

            except requests.exceptions.RequestException as e:
                logger.error(f"  [!] Request failed for {pdf_url}: {e}")
                # Break to avoid infinite loops on network/server errors
                break

            seq += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus SAFLII Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
