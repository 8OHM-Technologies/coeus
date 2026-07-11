import argparse
import asyncio
import csv
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


def save_to_csv(data: list, save_dir: str, file_name: str):
    """Saves the extracted lotto dictionary data to a CSV file."""
    if not data:
        logger.warning("  [!] No data to save.")
        return

    file_path = os.path.join(save_dir, file_name)
    try:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["Year", "Date", "Winning Numbers", "Jackpot", "Outcome"]
            )
            writer.writeheader()
            writer.writerows(data)
        logger.info(f"  [+] Data successfully saved to: {file_path}")
    except Exception as e:
        logger.error(f"  [!] Failed to save CSV {file_name}: {e}")


async def run_extraction(pipeline_name: str):
    config = await fetch_pipeline_config(pipeline_name)
    target_url = config["start_url"]
    extraction_params = config.get("extraction_params", {})
    
    start_year = extraction_params.get("start_year", 2000)
    end_year = extraction_params.get("end_year", 2026)
    
    # Standardized selector
    doc_selector = "table tbody tr"

    logger.info("==================================================")
    logger.info(f"🚀 COEUS PLAYWRIGHT WORKER INITIALIZED (LOTTO: {pipeline_name})")
    logger.info(f"Target Base: {target_url}")
    logger.info(f"Years: {start_year} - {end_year}")
    logger.info(f"Selector: {doc_selector}")
    logger.info("==================================================")

    output_dir = "/app/data/lotto_results"
    os.makedirs(output_dir, exist_ok=True)

    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            ignore_https_errors=config.get("allow_insecure_https", True)
        )
        page = await context.new_page()

        try:
            for year in range(int(start_year), int(end_year) + 1):
                logger.info(f"Scraping Year: {year}")

                # Construct the specific archive URL for the year
                year_url = f"{target_url.rstrip('/')}/{year}-archive"

                await page.goto(year_url, wait_until="networkidle")

                try:
                    # Wait for the container of the documents/rows
                    container_selector = doc_selector.split(" ")[0] if " " in doc_selector else doc_selector
                    await page.wait_for_selector(container_selector, timeout=5000)
                except Exception:
                    logger.warning(
                        f"  Selector '{doc_selector}' not found for {year} or page failed to load."
                    )
                    continue

                # Fetch all rows using the configured selector
                rows = await page.locator(doc_selector).all()
                logger.info(f"  Found {len(rows)} items for {year}. Extracting...")

                for row in rows:
                    columns = await row.locator("td").all_inner_texts()

                    if len(columns) >= 4:
                        # Clean the <br> tag out of the date
                        date = columns[0].replace("\n", " ").strip()

                        # Clean the newlines out of the <ul> list of balls
                        winning_numbers = columns[1].replace("\n", " ").strip()

                        jackpot = columns[2].strip()
                        outcome = columns[3].strip()

                        all_results.append(
                            {
                                "Year": year,
                                "Date": date,
                                "Winning Numbers": winning_numbers,
                                "Jackpot": jackpot,
                                "Outcome": outcome,
                            }
                        )

            # Save all scraped results after the loop finishes
            logger.info(f"Extraction finished. Total draws scraped: {len(all_results)}")
            save_to_csv(
                all_results, output_dir, f"historical_lotto_results_{pipeline_name}.csv"
            )

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("✅ Extraction Complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Lotto Playwright Worker")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    args = parser.parse_args()

    asyncio.run(run_extraction(args.pipeline_name))
