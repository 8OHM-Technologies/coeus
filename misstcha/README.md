# misstcha
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE_misstcha)

**misstcha** is an automated captcha solver built on [Playwright](https://playwright.dev/python/) and vision AI.  
It currently supports **hCaptcha** (via Grounding DINO + an LLM prompt translator) and **Cloudflare Turnstile** (human-like mouse interaction).

---

## Installation

### Turnstile-only (lightweight)

```bash
pip install misstcha
playwright install chromium
```

### hCaptcha support (adds PyTorch + Transformers)

```bash
pip install "misstcha[hcaptcha]"
playwright install chromium
```

### Everything

```bash
pip install "misstcha[all]"
playwright install chromium
```

---

## Quick Start

### Solve Cloudflare Turnstile

```python
import asyncio
from playwright.async_api import async_playwright
from misstcha import TurnstileSolver

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto("https://example.com/protected-page")

        solver = TurnstileSolver(check_timeout=5.0, solve_delay=7.0)
        result = await solver.solve(page, wait_selector="#logged-in")

        if result["success"]:
            print("Turnstile solved!")
        else:
            print(f"Failed: {result['error']}")

        await browser.close()

asyncio.run(main())
```

### Solve hCaptcha

```python
import asyncio
from playwright.async_api import async_playwright
from misstcha import HCaptchaSolver

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto("https://example.com/hcaptcha-page")

        # Models are downloaded from Hugging Face on first run
        solver = HCaptchaSolver(
            vision_model="IDEA-Research/grounding-dino-base",
            llm_model="Qwen/Qwen2.5-0.5B-Instruct",
        )
        result = await solver.solve(page, max_retries=3)

        print(result)  # {"success": True, "attempts": 1, "error": None}

        await browser.close()

asyncio.run(main())
```

### Using the factory helper

```python
from misstcha import solve_captcha

result = await solve_captcha("turnstile", page)
result = await solve_captcha("hcaptcha", page, max_retries=3)
```

---

## Extending misstcha

`misstcha` is built around the `BaseSolver` abstract class and a `CaptchaSolverFactory` registry.  
Adding a new captcha type is three steps:

```python
# my_recaptcha_solver.py
from playwright.async_api import Page
from misstcha import BaseSolver, CaptchaSolverFactory

class ReCaptchaSolver(BaseSolver):
    async def solve(self, page: Page, **kwargs) -> dict:
        # ... your implementation ...
        return {"success": True, "error": None}

# Register so solve_captcha("recaptcha", page) works
CaptchaSolverFactory.register("recaptcha", ReCaptchaSolver)
```

1. Subclass `BaseSolver` and implement `async def solve(self, page, **kwargs) -> dict`.
2. Call `CaptchaSolverFactory.register("your-name", YourSolver)`.
3. Use `solve_captcha("your-name", page)` or `CaptchaSolverFactory.get_solver("your-name")`.

---

## Development

```bash
git clone https://github.com/your-org/coeus.git
cd coeus
pip install -e ".[dev,hcaptcha]"
pytest
```

---

## License
MIT
