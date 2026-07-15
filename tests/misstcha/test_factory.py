import pytest
from unittest.mock import AsyncMock, MagicMock

from misstcha.base import BaseSolver
from misstcha.factory import CaptchaSolverFactory, solve_captcha
from misstcha.hcaptcha import HCaptchaSolver
from misstcha.turnstile import TurnstileSolver


@pytest.fixture(autouse=True)
def mock_ml_deps(mocker):
    # Patch VisionManager and PromptTranslator inside hcaptcha
    # to prevent trying to download Grounding DINO/Qwen models during test runs.
    mocker.patch("misstcha.hcaptcha.VisionManager")
    mocker.patch("misstcha.hcaptcha.PromptTranslator")


class DummySolver(BaseSolver):
    def __init__(self, some_arg=None, **kwargs):
        self.some_arg = some_arg

    async def solve(self, page, **kwargs):
        return {"success": True, "some_arg": self.some_arg}


def test_factory_registry():
    # Verify default registration
    assert isinstance(CaptchaSolverFactory.get_solver("turnstile"), TurnstileSolver)
    assert isinstance(CaptchaSolverFactory.get_solver("hcaptcha"), HCaptchaSolver)

    # Register custom solver
    CaptchaSolverFactory.register("dummy", DummySolver)
    solver = CaptchaSolverFactory.get_solver("dummy", some_arg="hello")
    assert isinstance(solver, DummySolver)
    assert solver.some_arg == "hello"

    # Get unknown solver should raise ValueError
    with pytest.raises(ValueError) as excinfo:
        CaptchaSolverFactory.get_solver("unknown_solver")
    assert "Unknown solver type: unknown_solver" in str(excinfo.value)


@pytest.mark.asyncio
async def test_solve_captcha_helper():
    mock_page = MagicMock()
    
    # Temporarily register dummy solver for solve test
    CaptchaSolverFactory.register("dummy", DummySolver)
    
    result = await solve_captcha("dummy", mock_page, some_arg="test_value")
    
    assert result["success"] is True
    assert result["some_arg"] == "test_value"
