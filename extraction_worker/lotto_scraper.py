# /extraction_worker/lotto_playwright_scraper.py
import argparse
import asyncio
import csv
import logging
import os
import sys

from playwright.async_api import async_playwright

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


async def run_extraction(target_url: str):
    logger.info("==================================================")
    logger.info("🚀 COEUS PLAYWRIGHT WORKER INITIALIZED (LOTTO)")
    logger.info(f"Target Base: {target_url}")
    logger.info("==================================================")

    output_dir = "/app/data/lotto_results"
    os.makedirs(output_dir, exist_ok=True)

    start_year = 2000
    end_year = 2026
    all_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()

        try:
            for year in range(start_year, end_year + 1):
                logger.info(f"Scraping Year: {year}")

                # Construct the specific archive URL for the year
                # Ensuring clean url joining whether target_url ends in a slalsh or not
                year_url = f"{target_url.rstrip('/')}/{year}-archive"

                await page.goto(year_url, wait_until="networkidle")

                try:
                    await page.wait_for_selector("table tbody", timeout=5000)
                except Exception:
                    logger.warning(
                        f"  No data table found for {year} or page failed to load."
                    )
                    continue

                # Fetch all rows asynchronously
                rows = await page.locator("table tbody tr").all()
                logger.info(f"  Found {len(rows)} draws for {year}. Extracting...")

                for row in rows:
                    columns = await row.locator("td").all_inner_texts()

                    # We now check for >= 4 to ensure we have Date, Numbers, Jackpot, and Outcome
                    # This safely ignores the "Next Lotto Jackpot" promo row
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
            save_to_csv(all_results, output_dir, "historical_lotto_results.csv")

        except Exception as e:
            logger.error(f"❌ Playwright extraction failed: {str(e)}")
            sys.exit(1)
        finally:
            await browser.close()
            logger.info("✅ Extraction Complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus Lotto Playwright Worker")
    # You can pass "https://za.national-lottery.com/lotto/results" as the URL
    parser.add_argument("--url", required=True, help="The target base URL to scrape")
    args = parser.parse_args()

    asyncio.run(run_extraction(args.url))
