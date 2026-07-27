import os
import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Any, Set, Optional

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

    def __init__(self, pipeline_name: str):
        self.pipeline_name: str = pipeline_name
        self.env: Dict[str, str] = dict(os.environ)
        self.config: Dict[str, Any] = {}
        self.output_dir: str = ""
        
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

        # Hydrate proxy flags from pipeline config
        self.use_proxy = self.config.get("use_proxy", False)
        self.proxy_url = self.config.get("proxy_url")

    async def save_progress(self, year: int, month: int = 0, completed: bool = False, **kwargs) -> None:
        """Persists rolling-window execution markers into database storage."""
        self.progress_state["last_year"] = year
        self.progress_state["last_month"] = month
        self.progress_state["last_completed"] = completed
        for k, v in kwargs.items():
            self.progress_state[k] = v
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
        Executes Stage 1 of the pipeline: Scraping (Indexing and Detailing)
        """
        logger.info("Starting Stage 1: Scraping Process Pipeline...")
        await self.indexing()
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
        """Main execution wrapper coordinating overall lifecycle stages."""
        try:
            await self.initialize()
            
            await self.scraping()
            await self.extraction()
            
        except Exception as err:
            logger.exception(f"Fatal exception occurred: {err}")
            raise err
        finally:
            await self.cleanup()