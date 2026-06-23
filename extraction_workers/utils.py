import json
import logging
import os
import sys

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

API_URL = os.getenv("COEUS_API_URL") or "http://coeus-control-plane:8001/api/pipelines/active/"


async def fetch_pipeline_config(pipeline_name_or_id: str) -> dict:
    """
    Fetches pipeline configuration, prioritizing environment variables
    provided by the dynamic_factory, falling back to the Control Plane API.
    """
    # 1. Try to load from environment variables (provided by dynamic_factory)
    env_config = {
        "start_url": os.getenv("START_URL"),
        "document_type": os.getenv("DOCUMENT_TYPE"),
        "target_css_selector_categories": os.getenv("CAT_SELECTOR"),
        "target_css_selector_documents": os.getenv("DOC_SELECTOR"),
        "allow_insecure_https": os.getenv("ALLOW_INSECURE_HTTPS", "False").lower()
        == "true",
        "allow_insecure_requests": os.getenv("ALLOW_INSECURE_REQUESTS", "False").lower()
        == "true",
    }

    # Extract extraction_params from env if it exists
    raw_params = os.getenv("EXTRACTION_PARAMS")
    if raw_params:
        try:
            env_config["extraction_params"] = json.loads(raw_params)
        except json.JSONDecodeError:
            logger.warning(
                "Failed to parse EXTRACTION_PARAMS env var as JSON. Using empty dict."
            )
            env_config["extraction_params"] = {}

    # If we have the essential bits from env, use them
    if env_config["start_url"] and env_config["document_type"]:
        logger.info(
            f"Using environment-provided configuration for pipeline: {pipeline_name_or_id}"
        )
        if env_config.get("allow_insecure_requests"):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
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

        # Standardize structure
        standard_config = {
            **config["metadata"],
            **config["phase_1_ingestion"],
            **config["phase_2_extraction"],
            "pipeline_id": config["pipeline_id"],
            "name": config.get("name"),
        }

        if standard_config.get("allow_insecure_requests"):
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
