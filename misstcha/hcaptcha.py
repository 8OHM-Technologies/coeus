import asyncio
import io
import logging
from typing import Any, Dict
from PIL import Image, ImageDraw
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .base import BaseSolver
from .translator import PromptTranslator
from .vision import VisionManager

logger = logging.getLogger(__name__)


class HCaptchaSolver(BaseSolver):
    def __init__(
        self,
        vision_model: str = "IDEA-Research/grounding-dino-base",
        llm_model: str = "Qwen/Qwen2.5-0.5B-Instruct",
        device: str = None,
    ):
        self.vision = VisionManager(model_id=vision_model, device=device)
        self.translator = PromptTranslator(model_id=llm_model, device=device)

    def _process_slices_sync(
        self, image_bytes: bytes, prompt: str
    ) -> tuple[list[int], list[list[float]]]:
        """
        Slices the 3x3 grid into 9 individual images, runs inference on each,
        and translates the bounding boxes back to the original image coordinates.
        """
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size

        # hCaptcha grids are always 3x3
        cell_w = width / 3.0
        cell_h = height / 3.0

        indexes_to_click = []
        absolute_boxes = []

        # Iterate through the 9 cells (0 to 8)
        for i in range(9):
            row = i // 3
            col = i % 3

            left = int(col * cell_w)
            upper = int(row * cell_h)
            right = int((col + 1) * cell_w)
            lower = int((row + 1) * cell_h)

            # Crop the current cell
            slice_img = image.crop((left, upper, right, lower))

            # Convert slice to bytes for the vision manager
            slice_io = io.BytesIO()
            slice_img.save(slice_io, format="PNG")
            slice_bytes = slice_io.getvalue()

            # Run inference on the single slice
            boxes = self.vision.detect_objects(image_bytes=slice_bytes, prompt=prompt)

            # If the model found the object in this slice, mark the index
            if boxes and len(boxes) > 0:
                indexes_to_click.append(i)

                # Translate the local slice coordinates back to the global 380x380 image
                for box in boxes:
                    abs_box = [
                        box[0] + left,  # x_min
                        box[1] + upper,  # y_min
                        box[2] + left,  # x_max
                        box[3] + upper,  # y_max
                    ]
                    absolute_boxes.append(abs_box)

        return indexes_to_click, absolute_boxes

    async def solve(self, page: Page, max_retries: int = 3, **kwargs) -> Dict[str, Any]:
        checkbox_frame = page.frame_locator(
            'iframe[title*="Widget containing checkbox for hCaptcha security challenge"]'
        )
        challenge_frame = page.frame_locator('iframe[title*="hCaptcha challenge"]')

        try:
            logger.info("Locating and clicking hCaptcha checkbox...")
            await checkbox_frame.locator("#checkbox").click(timeout=5000)
        except PlaywrightTimeoutError:
            return {"success": False, "error": "Checkbox not found."}

        for attempt in range(1, max_retries + 1):
            logger.info(f"Solving attempt {attempt} of {max_retries}...")

            try:
                grid_container = challenge_frame.locator(".task-grid")
                await grid_container.wait_for(state="visible", timeout=10000)
                await asyncio.sleep(1.5)

                prompt_locator = challenge_frame.locator("h2.prompt-text")
                raw_prompt_text = await prompt_locator.inner_text()

                logger.info(f"Translating prompt: '{raw_prompt_text}'")
                dino_prompt = await asyncio.to_thread(
                    self.translator.translate, raw_prompt_text
                )
                logger.info(f"DINO-friendly prompt generated: {dino_prompt}")

                grid_image_bytes = await grid_container.screenshot(
                    type="png", path="/app/data/scraped_pdfs/hcaptcha_grid.png"
                )

                # --- Run the slicing logic in a background thread ---
                logger.info("Slicing image and running vision model inference...")
                indexes_to_click, bounding_boxes = await asyncio.to_thread(
                    self._process_slices_sync,
                    image_bytes=grid_image_bytes,
                    prompt=dino_prompt,
                )

                logger.info(f"Model selected indexes: {indexes_to_click}")

                # Draw bounding boxes
                try:
                    image = Image.open(io.BytesIO(grid_image_bytes))
                    draw = ImageDraw.Draw(image)
                    for box in bounding_boxes:
                        draw.rectangle(box, outline="red", width=4)

                    debug_image_path = f"/app/data/scraped_pdfs/hcaptcha_grid_boxed_attempt_{attempt}.png"
                    image.save(debug_image_path)
                except Exception as e:
                    logger.error(f"Failed to draw or save bounding boxes: {e}")

                # Execute clicks
                task_elements = await challenge_frame.locator(".task-grid .task").all()
                for index in indexes_to_click:
                    if index < len(task_elements):
                        await task_elements[index].click()
                        await asyncio.sleep(0.3)

                await challenge_frame.locator(".button-submit").click()

                try:
                    await checkbox_frame.locator(
                        '#checkbox[aria-checked="true"]'
                    ).wait_for(timeout=5000)
                    logger.info("Captcha successfully solved!")
                    return {"success": True, "attempts": attempt, "error": None}

                except PlaywrightTimeoutError:
                    logger.warning("Puzzle failed. Retrying...")
                    continue

            except PlaywrightTimeoutError:
                is_checked = await checkbox_frame.locator("#checkbox").get_attribute(
                    "aria-checked"
                )
                if is_checked == "true":
                    return {"success": True, "attempts": 0, "error": None}
                else:
                    return {
                        "success": False,
                        "error": "Challenge iframe never became visible.",
                    }
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": False, "error": "Maximum retries reached without success."}

    async def solve_captcha(self, page: Page, max_retries: int = 3) -> Dict[str, Any]:
        """Backward compatibility wrapper for legacy callers."""
        return await self.solve(page, max_retries=max_retries)
