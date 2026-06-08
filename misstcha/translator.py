import logging
import re
from transformers import pipeline

logger = logging.getLogger(__name__)


class PromptTranslator:
    def __init__(
        self, model_id: str = "Qwen/Qwen2.5-0.5B-Instruct", device: str = "cpu"
    ):
        """
        Initializes a lightweight text-generation model.
        Use device="cuda:0" or "mps" if you have GPU acceleration.
        """
        logger.info(f"Loading LLM translator ({model_id}) onto {device}...")

        # Pipeline setup for lightweight inference
        self.generator = pipeline(
            "text-generation",
            model=model_id,
            device=device,
            # We don't need long responses, just a few nouns
            max_new_tokens=20,
            # Prevent the pipeline from spitting the prompt back at us
            return_full_text=False,
        )

    def translate(self, raw_prompt: str) -> str:
        """
        Uses few-shot prompting to convert abstract phrases into concrete,
        dot-separated nouns for Grounding DINO.
        """
        clean_prompt = re.sub(
            r"please click each image containing\s*",
            "",
            raw_prompt.lower(),
            flags=re.IGNORECASE,
        ).strip()

        # Chat template to strictly constrain the model's output
        messages = [
            {
                "role": "system",
                "content": "You translate abstract categories into a dot-separated list of 5 concrete, common nouns that belong to that category. Do not include introductory text. Respond ONLY with the dot-separated nouns.",
            },
            {"role": "user", "content": "items that are usually seen outside"},
            {
                "role": "assistant",
                "content": "pinecone . tree . bicycle . car . cloud .",
            },
            {"role": "user", "content": "things with wheels"},
            {"role": "assistant", "content": "car . truck . bicycle . bus . train ."},
            {"role": "user", "content": "living things"},
            {"role": "assistant", "content": "dog . cat . person . bird . bug ."},
            {"role": "user", "content": clean_prompt},
        ]

        try:
            # Generate the translation
            output = self.generator(messages)
            translated_text = output[0]["generated_text"].strip()

            # Ensure it ends with a dot for DINO
            if not translated_text.endswith("."):
                translated_text += " ."

            return translated_text
        except Exception as e:
            logger.error(
                f"LLM translation failed: {e}. Falling back to original prompt."
            )
            return f"{clean_prompt} ."
