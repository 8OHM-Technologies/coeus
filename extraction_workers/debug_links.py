import asyncio
import os
import sys
from playwright.async_api import async_playwright
from misstcha import TurnstileSolver

async def debug_start():
    turnstile_solver = TurnstileSolver()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy={"server": "http://ooumozlx-rotate:aud9ea66yrrq@p.webshare.io:80"}
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        print("Navigating to target URL...")
        await page.goto("https://www.saflii.org/za/cases/ZALCJHB/", timeout=30000)
        
        solve_res = await turnstile_solver.solve(page)
        print("Solve status:", solve_res)
        await asyncio.sleep(5)
        
        title = await page.title()
        print("Title:", title)
        
        anchors = await page.locator("a").all()
        print(f"Found {len(anchors)} anchors:")
        for idx, a in enumerate(anchors):
            href = await a.get_attribute("href")
            text = (await a.inner_text()).strip()
            print(f"  [{idx}] href={href}, text={text}")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(debug_start())
