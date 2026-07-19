import os
import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Any, Set
from playwright.async_api import async_playwright, Page

import db_storage
from utils.utils import fetch_pipeline_config, resolve_data_dir
from db import get_db_connection
from utils.browser_helper import setup_logger, BrowserManager

logger = setup_logger(__name__)

class BaseScraper(ABC):
    """
    Abstract Base Class providing common infrastructure for all scrapers.
    Handles configuration, environment, state tracking, database interaction,
    and exposes the standard lifecycle stages.
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
        
        # Browser Management
        self.playwright_instance = None
        self.browser_manager: BrowserManager = None

    async def initialize(self) -> None:
        """Initializes configs, resolves paths, and sets up database baselines."""
        self.config = await fetch_pipeline_config(self.pipeline_name)
        doc_type = self.config.get("document_type", "awards")
        self.output_dir = resolve_data_dir(self.pipeline_name, doc_type)
        
        # Establish DB Connections & Load context vectors
        self.conn = await get_db_connection()
        
        start_url = self.config.get("start_url")
        entity_name = self.config.get("name") or self.pipeline_name
        target_name = self.config.get("subset") or "Default Target"
        db_record_type = self.config.get("extraction_params", {}).get("shared_record_type") or self.pipeline_name

        logger.info(f"Resolving database targets for entity='{entity_name}'...")
        self.target_id = await db_storage.resolve_target_id(
            self.conn, entity_name, target_name, start_url
        )
        
        # Hydrate deduplication arrays
        urls = await db_storage.get_existing_urls(self.conn, db_record_type)
        self.existing_urls = set(urls)
        
        cases = await db_storage.get_existing_case_numbers(self.conn, db_record_type)
        self.existing_case_numbers = set(cases)
        
        self.progress_state = await db_storage.load_pipeline_state(self.conn, self.pipeline_name)
        logger.info(f"Progress state loaded for execution footprint: {self.progress_state}")

    async def save_progress(self, year: int, month: int, completed: bool = False) -> None:
        """Persists rolling-window pagination execution markers into storage."""
        self.progress_state["last_year"] = year
        self.progress_state["last_month"] = month
        self.progress_state["last_completed"] = completed
        await db_storage.save_pipeline_state(self.conn, self.pipeline_name, self.progress_state)

    async def cleanup(self) -> None:
        """Ensures connections and browser resources break down cleanly."""
        if self.browser_manager:
            await self.browser_manager.recycle()
        if self.playwright_instance:
            await self.playwright_instance.stop()
        if self.conn:
            await self.conn.close()
            logger.info("Database connection gracefully terminated.")

    @abstractmethod
    async def authenticate(self, headless: bool = False) -> None:
        """Handle structural authentication state logic generation."""
        pass

    @abstractmethod
    async def indexing(self) -> None:
        """Sub-process A of Scraping: Build index / discover list components."""
        pass

    @abstractmethod
    async def detailing(self) -> None:
        """Sub-process B of Scraping: Perform payload enrichment on specific entities."""
        pass

    @abstractmethod
    async def extraction(self) -> None:
        """Stage 2: Process, parse, structure, or validate the raw acquired records."""
        pass

    async def scraping(self) -> None:
        """Stage 1: Executes indexing discovery followed by item detailing."""
        logger.info("Starting Stage 1: Scraping Process Pipeline...")
        await self.indexing()
        await self.detailing()

    async def run(self) -> None:
        """The main execution wrapper coordinating orchestration layers."""
        try:
            await self.initialize()
            
            # Executing Orchestrated Engine Lifecycle Stages
            await self.scraping()
            await self.extraction()
            
        except Exception as err:
            logger.exception(f"Fatal disruption occurred during framework lifecycle run: {err}")
            raise err
        finally:
            await self.cleanup()