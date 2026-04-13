import asyncio
import logging
from typing import Any, Dict

# Assuming the user has installed playwright: pip install playwright
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .utils import get_grid_indexes

# Import our custom modules
from .vision import VisionManager

logger = logging.getLogger(__name__)


class HCaptchaSolver:
    """
    The main asynchronous solver class.
    Maintains the vision model in memory and executes Playwright interactions.
    """

    def __init__(
        self, model_id: str = "IDEA-Research/grounding-dino-base", device: str = None
    ):
        # Initialize the model once when the class is instantiated
        self.vision = VisionManager(model_id=model_id, device=device)

    async def solve_captcha(self, page: Page, max_retries: int = 3) -> Dict[str, Any]:
        """
        Takes an active Playwright Page object, locates the hCaptcha instances,
        and attempts to solve them up to `max_retries` times.
        """
        # 1. Define the frame locators
        # hCaptcha uses specific titles for its iframes which makes targeting them reliable
        checkbox_frame = page.frame_locator('iframe[title*="checkbox"]')
        challenge_frame = page.frame_locator('iframe[title*="challenge"]')

        # 2. Click the initial "I am human" checkbox
        try:
            logger.info("Locating and clicking hCaptcha checkbox...")
            await checkbox_frame.locator("#checkbox").click(timeout=5000)
        except PlaywrightTimeoutError:
            return {
                "success": False,
                "error": "Checkbox not found on the provided page.",
            }

        # 3. Enter the solving loop
        for attempt in range(1, max_retries + 1):
            logger.info(f"Solving attempt {attempt} of {max_retries}...")

            try:
                # Wait for the challenge grid to appear and stabilize
                grid_container = challenge_frame.locator(".task-grid")
                await grid_container.wait_for(state="visible", timeout=10000)

                # Small sleep to ensure images are fully loaded inside the grid
                await asyncio.sleep(1.5)

                # Extract the prompt text
                prompt_locator = challenge_frame.locator("h2.prompt-text")
                prompt_text = await prompt_locator.inner_text()
                logger.info(f"Target object: {prompt_text}")

                # Take a screenshot of the entire 380x380 grid
                grid_image_bytes = await grid_container.screenshot(type="jpeg")

                # 4. Run PyTorch inference in a separate thread to prevent blocking the async loop
                logger.info("Running vision model inference...")
                bounding_boxes = await asyncio.to_thread(
                    self.vision.detect_objects,
                    image_bytes=grid_image_bytes,
                    prompt=prompt_text,
                )

                # 5. Convert bounding boxes to grid indexes
                indexes_to_click = get_grid_indexes(bounding_boxes)
                logger.info(f"Model selected indexes: {indexes_to_click}")

                # 6. Execute clicks
                task_elements = await challenge_frame.locator(".task-grid .task").all()
                for index in indexes_to_click:
                    # Double check we don't index out of bounds on weird edge cases
                    if index < len(task_elements):
                        await task_elements[index].click()
                        # Humanize the click delay slightly
                        await asyncio.sleep(0.3)

                # 7. Submit the challenge
                await challenge_frame.locator(".button-submit").click()

                # 8. Check for success or a new challenge
                # We wait to see if the checkbox frame reports success, or if a new grid appears
                try:
                    # If aria-checked becomes true, the captcha is solved
                    await checkbox_frame.locator(
                        '#checkbox[aria-checked="true"]'
                    ).wait_for(timeout=5000)
                    logger.info("Captcha successfully solved!")
                    return {"success": True, "attempts": attempt, "error": None}

                except PlaywrightTimeoutError:
                    # If it didn't succeed, it means we failed and a new puzzle loaded. Loop continues.
                    logger.warning("Puzzle failed. Retrying...")
                    continue

            except PlaywrightTimeoutError:
                # This usually triggers if the captcha auto-passed on the first click
                # without showing a picture grid at all.
                is_checked = await checkbox_frame.locator("#checkbox").get_attribute(
                    "aria-checked"
                )
                if is_checked == "true":
                    logger.info("Captcha auto-passed without a visual challenge!")
                    return {"success": True, "attempts": 0, "error": None}
                else:
                    return {
                        "success": False,
                        "error": "Challenge iframe never became visible.",
                    }
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Maximum retries reached without success."}
