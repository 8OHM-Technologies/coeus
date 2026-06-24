import argparse
import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright
from utils import fetch_pipeline_config

from misstcha import TurnstileSolver

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    HAS_GDRIVE_LIBS = True
except ImportError:
    HAS_GDRIVE_LIBS = False

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
            title: document.title ? document.title.trim() : "",
            metadata: metadata
        };
    }
"""


def load_gdrive_credentials(extraction_params):
    if not HAS_GDRIVE_LIBS:
        raise ImportError(
            "Google client libraries (google-api-python-client, google-auth) are not installed."
        )

    scopes = ["https://www.googleapis.com/auth/drive"]

    creds_param = extraction_params.get("gdrive_credentials")
    if creds_param:
        if isinstance(creds_param, dict):
            logger.info(
                "Loading Google Drive credentials from dictionary in extraction_params."
            )
            return service_account.Credentials.from_service_account_info(
                creds_param, scopes=scopes
            )
        elif isinstance(creds_param, str):
            try:
                info = json.loads(creds_param)
                logger.info(
                    "Loading Google Drive credentials from JSON string in extraction_params."
                )
                return service_account.Credentials.from_service_account_info(
                    info, scopes=scopes
                )
            except json.JSONDecodeError:
                if os.path.exists(creds_param):
                    logger.info(
                        f"Loading Google Drive credentials from file path in extraction_params: {creds_param}"
                    )
                    return service_account.Credentials.from_service_account_file(
                        creds_param, scopes=scopes
                    )
                else:
                    logger.warning(
                        f"Credentials path in extraction_params does not exist: {creds_param}"
                    )

    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_path and os.path.exists(env_path):
        logger.info(
            f"Loading Google Drive credentials from GOOGLE_APPLICATION_CREDENTIALS: {env_path}"
        )
        return service_account.Credentials.from_service_account_file(
            env_path, scopes=scopes
        )

    search_paths = ["/app/data/gdrive_credentials.json"]
    for path in search_paths:
        if os.path.exists(path):
            logger.info(f"Loading Google Drive credentials from candidate path: {path}")
            return service_account.Credentials.from_service_account_file(
                path, scopes=scopes
            )

    raise FileNotFoundError("Google Drive credentials not found.")


def list_gdrive_files(service, folder_id):
    files = set()
    page_token = None
    while True:
        try:
            query = f"'{folder_id}' in parents and trashed = false"
            response = (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True
                )
                .execute()
            )
            for f in response.get("files", []):
                files.add(f["name"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        except Exception as e:
            logger.error(f"Error listing Google Drive files: {e}")
            raise
    return files


def upload_file_to_gdrive(service, file_path, folder_id):
    file_name = os.path.basename(file_path)
    logger.info(f"Uploading {file_name} to Google Drive...")

    if file_path.endswith(".pdf"):
        mime_type = "application/pdf"
    elif file_path.endswith(".json"):
        mime_type = "application/json"
    else:
        mime_type = "application/octet-stream"

    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    try:
        file_obj = (
            service.files()
            .create(
                body=file_metadata, 
                media_body=media, 
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        logger.info(
            f"Successfully uploaded {file_name} to Google Drive (ID: {file_obj.get('id')})"
        )
        return True
    except Exception as e:
        logger.error(f"Error uploading {file_name} to Google Drive: {e}")
        return False


def delete_file_from_gdrive(service, file_name, folder_id):
    try:
        query = f"'{folder_id}' in parents and name = '{file_name}' and trashed = false"
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            )
            .execute()
        )
        files = response.get("files", [])
        if not files:
            logger.info(f"File {file_name} not found on Google Drive (nothing to delete).")
            return True
        for f in files:
            file_id = f["id"]
            logger.info(f"Deleting {file_name} (ID: {file_id}) from Google Drive...")
            service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        return True
    except Exception as e:
        logger.error(f"Error deleting {file_name} from Google Drive: {e}")
        return False


turnstile_solver = TurnstileSolver()


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


async def wait_for_metadata_after_turnstile(page):
    for poll_sec in range(40):
        await asyncio.sleep(1)
        
        try:
            title = await page.title()
        except Exception:
            # Context destroyed or page navigation in progress, retry next poll
            continue
            
        try:
            h1 = await page.locator("h1").inner_text()
        except Exception:
            h1 = ""
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
            
        state = check_page_state(title, h1, body)
        if state == "NOT_FOUND":
            logger.info("Early exit from metadata wait: resolved to NOT_FOUND state.")
            return False

        if await page.locator(".metaDataLabel").count() > 0:
            return True
        if not await turnstile_solver.find_turnstile_frame(page):
            return await page.locator(".metaDataLabel").count() > 0
    return False


class BlockedException(Exception):
    pass


def check_page_state(page_title: str, h1_title: str, body_text: str) -> str:
    """
    Determine the logical state of the page.
    Returns:
        "BLOCKED": if it's a Cloudflare/Turnstile challenge page.
        "NOT_FOUND": if it's a SAFLII Apache 403 Forbidden, 404 Not Found, or similar non-existent page.
        "OK": if it's a valid page.
    """
    t_lower = (page_title or "").lower().strip()
    h_lower = (h1_title or "").lower().strip()
    b_lower = (body_text or "").lower().strip()

    # Check for Cloudflare block / Turnstile
    if (
        "just a moment" in t_lower
        or "cloudflare" in t_lower
        or "security verification" in t_lower
        or "verify you are human" in b_lower
        or "turnstile" in b_lower
    ):
        return "BLOCKED"

    # Check for SAFLII 404 / Apache Forbidden
    if (
        t_lower in ("not found", "page not found", "404 not found", "404 - not found", "404 error", "403 forbidden", "forbidden")
        or h_lower in ("not found", "page not found", "404 not found", "404 - not found", "404 error", "403 forbidden", "forbidden")
        or "you don't have permission to access this resource" in b_lower
    ):
        return "NOT_FOUND"

    return "OK"


async def scrape_case_metadata(page, case_url):
    """Navigate to the case page and extract metadata fields."""
    candidate_urls = [case_url]
    if not case_url.endswith(".html"):
        candidate_urls.append(f"{case_url}.html")

    response = None
    status = None
    resolved_url = case_url
    for url in candidate_urls:
        try:
            response = await page.goto(url, timeout=30000)
        except Exception as nav_err:
            err_str = str(nav_err)
            if "ERR_ABORTED" in err_str or "net::" in err_str:
                logger.warning(
                    f"Navigation aborted for {url} (possible download/redirect): {nav_err}. "
                    "Trying next candidate URL."
                )
                continue
            raise
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

    solve_res = await turnstile_solver.solve(page)
    if solve_res["success"]:
        await wait_for_metadata_after_turnstile(page)
    else:
        try:
            await page.wait_for_selector(".metaDataLabel", timeout=15000)
        except Exception:
            pass

    try:
        title = await page.title()
    except Exception:
        title = ""

    try:
        h1 = await page.locator("h1").inner_text()
    except Exception:
        h1 = ""

    try:
        body = await page.locator("body").inner_text()
    except Exception:
        body = ""

    state = check_page_state(title, h1, body)
    if state == "BLOCKED":
        logger.warning(f"Cloudflare Turnstile challenge not bypassed for {resolved_url}.")
        return None, 403
    elif state == "NOT_FOUND":
        logger.warning(f"Case page 404/Not Found (Apache Forbidden/Not Found) for {resolved_url}.")
        return None, 404

    # State is OK, extract metadata
    page_info = await page.evaluate(EXTRACT_METADATA_JS)
    h1_title = page_info.get("h1") or h1 or ""
    page_title = page_info.get("title") or title or ""

    resolved_title = h1_title.strip() if h1_title.strip() else (page_title.strip() if page_title.strip() else "Unknown Title")

    record = {
        "case_url": resolved_url,
        "title": resolved_title,
        **page_info.get("metadata", {}),
    }
    return record, 200



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

    try:
        response = await page.goto(pdf_url, timeout=30000)
        status = response.status if response else None
        content_type = response.headers.get("content-type", "").lower() if response else None
    except Exception as e:
        logger.debug(f"goto raised exception (likely download/redirect): {e}")
        response = None
        status = None
        content_type = None

    # Immediately check if browser download event or response listener captured the bytes!
    if pdf_data["bytes"]:
        return pdf_data["bytes"], 200, "application/pdf"

    if status == 404:
        return None, 404, content_type

    solve_res = await turnstile_solver.solve(page)

    # Clear status from the initial challenge page response so it doesn't pollute subsequent status checks
    pdf_status["status"] = None
    pdf_status["content_type"] = None

    logger.info("  [!] Starting PDF download/polling loop...")
    # Poll up to 30 seconds for the download or response listener to capture the PDF
    for poll_sec in range(30):
        await asyncio.sleep(1)
        if pdf_data["bytes"]:
            break

        # Check if browser received a response indicating a terminal state
        if pdf_status["status"] == 404:
            logger.info(f"Browser response resolved to 404 for {pdf_url}")
            break

        # Check the current page content state
        try:
            title = await page.title()
        except Exception as e:
            # Context destroyed, navigation in progress, etc. Let's try again in the next second.
            logger.debug(f"Failed to get page title at second {poll_sec+1}: {e}. Continuing...")
            continue

        try:
            h1 = await page.locator("h1").inner_text()
        except Exception:
            h1 = ""
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""

        state = check_page_state(title, h1, body)
        if state == "BLOCKED":
            # Still blocked / challenge page is active. Keep waiting.
            if poll_sec % 5 == 0:
                logger.debug(f"Waiting for challenge page to transition (current state: BLOCKED)...")
            continue

        if state == "NOT_FOUND":
            logger.info(f"Browser response resolved to HTML 'Not Found' / Apache Forbidden (404/403) for {pdf_url}")
            pdf_status["status"] = 404
            break

    # Last resort fallback: if we still don't have the PDF bytes, try in-page JS fetch
    if not pdf_data["bytes"] and pdf_status["status"] != 404:
        logger.info("  [!] PDF not captured by browser. Attempting in-page fetch fallback...")
        try:
            res = await page.evaluate(FETCH_PDF_JS, pdf_url)
            if res.get("success"):
                pdf_data["bytes"] = base64.b64decode(res["data"])
                logger.info("  [!] In-page fetch fallback succeeded.")
            else:
                fetch_status = res.get("status")
                fetch_content_type = res.get("content_type", "")
                logger.warning(f"  [!] In-page fetch fallback returned status {fetch_status}")
                if fetch_status:
                    pdf_status["status"] = fetch_status
                    pdf_status["content_type"] = fetch_content_type
        except Exception as e:
            logger.debug(f"Direct fetch evaluation failed: {e}")

    if pdf_data["bytes"]:
        return pdf_data["bytes"], 200, "application/pdf"

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

    proxy_url = None
    use_proxy = (
        config.get("use_proxy", False)
        or config.get("extraction_params", {}).get("use_proxy", False)
        or os.getenv("USE_PROXY", "False").lower() == "true"
    )
    WEBSHARE_PROXY = "http://ooumozlx-rotate:aud9ea66yrrq@p.webshare.io:80/"

    if use_proxy:
        # Mask credentials in proxy URL for logs
        masked_proxy = WEBSHARE_PROXY
        if "@" in WEBSHARE_PROXY:
            parts = WEBSHARE_PROXY.split("@")
            creds_part = parts[0].split("://")
            scheme = creds_part[0]
            user = creds_part[1].split(":")[0]
            host_part = parts[1]
            masked_proxy = f"{scheme}://{user}:****@{host_part}"
        logger.info(f"Using rotating proxy configuration: {masked_proxy}")

        logger.info(
            "Proxy is ENABLED — verifying connectivity via Webshare rotating proxy..."
        )
        try:
            ip = requests.get(
                "https://ipv4.webshare.io/",
                proxies={"http": WEBSHARE_PROXY, "https": WEBSHARE_PROXY},
                timeout=15,
            ).text.strip()
            logger.info(f"Proxy active. Outbound IP: {ip}")
        except Exception as proxy_err:
            logger.error(f"Proxy connectivity check failed: {proxy_err}")
            sys.exit(1)
        proxy_url = WEBSHARE_PROXY

    logger.info("==================================================")
    logger.info(f"COEUS SAFLII WORKER INITIALIZED (PIPELINE: {pipeline_name})")
    logger.info(f"Base URL: {target_url}")
    logger.info(f"Year Range: {start_year} - {end_year}")
    logger.info("==================================================")

    parsed = urlparse(target_url)
    path_parts = [p for p in parsed.path.split("/") if p]
    court_name = path_parts[-1] if path_parts else "SAFLII"

    document_type = config.get("document_type", "pdf").lower()
    base_data_dir = "/app/data" if os.path.exists("/app/data") else "data"
    output_dir = os.path.join(base_data_dir, pipeline_name, document_type)
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {os.path.abspath(output_dir)}")

    gdrive_folder_id = extraction_params.get("gdrive_folder_id")
    gdrive_delete_local = extraction_params.get("gdrive_delete_local", False)
    gdrive_service = None
    existing_files = set()

    if gdrive_folder_id:
        logger.info("Google Drive integration is enabled. Initializing deduplication set from Google Drive...")
        try:
            creds = load_gdrive_credentials(extraction_params)
            gdrive_service = build("drive", "v3", credentials=creds)
            logger.info("Fetching existing files from Google Drive folder...")
            gdrive_files = list_gdrive_files(gdrive_service, gdrive_folder_id)
            logger.info(f"Found {len(gdrive_files)} existing files on Google Drive.")
            existing_files.update(gdrive_files)
        except Exception as e:
            logger.error(f"Failed to initialize Google Drive: {e}")
            sys.exit(1)
    else:
        logger.info("Google Drive integration is not enabled. Initializing deduplication set from local output directory...")
        for fname in os.listdir(output_dir):
            if fname.endswith((".pdf", ".json")):
                existing_files.add(fname)
        logger.info(f"Found {len(existing_files)} existing local files.")

    async with async_playwright() as p:
        logger.info(f"Launching Playwright browser (headless={headless})...")
        launch_kwargs = {"headless": headless}
        if proxy_url:
            parsed_proxy = urlparse(proxy_url)
            server_url = f"{parsed_proxy.scheme}://{parsed_proxy.hostname}"
            if parsed_proxy.port:
                server_url += f":{parsed_proxy.port}"
            
            proxy_config = {"server": server_url}
            if parsed_proxy.username:
                proxy_config["username"] = parsed_proxy.username
            if parsed_proxy.password:
                proxy_config["password"] = parsed_proxy.password
                
            masked_log = server_url
            if parsed_proxy.username:
                masked_log = f"{parsed_proxy.scheme}://{parsed_proxy.username}:****@{parsed_proxy.hostname}"
                if parsed_proxy.port:
                    masked_log += f":{parsed_proxy.port}"
            logger.info(f"Using proxy for browser: {masked_log}")
            launch_kwargs["proxy"] = proxy_config

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            ignore_https_errors=allow_insecure_requests,
            accept_downloads=True,
        )
        page = await context.new_page()

        await log_browser_proxy_ip(page, "Startup", use_proxy)

        current_pdf_url = {"url": ""}
        pdf_data = {"bytes": None}
        pdf_status = {"status": None, "content_type": None}

        async def on_response(response):
            try:
                if response.url == current_pdf_url["url"]:
                    pdf_status["status"] = response.status
                    pdf_status["content_type"] = response.headers.get(
                        "content-type", ""
                    ).lower()

                content_type = response.headers.get("content-type", "").lower()
                if "application/pdf" in content_type and response.status == 200:
                    body = await response.body()
                    if body.startswith(b"%PDF"):
                        pdf_data["bytes"] = body
            except Exception:
                pass

        async def on_download(download):
            try:
                path = await download.path()
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        body = f.read()
                    if body.startswith(b"%PDF"):
                        pdf_data["bytes"] = body
                        pdf_status["status"] = 200
                        pdf_status["content_type"] = "application/pdf"
                        logger.info("PDF captured via browser download event.")
            except Exception as e:
                logger.warning(f"Failed to read download: {e}")

        page.on("response", on_response)
        page.on("download", on_download)

        for year in range(start_year, end_year + 1):
            logger.info(f"Processing Year: {year}")
            await log_browser_proxy_ip(page, f"Year {year}", use_proxy)
            seq = 1
            while True:
                file_name = f"{court_name}_{year}_{seq}.pdf"
                file_path = os.path.join(output_dir, file_name)
                json_name = f"{court_name}_{year}_{seq}.json"
                json_path = os.path.join(output_dir, json_name)

                pdf_exists = file_name in existing_files
                json_exists = json_name in existing_files

                if pdf_exists and json_exists:
                    storage_name = "GDrive" if gdrive_service else "local storage"
                    logger.info(
                        f"  [-] Skipping (PDF and JSON exist in {storage_name}): {file_name}"
                    )
                    seq += 1
                    continue

                need_pdf = not pdf_exists
                need_json = not json_exists

                if pdf_exists and need_json:
                    logger.info(f"  [~] PDF exists, backfilling metadata: {json_name}")
                elif json_exists and need_pdf:
                    logger.info(f"  [~] JSON exists, downloading PDF: {file_name}")
                else:
                    logger.info(f"  [~] Fetching PDF and metadata for sequence {seq}")

                case_url = f"{target_url.rstrip('/')}/{year}/{seq}"
                pdf_url = f"{case_url}.pdf"

                terminated_year = False
                success = False
                max_attempts = 3

                def handle_sequence_failure():
                    nonlocal terminated_year, success
                    if os.path.exists(json_path):
                        try:
                            os.remove(json_path)
                            logger.info(f"Removed local orphan metadata file: {json_name}")
                        except Exception:
                            pass
                    if gdrive_service and json_name in existing_files:
                        try:
                            delete_file_from_gdrive(gdrive_service, json_name, gdrive_folder_id)
                        except Exception:
                            pass
                    existing_files.discard(json_name)
                    terminated_year = True
                    success = True

                for attempt in range(1, max_attempts + 1):
                    logger.info(f"Processing sequence {seq} (Attempt {attempt}/{max_attempts}) for {case_url}...")
                    try:
                        scraped_metadata = False

                        if need_json:
                            if os.path.exists(json_path):
                                try:
                                    with open(json_path, "r", encoding="utf-8") as f:
                                        local_data = json.load(f)
                                    if local_data.get("title", "").strip().lower() in ("not found", "page not found", "404 not found"):
                                        logger.warning(
                                            f"Local metadata file {json_name} contains 'Not Found' title. Removing and treating as 404."
                                        )
                                        try:
                                            os.remove(json_path)
                                        except Exception:
                                            pass
                                        if pdf_exists or os.path.exists(file_path):
                                            logger.warning(
                                                f"Case page 404 for {case_url}, but PDF exists. Skipping metadata for this sequence."
                                            )
                                            success = True
                                            break
                                        logger.info(
                                            f"Treating 'Not Found' file as 404 for {case_url}. Moving to next year."
                                        )
                                        terminated_year = True
                                        success = True
                                        break
                                except Exception as e:
                                    logger.debug(f"Failed to check local json title: {e}")

                                logger.info(f"  [~] Metadata exists locally. Uploading to GDrive: {json_name}")
                                existing_files.add(json_name)
                                if gdrive_service:
                                    if upload_file_to_gdrive(
                                        gdrive_service, json_path, gdrive_folder_id
                                    ):
                                        if gdrive_delete_local:
                                            try:
                                                os.remove(json_path)
                                                logger.info(
                                                    f"  [+] Deleted local metadata file: {json_name}"
                                                )
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to delete local file {json_name}: {e}"
                                                )
                            else:
                                logger.info(f"Scraping metadata: {case_url}")
                                metadata, case_status = await scrape_case_metadata(
                                    page, case_url
                                )
                                if case_status == 404:
                                    if pdf_exists or os.path.exists(file_path):
                                        logger.warning(
                                            f"Case page 404 for {case_url}, but PDF exists. Skipping metadata for this sequence."
                                        )
                                        # Skip and continue to PDF check
                                    else:
                                        logger.info(
                                            f"Received 404 for {case_url}. Moving to next year."
                                        )
                                        handle_sequence_failure()
                                        break

                                if case_status == 403:
                                    logger.warning(f"Metadata scraping was blocked (403) for {case_url}.")
                                    raise BlockedException("Metadata scraping blocked")

                                if metadata:
                                    metadata["pdf_url"] = pdf_url
                                    with open(json_path, "w", encoding="utf-8") as f:
                                        json.dump(metadata, f, indent=2, ensure_ascii=False)
                                    logger.info(f"  [+] Saved metadata: {json_name}")
                                    existing_files.add(json_name)
                                    scraped_metadata = True

                                    if gdrive_service:
                                        if upload_file_to_gdrive(
                                            gdrive_service, json_path, gdrive_folder_id
                                        ):
                                            if gdrive_delete_local:
                                                try:
                                                    os.remove(json_path)
                                                    logger.info(
                                                        f"  [+] Deleted local metadata file: {json_name}"
                                                    )
                                                except Exception as e:
                                                    logger.warning(
                                                        f"Failed to delete local file {json_name}: {e}"
                                                    )
                                else:
                                    logger.warning(
                                        f"  [!] No metadata extracted for {case_url}"
                                    )
                                    if not need_pdf:
                                        success = True
                                        break

                        if scraped_metadata and need_pdf and not os.path.exists(file_path):
                            # Metadata scrape just ran — a Turnstile was likely solved for
                            # the HTML case page. Brief cooldown before hitting the PDF URL
                            # to avoid triggering rate limiting on SAFLII.
                            await asyncio.sleep(5)

                        if need_pdf:
                            if os.path.exists(file_path):
                                logger.info(f"  [~] PDF exists locally. Uploading to GDrive: {file_name}")
                                existing_files.add(file_name)
                                if gdrive_service:
                                    if upload_file_to_gdrive(
                                        gdrive_service, file_path, gdrive_folder_id
                                    ):
                                        if gdrive_delete_local:
                                            try:
                                                os.remove(file_path)
                                                logger.info(
                                                    f"  [+] Deleted local PDF file: {file_name}"
                                                )
                                            except Exception as e:
                                                logger.warning(
                                                    f"Failed to delete local file {file_name}: {e}"
                                                )
                            else:
                                logger.info(f"Checking PDF URL: {pdf_url}")
                                (
                                    pdf_bytes,
                                    pdf_result_status,
                                    pdf_content_type,
                                ) = await download_pdf(
                                    page, pdf_url, pdf_data, pdf_status, current_pdf_url
                                )

                                if pdf_bytes:
                                    with open(file_path, "wb") as f:
                                        f.write(pdf_bytes)
                                    logger.info(f"  [+] Downloaded: {file_name}")
                                    existing_files.add(file_name)

                                    if gdrive_service:
                                        if upload_file_to_gdrive(
                                            gdrive_service, file_path, gdrive_folder_id
                                        ):
                                            if gdrive_delete_local:
                                                try:
                                                    os.remove(file_path)
                                                    logger.info(
                                                        f"  [+] Deleted local PDF file: {file_name}"
                                                    )
                                                except Exception as e:
                                                    logger.warning(
                                                        f"Failed to delete local file {file_name}: {e}"
                                                    )
                                else:
                                    if pdf_result_status == 404:
                                        # If we did not scrape metadata in this iteration, double-check the HTML page to confirm it is a 404
                                        is_real_404 = False
                                        if not scraped_metadata:
                                            logger.info(f"PDF returned 404. Double-checking HTML case page: {case_url}")
                                            temp_metadata, temp_status = await scrape_case_metadata(page, case_url)
                                            if temp_status == 404:
                                                is_real_404 = True
                                        else:
                                            is_real_404 = True

                                        if is_real_404:
                                            logger.warning(
                                                f"URL {pdf_url} returned 404 and case page is confirmed 404/Not Found. Assuming end of sequence for the year."
                                            )
                                            handle_sequence_failure()
                                            break
                                        else:
                                            logger.warning(
                                                f"PDF for {pdf_url} returned 404, but case HTML page is valid. Skipping download."
                                            )
                                            success = True
                                            break

                                    if pdf_result_status == 403:
                                        logger.warning(f"PDF download was blocked (403) for {pdf_url}.")
                                        raise BlockedException("PDF download blocked")

                                    if pdf_content_type and "html" in pdf_content_type:
                                        logger.warning(f"URL {pdf_url} returned HTML block page (status: {pdf_result_status}).")
                                        raise BlockedException("HTML response received instead of PDF")

                                    logger.error(
                                        f"  [!] Failed to download PDF for {pdf_url} (status: {pdf_result_status})"
                                    )
                                    raise BlockedException(f"Failed to download PDF (status: {pdf_result_status})")

                        # If we reached here without raising or breaking, the sequence is successfully processed!
                        success = True
                        break

                    except BlockedException as be:
                        logger.warning(f"Blocked or error on attempt {attempt}: {be}")
                        if attempt < max_attempts:
                            backoff_seconds = attempt * 15
                            logger.info(f"Sleeping for {backoff_seconds} seconds before attempt {attempt + 1}...")
                            await asyncio.sleep(backoff_seconds)
                        else:
                            logger.warning(f"Failed all {max_attempts} attempts for sequence {seq}. Assuming end of sequence for the year.")
                            handle_sequence_failure()
                            break
                    except Exception as e:
                        err_str = str(e)
                        if "ERR_ABORTED" in err_str or "net::" in err_str:
                            logger.warning(f"Transient network error for {case_url}: {e}.")
                            if attempt < max_attempts:
                                backoff_seconds = attempt * 15
                                logger.info(f"Sleeping for {backoff_seconds} seconds before attempt {attempt + 1}...")
                                await asyncio.sleep(backoff_seconds)
                            else:
                                logger.warning(f"Failed all {max_attempts} attempts (network error) for sequence {seq}. Assuming end of sequence for the year.")
                                handle_sequence_failure()
                                break
                        else:
                            logger.error(f"Unrecoverable error for {case_url}: {e}")
                            sys.exit(1)

                if terminated_year:
                    break

                if success:
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
