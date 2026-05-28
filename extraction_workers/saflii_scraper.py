import argparse
import asyncio
import base64
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

    # Configure optional proxy support
    proxy_url = os.getenv("SCRAPING_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")

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
    output_dir = os.path.join("/app/data/scraped_pdfs", "saflii")
    if not os.path.exists("/app/data") and not os.path.exists("/app"):
        output_dir = os.path.join("data", "scraped_pdfs", "saflii")
    os.makedirs(output_dir, exist_ok=True)

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
            ignore_https_errors=allow_insecure_requests
        )
        page = await context.new_page()

        fetch_js = """
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

        current_pdf_url = ""
        pdf_data = {"bytes": None}
        pdf_status = {"status": None, "content_type": None}

        async def on_response(response):
            try:
                if response.url == current_pdf_url:
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

                # Update closure variables for response listener
                current_pdf_url = pdf_url
                pdf_data["bytes"] = None
                pdf_status["status"] = None
                pdf_status["content_type"] = None

                try:
                    response = await page.goto(pdf_url, timeout=30000)
                    if not response:
                        logger.warning(f"No response received for {pdf_url}. Skipping.")
                        seq += 1
                        continue

                    status = response.status
                    content_type = response.headers.get("content-type", "").lower()

                    if status == 404:
                        logger.info(f"Received 404 for {pdf_url}. Moving to next year.")
                        break

                    # Check for Turnstile
                    is_turnstile = False
                    turnstile_frame = None
                    for _ in range(10):
                        for frame in page.frames:
                            if "challenges.cloudflare.com" in frame.url:
                                turnstile_frame = frame
                                is_turnstile = True
                                break
                        if is_turnstile:
                            break
                        await asyncio.sleep(0.5)

                    if is_turnstile:
                        logger.info("  [!] Cloudflare Turnstile challenge detected. Attempting to solve...")
                        # Sleep 7 seconds for the spinner to finish and reveal the checkbox
                        await asyncio.sleep(7)

                        frame_el = await turnstile_frame.frame_element()
                        if frame_el:
                            box = await frame_el.bounding_box()
                            if box:
                                click_x = box["x"] + 30
                                click_y = box["y"] + 32
                                logger.info(f"  [!] Clicking verification checkbox at ({click_x}, {click_y})")
                                await page.mouse.move(click_x, click_y)
                                await asyncio.sleep(0.3)
                                await page.mouse.click(click_x, click_y)
                                logger.info("  [!] Click sent. Starting polling fetch loop...")

                                # Poll up to 40 seconds for PDF response or solve failure
                                for i in range(40):
                                    await asyncio.sleep(1)
                                    if pdf_data["bytes"]:
                                        break
                                    
                                    # Fallback: evaluate in-page fetch request
                                    try:
                                        res = await page.evaluate(fetch_js, pdf_url)
                                        if res.get("success"):
                                            pdf_data["bytes"] = base64.b64decode(res["data"])
                                            break
                                        
                                        pdf_status["status"] = res.get("status")
                                        pdf_status["content_type"] = res.get("content_type", "")
                                        
                                        # Break early on definitive 404 or redirect to HTML
                                        if pdf_status["status"] == 404 or (pdf_status["status"] == 200 and "html" in pdf_status["content_type"].lower()):
                                            break
                                    except Exception as e:
                                        logger.debug(f"Fetch loop evaluation failed: {e}")
                    else:
                        # Direct navigation: Wait up to 3 seconds for on_response to capture
                        for _ in range(3):
                            if pdf_data["bytes"]:
                                break
                            await asyncio.sleep(1)
                        
                        # If still no bytes, try evaluating in-page fetch
                        if not pdf_data["bytes"]:
                            try:
                                res = await page.evaluate(fetch_js, pdf_url)
                                if res.get("success"):
                                    pdf_data["bytes"] = base64.b64decode(res["data"])
                                else:
                                    pdf_status["status"] = res.get("status")
                                    pdf_status["content_type"] = res.get("content_type", "")
                            except Exception as e:
                                logger.debug(f"Direct fetch evaluation failed: {e}")

                    if pdf_data["bytes"]:
                        with open(file_path, "wb") as f:
                            f.write(pdf_data["bytes"])
                        logger.info(f"  [+] Downloaded: {file_name}")
                    else:
                        final_status = pdf_status["status"] or status
                        final_content_type = pdf_status["content_type"] or content_type

                        if final_status == 404:
                            logger.warning(
                                f"URL {pdf_url} returned 404. Assuming end of sequence for the year."
                            )
                            break
                        elif final_status == 403:
                            logger.error(f"Persistent 403 Forbidden for {pdf_url}. Exiting scraper.")
                            sys.exit(1)
                        elif final_content_type and "html" in final_content_type:
                            logger.warning(
                                f"URL {pdf_url} returned HTML content (status: {final_status}). Retrying the iteration."
                            )
                            await asyncio.sleep(2)
                            continue
                        else:
                            logger.error(f"  [!] Failed to download PDF for {pdf_url} (status: {final_status})")
                            seq += 1
                            continue

                except Exception as e:
                    logger.error(f"  [!] Request failed for {pdf_url}: {e}")
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
