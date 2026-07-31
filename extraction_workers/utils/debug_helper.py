import logging
from typing import Any

logger = logging.getLogger(__name__)

async def log_browser_proxy_ip(page_or_sb: Any, step_label: str, use_proxy: bool):
    """Log the outbound public IP of a Playwright page or SeleniumBase driver context."""
    if not use_proxy:
        logger.info(f"ℹ️ [{step_label}] Direct Connection (Proxy Disabled)")
        return
    
    if page_or_sb is None:
        logger.warning(f"⚠️ [{step_label}] Cannot check outbound IP: Driver context is None.")
        return

    # SeleniumBase driver (sb)
    if hasattr(page_or_sb, "open") and hasattr(page_or_sb, "get_text"):
        try:
            page_or_sb.open("https://ipv4.webshare.io/")
            ip = (page_or_sb.get_text("body") or "").strip()
            logger.info(f"🌐 [{step_label}] Browser Outbound IP (via proxy): {ip}")
        except Exception as e:
            logger.warning(f"⚠️ [{step_label}] Could not fetch browser proxy IP: {e}")
        return

    # Playwright page
    if hasattr(page_or_sb, "context") and hasattr(page_or_sb.context, "new_page"):
        try:
            test_page = await page_or_sb.context.new_page()
            await test_page.goto("https://ipv4.webshare.io/", timeout=10000)
            ip = (await test_page.locator("body").inner_text()).strip()
            logger.info(f"🌐 [{step_label}] Browser Outbound IP (via proxy): {ip}")
            await test_page.close()
        except Exception as e:
            logger.warning(f"⚠️ [{step_label}] Could not fetch browser proxy IP: {e}")
        return


def log_sb_proxy_ip_sync(sb: Any, step_label: str, use_proxy: bool):
    """Synchronous helper to log outbound IP for SeleniumBase drivers."""
    if not use_proxy:
        logger.info(f"ℹ️ [{step_label}] Direct Connection (Proxy Disabled)")
        return
    try:
        sb.open("https://ipv4.webshare.io/")
        ip = (sb.get_text("body") or "").strip()
        logger.info(f"🌐 [{step_label}] Browser Outbound IP (via proxy): {ip}")
    except Exception as e:
        logger.warning(f"⚠️ [{step_label}] Could not fetch browser proxy IP: {e}")