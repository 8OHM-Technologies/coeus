import asyncio
import logging
import sys
import os

from playwright.async_api import async_playwright
from misstcha import TurnstileSolver
from new_saflii_scraper import check_page_state, wait_for_page_load

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--window-size=1280,720",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        url = "https://www.saflii.org/za/cases/ZALCJHB/2001/10.html"
        logger.info(f"Navigating to {url}")
        
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await wait_for_page_load(page, "case")

        title = await page.title()
        try:
            h1 = await page.locator("h1").first.inner_text(timeout=2000)
        except:
            h1 = ""
        try:
            body = await page.locator("body").inner_text(timeout=2000)
        except:
            body = ""

        state = check_page_state(title, h1, body)
        logger.info(f"Page state: {state}")

        if state == "BLOCKED":
            logger.info("Attempting Turnstile solve...")
            solver = TurnstileSolver()
            screenshot_dir = "/app/data/test_screenshots"
            os.makedirs(screenshot_dir, exist_ok=True)
            res = await solver.solve(page, screenshot_dir=screenshot_dir)
            logger.info(f"Solve result: {res}")
            
            await wait_for_page_load(page, "case")
            
            title = await page.title()
            logger.info(f"Post-solve title: {title}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
