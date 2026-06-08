from typing import Dict, Type
from playwright.async_api import Page
from .base import BaseSolver
from .hcaptcha import HCaptchaSolver
from .turnstile import TurnstileSolver


class CaptchaSolverFactory:
    _solvers: Dict[str, Type[BaseSolver]] = {}

    @classmethod
    def register(cls, name: str, solver_class: Type[BaseSolver]) -> None:
        cls._solvers[name.lower()] = solver_class

    @classmethod
    def get_solver(cls, name: str, **kwargs) -> BaseSolver:
        solver_class = cls._solvers.get(name.lower())
        if not solver_class:
            raise ValueError(
                f"Unknown solver type: {name}. Registered solvers: {list(cls._solvers.keys())}"
            )
        return solver_class(**kwargs)


# Register default solvers
CaptchaSolverFactory.register("hcaptcha", HCaptchaSolver)
CaptchaSolverFactory.register("turnstile", TurnstileSolver)


async def solve_captcha(solver_type: str, page: Page, **kwargs) -> dict:
    """Helper function to quickly solve a captcha using a registered solver."""
    solver = CaptchaSolverFactory.get_solver(solver_type, **kwargs)
    return await solver.solve(page, **kwargs)
