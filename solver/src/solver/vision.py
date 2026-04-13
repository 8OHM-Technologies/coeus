import io
from typing import List

import torch
from PIL import Image
from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor


class VisionManager:
    """
    Handles the initialization and inference of the Grounding DINO model.
    Keeps the model loaded in memory to prevent cold starts on every captcha.
    """

    def __init__(
        self, model_id: str = "IDEA-Research/grounding-dino-base", device: str = None
    ):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        print(f"Loading Grounding DINO ({model_id}) on {self.device.upper()}...")

        # Load processor and model via Hugging Face
        self.processor = AutoProcessor.from_pretrained(model_id)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id).to(
            self.device
        )
        self.model.eval()

        print("Vision model loaded successfully.")

    def detect_objects(
        self,
        image_bytes: bytes,
        prompt: str,
        box_threshold: float = 0.3,
        text_threshold: float = 0.25,
    ) -> List[List[float]]:
        """
        Takes the raw Playwright screenshot bytes and the captcha question.
        Returns a list of bounding boxes: [[xmin, ymin, xmax, ymax], ...]
        """
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        # --- PROMPT HACK ---
        # Example: "Select all gardening tools" -> "gardening tools."
        # 1. Lowercase everything
        clean_prompt = prompt.lower().strip()

        # 2. Strip hCaptcha's command language to isolate the noun phrase
        prefixes_to_strip = [
            "please select all ",
            "select all ",
            "click all ",
            "find all ",
        ]
        for prefix in prefixes_to_strip:
            if clean_prompt.startswith(prefix):
                clean_prompt = clean_prompt[len(prefix) :].strip()
                break

        # 3. Ensure it ends with a dot for DINO's text encoder
        if not clean_prompt.endswith("."):
            clean_prompt += "."
        # -------------------

        inputs = self.processor(
            images=image, text=clean_prompt, return_tensors="pt"
        ).to(self.device)

        with torch.no_grad():
            outputs = self.model(**inputs)

        target_sizes = torch.tensor([image.size[::-1]])

        results = self.processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            box_threshold=box_threshold,
            text_threshold=text_threshold,
            target_sizes=target_sizes,
        )[0]

        boxes = results["boxes"].cpu().numpy().tolist()

        return boxes
