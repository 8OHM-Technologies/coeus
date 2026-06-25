import logging

logger = logging.getLogger(__name__)

async def log_browser_proxy_ip(page, step_label: str, use_proxy: bool):
    if not use_proxy:
        return
    try:
        test_page = await page.context.new_page()
        await test_page.goto("https://ipv4.webshare.io/", timeout=10000)
        ip = (await test_page.locator("body").inner_text()).strip()
        logger.info(f"[{step_label}] Browser Outbound IP (via proxy): {ip}")
        await test_page.close()
    except Exception as e:
        logger.warning(f"[{step_label}] Could not fetch browser proxy IP: {e}")