import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from utils import fetch_pipeline_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

FETCH_PDF_JS = """
    async (url) => {
        try {
            const response = await fetch(url);
            const status = response.status;
            const contentType = response.headers.get("content-type") || "";

            if (status === 200 && contentType.toLowerCase().includes("application/pdf")) {
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) {
                    binary += String.fromCharCode(bytes[i]);
                }
                return {
                    "success": true,
                    "status": status,
                    "content_type": contentType,
                    "data": btoa(binary)
                };
            } else {
                return {
                    "success": false,
                    "status": status,
                    "content_type": contentType,
                    "data": null
                };
            }
        } catch (err) {
            return {
                "success": false,
                "status": 500,
                "content_type": "",
                "error": err.toString(),
                "data": null
            };
        }
    }
"""

EXTRACT_METADATA_JS = """
    () => {
        const h1El = document.querySelector('h1');

        const expandButtons = document.querySelectorAll('.ant-typography-expand');
        expandButtons.forEach(btn => {
            try { btn.click(); } catch(e) {}
        });

        const metadata = {};
        const rows = document.querySelectorAll('.ant-row');
        rows.forEach(row => {
            const labelEl = row.querySelector('.metaDataLabel');
            const valueEl = row.querySelector('.metaDataValue');
            if (labelEl && valueEl) {
                const labelText = labelEl.innerText.trim();
                if (!labelText) return;

                let value = "";

                const listItems = Array.from(valueEl.querySelectorAll('.ant-list-items .ant-list-item'));
                if (listItems.length > 0) {
                    value = listItems.map(li => li.innerText.trim()).filter(Boolean);
                } else {
                    const ellipsisEl = valueEl.querySelector('.ant-typography-ellipsis[aria-label]');
                    if (ellipsisEl) {
                        value = ellipsisEl.getAttribute('aria-label').trim();
                    } else {
                        value = valueEl.innerText.trim();
                    }
                }

                const key = labelText
                    .toLowerCase()
                    .replace(/[^a-z0-9_]/g, '_')
                    .replace(/_+/g, '_')
                    .trim()
                    .replace(/^_+|_+$/g, '');

                if (key) {
                    metadata[key] = value;
                }
            }
        });

        return {
            h1: h1El ? h1El.innerText.trim() : "",
            metadata: metadata
        };
    }
"""


async def find_turnstile_frame(page):
    for _ in range(10):
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                return frame
        await asyncio.sleep(0.5)
    return None


async def solve_turnstile_checkbox(page, turnstile_frame):
    logger.info("  [!] Cloudflare Turnstile challenge detected. Attempting to solve...")
    await asyncio.sleep(7)

    frame_el = await turnstile_frame.frame_element()
    if not frame_el:
        return False

    box = await frame_el.bounding_box()
    if not box:
        return False

    click_x = box["x"] + 30
    click_y = box["y"] + 32
    logger.info(f"  [!] Clicking verification checkbox at ({click_x}, {click_y})")
    await page.mouse.move(click_x, click_y)
    await asyncio.sleep(0.3)
    await page.mouse.click(click_x, click_y)
    return True


async def wait_for_metadata_after_turnstile(page):
    for _ in range(40):
        await asyncio.sleep(1)
        if await page.locator(".metaDataLabel").count() > 0:
            return True
        if not await find_turnstile_frame(page):
            return await page.locator(".metaDataLabel").count() > 0
    return False


async def scrape_case_metadata(page, case_url):
    """Navigate to the case page and extract metadata fields."""
    candidate_urls = [case_url]
    if not case_url.endswith(".html"):
        candidate_urls.append(f"{case_url}.html")

    response = None
    status = None
    resolved_url = case_url
    for url in candidate_urls:
        response = await page.goto(url, timeout=30000)
        if not response:
            continue
        status = response.status
        if status != 404:
            resolved_url = url
            break

    if not response:
        logger.warning(f"No response received for {case_url}.")
        return None, None

    if status == 404:
        return None, 404

    turnstile_frame = await find_turnstile_frame(page)
    if turnstile_frame:
        await solve_turnstile_checkbox(page, turnstile_frame)
        await wait_for_metadata_after_turnstile(page)
    else:
        try:
            await page.wait_for_selector(".metaDataLabel", timeout=15000)
        except Exception:
            pass

    page_info = await page.evaluate(EXTRACT_METADATA_JS)
    record = {
        "case_url": resolved_url,
        "title": page_info.get("h1", ""),
        **page_info.get("metadata", {}),
    }
    return record, status


async def download_pdf(
    page,
    pdf_url,
    pdf_data,
    pdf_status,
    current_pdf_url,
):
    """Download PDF bytes from pdf_url using navigation and in-page fetch fallback."""
    current_pdf_url["url"] = pdf_url
    pdf_data["bytes"] = None
    pdf_status["status"] = None
    pdf_status["content_type"] = None

    response = await page.goto(pdf_url, timeout=30000)
    if not response:
        return None, None, None

    status = response.status
    content_type = response.headers.get("content-type", "").lower()

    if status == 404:
        return None, 404, content_type

    turnstile_frame = await find_turnstile_frame(page)
    if turnstile_frame:
        await solve_turnstile_checkbox(page, turnstile_frame)
        logger.info("  [!] Click sent. Starting polling fetch loop...")

        for _ in range(40):
            await asyncio.sleep(1)
            if pdf_data["bytes"]:
                break

            try:
                res = await page.evaluate(FETCH_PDF_JS, pdf_url)
                if res.get("success"):
                    pdf_data["bytes"] = base64.b64decode(res["data"])
                    break

                pdf_status["status"] = res.get("status")
                pdf_status["content_type"] = res.get("content_type", "")

                if pdf_status["status"] == 404 or (
                    pdf_status["status"] == 200
                    and "html" in pdf_status["content_type"].lower()
                ):
                    break
            except Exception as e:
                logger.debug(f"Fetch loop evaluation failed: {e}")
    else:
        for _ in range(3):
            if pdf_data["bytes"]:
                break
            await asyncio.sleep(1)

        if not pdf_data["bytes"]:
            try:
                res = await page.evaluate(FETCH_PDF_JS, pdf_url)
                if res.get("success"):
                    pdf_data["bytes"] = base64.b64decode(res["data"])
                else:
                    pdf_status["status"] = res.get("status")
                    pdf_status["content_type"] = res.get("content_type", "")
            except Exception as e:
                logger.debug(f"Direct fetch evaluation failed: {e}")

    if pdf_data["bytes"]:
        return pdf_data["bytes"], status, content_type

    final_status = pdf_status["status"] or status
    final_content_type = pdf_status["content_type"] or content_type
    return None, final_status, final_content_type


async def run_extraction(pipeline_name: str, headless: bool = False):
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

    proxy_url = os.getenv("SCRAPING_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")

    logger.info("==================================================")
    logger.info(f"COEUS SAFLII WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Base URL: {target_url}")
    logger.info(f"Year Range: {start_year} - {end_year}")
    logger.info("==================================================")

    parsed = urlparse(target_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    court_name = path_parts[-1] if path_parts else "SAFLII"

    base_data_dir = "/app/data" if os.path.exists("/app/data") else "data"
    output_dir = os.path.join(base_data_dir, "scraped_pdfs", "saflii")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {os.path.abspath(output_dir)}")

    async with async_playwright() as p:
        logger.info(f"Launching Playwright browser (headless={headless})...")
        launch_kwargs = {"headless": headless}
        if proxy_url:
            logger.info(f"Using proxy for browser: {proxy_url}")
            launch_kwargs["proxy"] = {"server": proxy_url}

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=allow_insecure_requests,
        )
        page = await context.new_page()

        current_pdf_url = {"url": ""}
        pdf_data = {"bytes": None}
        pdf_status = {"status": None, "content_type": None}

        async def on_response(response):
            try:
                if response.url == current_pdf_url["url"]:
                    pdf_status["status"] = response.status
                    pdf_status["content_type"] = response.headers.get("content-type", "").lower()

                content_type = response.headers.get("content-type", "").lower()
                if "application/pdf" in content_type and response.status == 200:
                    body = await response.body()
                    if body.startswith(b"%PDF"):
                        pdf_data["bytes"] = body
            except Exception:
                pass

        page.on("response", on_response)

        for year in range(start_year, end_year + 1):
            logger.info(f"Processing Year: {year}")
            seq = 1
            while True:
                file_name = f"{court_name}_{year}_{seq}.pdf"
                file_path = os.path.join(output_dir, file_name)
                json_name = f"{court_name}_{year}_{seq}.json"
                json_path = os.path.join(output_dir, json_name)

                pdf_exists = os.path.exists(file_path)
                json_exists = os.path.exists(json_path)

                if pdf_exists and json_exists:
                    logger.info(f"  [-] Skipping (PDF and JSON exist): {file_name}")
                    seq += 1
                    continue

                need_pdf = not pdf_exists
                need_json = not json_exists

                case_url = f"{target_url.rstrip('/')}/{year}/{seq}"
                pdf_url = f"{case_url}.pdf"

                if pdf_exists and need_json:
                    logger.info(f"  [~] PDF exists, backfilling metadata: {json_name}")
                elif json_exists and need_pdf:
                    logger.info(f"  [~] JSON exists, downloading PDF: {file_name}")
                else:
                    logger.info(f"  [~] Fetching PDF and metadata for sequence {seq}")

                if need_json:
                    logger.info(f"Scraping metadata: {case_url}")
                if need_pdf:
                    logger.info(f"Checking PDF URL: {pdf_url}")

                try:
                    if need_json:
                        metadata, case_status = await scrape_case_metadata(page, case_url)
                        if case_status == 404:
                            if pdf_exists:
                                logger.warning(
                                    f"Case page 404 for {case_url}, but PDF exists. Skipping metadata for this sequence."
                                )
                                seq += 1
                                continue
                            logger.info(f"Received 404 for {case_url}. Moving to next year.")
                            break

                        if metadata:
                            metadata["pdf_url"] = pdf_url
                            with open(json_path, "w", encoding="utf-8") as f:
                                json.dump(metadata, f, indent=2, ensure_ascii=False)
                            logger.info(f"  [+] Saved metadata: {json_name}")
                        else:
                            logger.warning(f"  [!] No metadata extracted for {case_url}")
                            if not need_pdf:
                                seq += 1
                                continue

                    if need_pdf:
                        pdf_bytes, pdf_result_status, pdf_content_type = await download_pdf(
                            page, pdf_url, pdf_data, pdf_status, current_pdf_url
                        )

                        if pdf_bytes:
                            with open(file_path, "wb") as f:
                                f.write(pdf_bytes)
                            logger.info(f"  [+] Downloaded: {file_name}")
                        else:
                            if pdf_result_status == 404:
                                logger.warning(
                                    f"URL {pdf_url} returned 404. Assuming end of sequence for the year."
                                )
                                break
                            if pdf_result_status == 403:
                                logger.error(f"Persistent 403 Forbidden for {pdf_url}. Exiting scraper.")
                                sys.exit(1)
                            if pdf_content_type and "html" in pdf_content_type:
                                logger.warning(
                                    f"URL {pdf_url} returned HTML content (status: {pdf_result_status}). Retrying."
                                )
                                await asyncio.sleep(2)
                                continue

                            logger.error(
                                f"  [!] Failed to download PDF for {pdf_url} (status: {pdf_result_status})"
                            )
                            seq += 1
                            continue

                except Exception as e:
                    logger.error(f"  [!] Request failed for {case_url}: {e}")
                    sys.exit(1)

                seq += 1

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coeus SAFLII Scraper")
    parser.add_argument(
        "--pipeline_name",
        required=True,
        help="The name of the pipeline configuration to use",
    )
    parser.add_argument(
        "--headless",
        default="false",
        help="Run browser in headless mode (true or false, defaults to false)",
    )
    args = parser.parse_args()

    headless_value = args.headless.lower() == "true"
    asyncio.run(run_extraction(args.pipeline_name, headless=headless_value))
