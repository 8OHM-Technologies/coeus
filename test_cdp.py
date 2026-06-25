import asyncio
from playwright.async_api import async_playwright
import subprocess
import time
import socket

async def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    port = s.getsockname()[1]
    s.close()
    
    proc = subprocess.Popen([
        "google-chrome",
        f"--remote-debugging-port={port}",
        "--headless=new"
    ])
    
    time.sleep(2)
    
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://localhost:{port}")
        print("Connected:", browser)
        context = await browser.new_context(proxy={"server": "http://127.0.0.1:8080"})
        print("New context created with proxy")
        await browser.close()
        proc.terminate()

asyncio.run(main())
