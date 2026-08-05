"""
BookStack API Client module.

Provides a clean interface to interact with BookStack REST API for managing
Shelves, Books, and Pages.
"""

import logging
import os
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)


class BookStackClientError(Exception):
    """Custom exception raised when BookStack API returns an error."""
    pass


class BookStackClient:
    """
    REST API Client for BookStack.
    Handles shelf, book, and page creation, deletion, and indexing.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        token_id: Optional[str] = None,
        token_secret: Optional[str] = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = (base_url or os.getenv("BOOKSTACK_URL", "http://ohmbase:80")).rstrip("/")
        self.token_id = token_id or os.getenv("BOOKSTACK_TOKEN_ID", "")
        self.token_secret = token_secret or os.getenv("BOOKSTACK_TOKEN_SECRET", "")
        self.timeout = timeout

        # Ensure API endpoint path
        if not self.base_url.endswith("/api"):
            self.api_url = f"{self.base_url}/api"
        else:
            self.api_url = self.base_url

        self.session = requests.Session()
        if self.token_id and self.token_secret:
            self.session.headers.update({
                "Authorization": f"Token {self.token_id}:{self.token_secret}",
                "Accept": "application/json",
            })
        else:
            self.session.headers.update({
                "Accept": "application/json",
            })

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict:
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        try:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            if response.status_code == 24:  # 204 No Content for DELETE
                return {}
            if not response.content:
                return {}
            return response.json()
        except requests.exceptions.RequestException as exc:
            err_msg = f"BookStack API error on {method} {url}: {exc}"
            if hasattr(exc, "response") and exc.response is not None:
                try:
                    err_json = exc.response.json()
                    err_msg += f" | Details: {err_json}"
                except Exception:
                    err_msg += f" | Body: {exc.response.text}"
            logger.error(err_msg)
            raise BookStackClientError(err_msg) from exc

    def get_or_create_shelf(self, name: str, description: str = "") -> dict:
        """
        Finds a shelf by exact name or creates a new one.
        Returns the shelf dict containing 'id', 'name', 'books', etc.
        """
        res = self._request("GET", "shelves", params={"filter[name]": name})
        data = res.get("data", [])
        for shelf in data:
            if shelf.get("name") == name:
                return self._request("GET", f"shelves/{shelf['id']}")

        # Create shelf if not found
        payload = {
            "name": name,
            "description": description or f"Shelf for {name}",
        }
        logger.info(f"Creating new BookStack Shelf: '{name}'")
        return self._request("POST", "shelves", json=payload)

    def get_or_create_book(self, court_name: str, shelf_id: int) -> dict:
        """
        Finds a book by court_name or creates a new one, ensuring it is attached to shelf_id.
        Returns the book dict containing 'id', 'name', etc.
        """
        res = self._request("GET", "books", params={"filter[name]": court_name})
        data = res.get("data", [])
        book_id: Optional[int] = None
        book_obj: Optional[dict] = None

        for book in data:
            if book.get("name") == court_name:
                book_id = book["id"]
                book_obj = book
                break

        if not book_id:
            payload = {
                "name": court_name,
                "description": f"Court judgments and records for {court_name}",
            }
            logger.info(f"Creating new BookStack Book for Court: '{court_name}'")
            book_obj = self._request("POST", "books", json=payload)
            book_id = book_obj["id"]

        # Ensure book is linked to shelf
        if shelf_id and book_id:
            self.attach_book_to_shelf(shelf_id, book_id)

        return book_obj  # type: ignore

    def attach_book_to_shelf(self, shelf_id: int, book_id: int) -> dict:
        """
        Ensures a book ID is included in a shelf's books list.
        """
        shelf = self._request("GET", f"shelves/{shelf_id}")
        existing_books = shelf.get("books", [])
        existing_book_ids = [b["id"] for b in existing_books if isinstance(b, dict) and "id" in b]

        if book_id not in existing_book_ids:
            updated_book_ids = existing_book_ids + [book_id]
            logger.info(f"Attaching Book #{book_id} to Shelf #{shelf_id}")
            return self._request("PUT", f"shelves/{shelf_id}", json={"books": updated_book_ids})
        return shelf

    def create_page(
        self,
        book_id: int,
        name: str,
        html: Optional[str] = None,
        markdown: Optional[str] = None,
        tags: Optional[List[Dict[str, str]]] = None,
        chapter_id: Optional[int] = None,
    ) -> dict:
        """
        Creates a new page within a book (or chapter), using HTML or Markdown.
        """
        payload: Dict[str, Any] = {
            "book_id": book_id,
            "name": name[:250],  # Ensure title length boundary
        }
        if html:
            payload["html"] = html
        elif markdown:
            payload["markdown"] = markdown
        else:
            raise ValueError("Either 'html' or 'markdown' content must be provided.")

        if chapter_id:
            payload["chapter_id"] = chapter_id
        if tags:
            payload["tags"] = tags

        logger.info(f"Creating BookStack Page '{name}' in Book #{book_id}")
        return self._request("POST", "pages", json=payload)

    def delete_shelf(self, shelf_id: int) -> None:
        """Deletes a shelf by ID."""
        logger.info(f"Deleting BookStack Shelf #{shelf_id}")
        self._request("DELETE", f"shelves/{shelf_id}")

    def delete_book(self, book_id: int) -> None:
        """Deletes a book by ID."""
        logger.info(f"Deleting BookStack Book #{book_id}")
        self._request("DELETE", f"books/{book_id}")

    def delete_page(self, page_id: int) -> None:
        """Deletes a page by ID."""
        logger.info(f"Deleting BookStack Page #{page_id}")
        self._request("DELETE", f"pages/{page_id}")

    def clear_all_shelves_and_books(self) -> None:
        """
        Deletes all existing shelves and books in BookStack via REST API.
        """
        logger.info("Clearing all shelves and books from BookStack...")
        
        # Delete shelves
        try:
            shelves_res = self._request("GET", "shelves", params={"count": 500})
            shelves = shelves_res.get("data", [])
            for shelf in shelves:
                try:
                    self.delete_shelf(shelf["id"])
                except Exception as exc:
                    logger.warning(f"Failed to delete shelf #{shelf['id']}: {exc}")
        except Exception as exc:
            logger.warning(f"Failed to list shelves for deletion: {exc}")

        # Delete books
        try:
            books_res = self._request("GET", "books", params={"count": 500})
            books = books_res.get("data", [])
            for book in books:
                try:
                    self.delete_book(book["id"])
                except Exception as exc:
                    logger.warning(f"Failed to delete book #{book['id']}: {exc}")
        except Exception as exc:
            logger.warning(f"Failed to list books for deletion: {exc}")
