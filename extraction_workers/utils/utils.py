import json
import logging
import os
import sys
from typing import Any, Optional, Dict
import requests
import urllib3

def load_dotenv():
    # Load .env variables into os.environ for local script runs
    for path in [".env", "../.env", os.path.join(os.path.dirname(__file__), "../.env")]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass
            break

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
}

API_URL = os.getenv("COEUS_API_URL")
if API_URL:
    API_URL = API_URL.strip("'\"")
else:
    API_URL = "http://coeus-control-plane:8001/api/pipelines/active/"


def to_bool(val: Any) -> bool:
    """Parse boolean values from bool, str, int, or float."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on", "t")
    return False


async def fetch_pipeline_config(pipeline_name_or_id: str) -> dict:
    """
    Fetches pipeline configuration, prioritizing environment variables
    provided by the dynamic_factory, falling back to the Control Plane API.
    """
    proxy_url = os.getenv("PROXY_URL")

    # Extract extraction_params from env if it exists
    raw_params = os.getenv("EXTRACTION_PARAMS")
    extraction_params = {}
    if raw_params:
        try:
            if isinstance(raw_params, str):
                extraction_params = json.loads(raw_params)
            elif isinstance(raw_params, dict):
                extraction_params = raw_params
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse EXTRACTION_PARAMS env var as JSON. Using empty dict."
            )

    use_proxy_from_env = (
        to_bool(extraction_params.get("use_proxy"))
        or to_bool(os.getenv("USE_PROXY"))
    )

    # 1. Try to load from environment variables (provided by dynamic_factory)
    env_config = {
        "start_url": os.getenv("START_URL"),
        "document_type": os.getenv("DOCUMENT_TYPE"),
        "allow_insecure_https": to_bool(os.getenv("ALLOW_INSECURE_HTTPS", "False")),
        "allow_insecure_requests": to_bool(os.getenv("ALLOW_INSECURE_REQUESTS", "False")),
        "use_proxy": use_proxy_from_env,
        "proxy_url": proxy_url,
        "name": os.getenv("PIPELINE_NAME", pipeline_name_or_id),
        "subset": os.getenv("SUBSET", ""),
        "extraction_params": extraction_params,
    }

    # If we have the essential bits from env, use them
    if env_config["start_url"] and env_config["document_type"]:
        logger.info(
            f"Using environment-provided configuration for pipeline: {pipeline_name_or_id}"
        )
        if env_config.get("allow_insecure_requests"):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if env_config.get("use_proxy") and not env_config.get("proxy_url"):
            logger.warning(
                f"⚠️ [CONFIG WARNING] Pipeline '{pipeline_name_or_id}' has use_proxy=True, "
                f"but PROXY_URL environment variable is missing or empty!"
            )
        return env_config

    # 2. Fallback to API if env vars are missing
    try:
        logger.info(
            f"Fetching configuration from API for pipeline: {pipeline_name_or_id}"
        )
        response = requests.get(API_URL, timeout=10)
        response.raise_for_status()
        pipelines = response.json().get("pipelines", [])

        config = next(
            (
                p
                for p in pipelines
                if p["pipeline_id"] == pipeline_name_or_id
                or p.get("name") == pipeline_name_or_id
            ),
            None,
        )

        if not config:
            logger.error(
                f"Configuration for pipeline '{pipeline_name_or_id}' not found in API response."
            )
            sys.exit(1)

        api_extraction_params = (
            config.get("phase_2_extraction", {}).get("extraction_params", {})
            or config.get("extraction_params", {})
            or {}
        )
        api_use_proxy = (
            to_bool(api_extraction_params.get("use_proxy"))
            or to_bool(config.get("phase_1_ingestion", {}).get("use_proxy"))
            or to_bool(config.get("use_proxy"))
            or use_proxy_from_env
        )

        # Standardize structure
        standard_config = {
            **config["metadata"],
            **config["phase_1_ingestion"],
            **config["phase_2_extraction"],
            "pipeline_id": config["pipeline_id"],
            "name": config.get("name"),
            "subset": config.get("subset", ""),
            "use_proxy": api_use_proxy,
            "proxy_url": proxy_url,
            "extraction_params": api_extraction_params,
        }

        if standard_config.get("allow_insecure_requests"):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        if standard_config.get("use_proxy") and not standard_config.get("proxy_url"):
            logger.warning(
                f"⚠️ [CONFIG WARNING] Pipeline '{pipeline_name_or_id}' has use_proxy=True, "
                f"but PROXY_URL environment variable is missing or empty!"
            )

        return standard_config

    except Exception as e:
        logger.error(f"Failed to fetch configuration from {API_URL} or env: {e}")
        sys.exit(1)


def download_pdf(
    url: str, save_dir: str, file_name: str, allow_insecure_requests: bool = False
):
    """Downloads a PDF using requests, following the CCMA scraper pattern."""
    try:
        # Sanitize filename: keep only alphanumeric, spaces, and dots
        safe_name = "".join(
            [c for c in file_name if c.isalpha() or c.isdigit() or c in (" ", ".")]
        ).rstrip()
        safe_name = safe_name.replace(" ", "_")
        if not safe_name.lower().endswith(".pdf"):
            safe_name += ".pdf"

        file_path = os.path.join(save_dir, safe_name)

        # Efficient check if already downloaded
        if os.path.exists(file_path):
            logger.info(f"  [-] Skipping (already exists): {safe_name}")
            return

        response = requests.get(
            url,
            headers=HEADERS,
            stream=True,
            timeout=30,
            verify=not allow_insecure_requests,
        )
        response.raise_for_status()

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"  [+] Downloaded: {safe_name}")
    except Exception as e:
        logger.error(f"  [!] Failed to download {file_name}: {e}")


async def take_screenshot(page, screenshot_dir: str, name: str):
    if not screenshot_dir:
        return
    try:
        os.makedirs(screenshot_dir, exist_ok=True)
        filename = f"{name}.png"
        path = os.path.join(screenshot_dir, filename)
        await page.screenshot(path=path)
        logger.info(f"Saved screenshot: {path}")
    except Exception as e:
        logger.warning(f"Failed to take screenshot {name}: {e}")


def resolve_data_dir(pipeline_name: str, document_type: str = "awards") -> str:
    """Resolve the output data directory, handling container vs local layouts.

    Returns the absolute path to the output directory, creating it if needed.
    Uses ``/app/data`` when running inside a container, otherwise ``data/``.
    """
    if os.path.exists("/app/data"):
        base = "/app/data"
    elif os.path.exists("/app"):
        base = "/app/data"
    else:
        base = "data"

    output_dir = os.path.join(base, pipeline_name, document_type.lower())
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def save_json(path: str, data, indent: int = 4) -> None:
    """Atomically write *data* as JSON to *path*."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
