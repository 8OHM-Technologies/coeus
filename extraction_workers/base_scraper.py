import os
import asyncio
import urllib.parse
from abc import ABC, abstractmethod
from typing import Dict, Any, Set, Optional, FrozenSet

import requests
import db_storage
from utils.utils import fetch_pipeline_config, resolve_data_dir
from db import get_db_connection
from utils.browser_helper import setup_logger

logger = setup_logger(__name__)


class BaseScraper(ABC):
    """
    Abstract Base Class providing infrastructure for all scrapers.
    Handles configuration, environment, state tracking, database interactions,
    and exposes standard lifecycle stages compatible with SeleniumBase UC Mode.
    """

    # -------------------------------------------------------------------------
    # Pipeline Stage Numbers
    # -------------------------------------------------------------------------
    #  Stage 1 — Indexing   (harvests/indexes record URLs into the database)
    #  Stage 2 — Detailing  (enriches indexed records with detailed metadata)
    #  Stage 3 — Extraction (post-processing / downstream extraction step)
    # -------------------------------------------------------------------------
    STAGE_INDEXING: int = 1
    STAGE_DETAILING: int = 2
    STAGE_EXTRACTION: int = 3

    def __init__(self, pipeline_name: str, skip_stages: Optional[str] = None):
        self.pipeline_name: str = pipeline_name
        self.env: Dict[str, str] = dict(os.environ)
        self.config: Dict[str, Any] = {}
        self.output_dir: str = ""

        # Pipeline Stage Control
        # Accepts a comma-separated string of stage numbers to skip, e.g. "1" or "1,2".
        # Parsed into a frozenset of ints for fast O(1) membership checks at runtime.
        self.skip_stages: FrozenSet[int] = self._parse_skip_stages(skip_stages)

        # Database & Deduplication State
        self.conn = None
        self.target_id: Any = None
        self.existing_urls: Set[str] = set()
        self.existing_case_numbers: Set[str] = set()
        self.progress_state: Dict[str, Any] = {}
        self.db_lock = asyncio.Lock()

        # Proxy Configuration
        self.use_proxy: bool = False
        self.proxy_url: Optional[str] = None

    @staticmethod
    def _parse_skip_stages(skip_stages: Optional[str]) -> FrozenSet[int]:
        """Parse a comma-separated string of stage numbers into a frozenset of ints.

        Examples::

            _parse_skip_stages("1")    -> frozenset({1})
            _parse_skip_stages("1,2")  -> frozenset({1, 2})
            _parse_skip_stages(None)   -> frozenset()
        """
        if not skip_stages:
            return frozenset()
        result: Set[int] = set()
        for token in skip_stages.split(","):
            token = token.strip()
            if token.isdigit():
                result.add(int(token))
            elif token:
                logger.warning(f"Invalid skip_stages token ignored: '{token}'")
        return frozenset(result)

    async def initialize(self) -> None:
        """Initializes configs, resolves output paths, and sets up database baselines."""
        self.config = await fetch_pipeline_config(self.pipeline_name)
        doc_type = self.config.get("document_type", "awards")
        self.output_dir = resolve_data_dir(self.pipeline_name, doc_type)
        
        # Establish DB Connection
        self.conn = await get_db_connection()
        
        start_url = self.config.get("start_url")
        entity_name = self.config.get("name") or self.pipeline_name
        target_name = self.config.get("subset") or "Default Target"
        db_record_type = (
            self.config.get("extraction_params", {}).get("shared_record_type")
            or self.pipeline_name
        )

        logger.info(f"Resolving database targets for entity='{entity_name}'...")
        self.target_id = await db_storage.resolve_target_id(
            self.conn, entity_name, target_name, start_url
        )
        
        # Hydrate deduplication sets
        urls = await db_storage.get_existing_urls(self.conn, db_record_type)
        self.existing_urls = set(urls)
        
        cases = await db_storage.get_existing_case_numbers(self.conn, db_record_type)
        self.existing_case_numbers = set(cases)
        
        self.progress_state = await db_storage.load_pipeline_state(
            self.conn, self.pipeline_name
        )
        logger.info(f"Progress state loaded: {self.progress_state}")

        # Hydrate proxy flags from pipeline config & environment
        self.use_proxy = bool(self.config.get("use_proxy", False))
        self.proxy_url = self.config.get("proxy_url") or os.getenv("PROXY_URL")

        if self.use_proxy:
            if not self.proxy_url:
                logger.error(
                    f"❌ [PROXY CONFIG] Pipeline '{self.pipeline_name}' requires proxy (use_proxy=True), "
                    f"but NO PROXY_URL is configured or available in environment! Traffic WILL NOT be proxied."
                )
            else:
                try:
                    p_str = self.proxy_url if "://" in self.proxy_url else f"http://{self.proxy_url}"
                    parsed = urllib.parse.urlparse(p_str)
                    safe_net = f"{parsed.username}:****@{parsed.hostname}:{parsed.port}" if parsed.username else parsed.netloc
                    logger.info(f"🌐 [PROXY CONFIG] Pipeline '{self.pipeline_name}' Proxy ENABLED -> {safe_net}")
                except Exception:
                    logger.info(f"🌐 [PROXY CONFIG] Pipeline '{self.pipeline_name}' Proxy ENABLED")
        else:
            logger.info(f"ℹ️ [PROXY CONFIG] Pipeline '{self.pipeline_name}' Proxy DISABLED (Direct Connection)")

    def log_outbound_ip(self, sb: Any = None, label: str = "Scraper") -> Optional[str]:
        """Check and log the current outbound IP address via SeleniumBase driver or HTTP request."""
        if not self.use_proxy:
            logger.info(f"ℹ️ [{label}] Direct Connection (Proxy Disabled)")
            return None

        if sb:
            try:
                sb.open("https://ipv4.webshare.io/")
                ip = (sb.get_text("body") or "").strip()
                logger.info(f"🌐 [{label}] Verified Outbound Public IP (via Proxy Browser): {ip}")
                return ip
            except Exception as e:
                logger.warning(f"⚠️ [{label}] Failed to verify outbound IP via browser: {e}")
                return None
        else:
            try:
                formatted_proxy = self.proxy_url
                if formatted_proxy and "://" not in formatted_proxy:
                    formatted_proxy = f"http://{formatted_proxy}"
                proxies = {"http": formatted_proxy, "https": formatted_proxy} if formatted_proxy else None
                resp = requests.get("https://ipv4.webshare.io/", proxies=proxies, timeout=10)
                ip = resp.text.strip()
                logger.info(f"🌐 [{label}] Verified Outbound Public IP (via Proxy HTTP): {ip}")
                return ip
            except Exception as e:
                logger.warning(f"⚠️ [{label}] Failed to verify outbound IP via HTTP request: {e}")
                return None

    async def save_progress(self, year: int, month: int = 0, completed: bool = False, **kwargs) -> None:
        """Persists rolling-window execution markers into database storage."""
        self.progress_state["last_year"] = year
        self.progress_state["last_month"] = month
        self.progress_state["last_completed"] = completed
        for k, v in kwargs.items():
            self.progress_state[k] = v
        async with self.db_lock:
            await db_storage.save_pipeline_state(
                self.conn, self.pipeline_name, self.progress_state
            )

    async def cleanup(self) -> None:
        """Ensures database connections terminate cleanly on shutdown."""
        if self.conn:
            await self.conn.close()
            logger.info("Database connection gracefully terminated.")

    async def scraping(self) -> None:
        """
        Executes Stage 1 of the pipeline: Scraping (Indexing and Detailing).

        Individual sub-stages can be bypassed via ``skip_stages``:
          - Stage 1 (indexing)  is skipped when ``1`` is in ``self.skip_stages``.
          - Stage 2 (detailing) is skipped when ``2`` is in ``self.skip_stages``.
        """
        logger.info("Starting Stage 1: Scraping Process Pipeline...")
        if self.STAGE_INDEXING in self.skip_stages:
            logger.info("⏭  Stage 1 (Indexing) skipped via skip_stages configuration.")
        else:
            await self.indexing()

        if self.STAGE_DETAILING in self.skip_stages:
            logger.info("⏭  Stage 2 (Detailing) skipped via skip_stages configuration.")
        else:
            await self.detailing()

    @abstractmethod
    async def authenticate(self, headless: bool = False) -> None:
        """Handle structural authentication or session cookie persistence logic."""
        pass

    @abstractmethod
    async def indexing(self) -> None:
        """
        Executes Stage 1A of the pipeline: Indexing
        """
        logger.info("Starting Stage 1A: Indexing Process Pipeline...")
        pass

    @abstractmethod
    async def detailing(self) -> None:
        """
        Executes Stage 1B of the pipeline: Detailing
        """
        logger.info("Starting Stage 1B: Detailing Process Pipeline...")
        pass

    @abstractmethod
    async def extraction(self) -> None:
        """
        Executes Stage 2 of the pipeline: Extraction
        """
        logger.info("Starting Stage 2: Extraction Process Pipeline...")
        pass

    async def run(self) -> None:
        """Main execution wrapper coordinating overall lifecycle stages.

        Stages skipped via ``self.skip_stages``:
          - Stage 1 (indexing)  — skip_stages includes ``1``
          - Stage 2 (detailing) — skip_stages includes ``2``
          - Stage 3 (extraction)— skip_stages includes ``3``

        Skipping *all* scraping sub-stages (1 and 2) causes ``scraping()`` to
        be called but both inner steps are no-ops, which is functionally
        equivalent to going straight to extraction.
        """
        if self.skip_stages:
            logger.info(f"🔧 Pipeline skip_stages active: {sorted(self.skip_stages)}")
        try:
            await self.initialize()

            await self.scraping()

            if self.STAGE_EXTRACTION in self.skip_stages:
                logger.info("⏭  Stage 3 (Extraction) skipped via skip_stages configuration.")
            else:
                await self.extraction()

        except Exception as err:
            logger.exception(f"Fatal exception occurred: {err}")
            raise err
        finally:
            await self.cleanup()