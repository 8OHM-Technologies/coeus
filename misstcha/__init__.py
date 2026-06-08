from .base import BaseSolver
from .hcaptcha import HCaptchaSolver
from .turnstile import TurnstileSolver
from .factory import CaptchaSolverFactory, solve_captcha

__all__ = [
    "BaseSolver",
    "HCaptchaSolver",
    "TurnstileSolver",
    "CaptchaSolverFactory",
    "solve_captcha",
]
