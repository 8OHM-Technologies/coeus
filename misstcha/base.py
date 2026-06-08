from abc import ABC, abstractmethod
from typing import Any, Dict
from playwright.async_api import Page

class BaseSolver(ABC):
    """Base class for captcha solvers. Extensible for hCaptcha, Turnstile, reCAPTCHA, etc."""
    
    @abstractmethod
    async def solve(self, page: Page, *args, **kwargs) -> Dict[str, Any]:
        """Solve the captcha on the given Playwright page.
        
        Returns:
            Dict[str, Any]: A dictionary containing:
                - "success" (bool): True if solved successfully, False otherwise.
                - "attempts" (int): The number of attempts taken (if applicable).
                - "error" (str or None): An error message if success is False.
        """
        pass
