"""
misstcha — automated captcha solver.

Supported captcha types (built-in):
  - hCaptcha  (``HCaptchaSolver``)   — image-grid challenge via Grounding DINO + LLM
  - Cloudflare Turnstile (``TurnstileSolver``) — checkbox challenge via human-like mouse

Extend by subclassing ``BaseSolver`` and registering with ``CaptchaSolverFactory``.
"""

__version__ = "0.1.0"

from .base import BaseSolver
from .hcaptcha import HCaptchaSolver
from .turnstile import TurnstileSolver
from .factory import CaptchaSolverFactory, solve_captcha

__all__ = [
    "__version__",
    "BaseSolver",
    "HCaptchaSolver",
    "TurnstileSolver",
    "CaptchaSolverFactory",
    "solve_captcha",
]
