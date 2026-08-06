"""GLiNER-based PII Redaction Module.

Provides zero-shot Personally Identifiable Information (PII) entity detection
and redaction across strings and structured dictionaries.
"""

import json
import logging
from typing import Any, Union

try:
    from gliner import GLiNER
    _gliner_import_error = None
except Exception as exc:
    GLiNER = None
    _gliner_import_error = exc

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "urchade/gliner_multi_pii-v1"

DEFAULT_PII_LABELS = [
    "person",
    "date of birth",
    "social security number",
    "email",
    "phone number",
    "medical facility",
    "insurance id",
    "address",
]


class Scrub:
    """Zero-shot PII scrubber powered by GLiNER."""

    _models: dict[str, Any] = {}

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        default_labels: list[str] | None = None,
        threshold: float = 0.5,
    ) -> None:
        self.model_name = model_name
        self.default_labels = default_labels or DEFAULT_PII_LABELS
        self.threshold = threshold

    @property
    def model(self) -> Any:
        """Lazily load and cache the GLiNER model."""
        if self.model_name not in Scrub._models:
            if GLiNER is None:
                logger.warning(f"GLiNER library is not available ({_gliner_import_error}). PII scrubbing will be bypassed.")
                return None
            try:
                logger.info(f"Loading GLiNER model '{self.model_name}'...")
                Scrub._models[self.model_name] = GLiNER.from_pretrained(self.model_name)
                logger.info(f"GLiNER model '{self.model_name}' loaded successfully.")
            except Exception as exc:
                logger.error(f"Failed to load GLiNER model '{self.model_name}': {exc}")
                return None
        return Scrub._models.get(self.model_name)

    def _split_into_chunks(self, text: str, max_chars: int = 1500) -> list[str]:
        """Splits a long string into chunks under max_chars length, preserving formatting."""
        if not text:
            return []

        import re
        # Split on sentence boundaries and newlines, preserving the delimiters
        raw_tokens = re.split(r"(\n|\. |\? |\! )", text)

        chunks = []
        current_chunk = []
        current_length = 0

        for token in raw_tokens:
            if not token:
                continue
            if current_length + len(token) > max_chars and current_chunk:
                chunks.append("".join(current_chunk))
                current_chunk = [token]
                current_length = len(token)
            else:
                current_chunk.append(token)
                current_length += len(token)

        if current_chunk:
            chunks.append("".join(current_chunk))

        # Further split any exceptionally long individual tokens that exceed max_chars
        final_chunks = []
        for chunk in chunks:
            if len(chunk) <= max_chars:
                final_chunks.append(chunk)
            else:
                for i in range(0, len(chunk), max_chars):
                    final_chunks.append(chunk[i : i + max_chars])

        return final_chunks

    def scrub_text(
        self,
        text: str,
        labels: list[str] | None = None,
        threshold: float | None = None,
        batch_size: int = 32,
    ) -> str:
        """Redacts PII entities from text by splitting it into chunks and batch processing with GLiNER."""
        if not text or not isinstance(text, str) or not text.strip():
            return text

        if self.model is None:
            return text

        active_labels = labels or self.default_labels
        active_threshold = threshold if threshold is not None else self.threshold

        chunks = self._split_into_chunks(text)
        if not chunks:
            return text

        try:
            # Use GLiNER batch inference for all chunks
            all_entities = self.model.inference(
                chunks,
                active_labels,
                batch_size=batch_size,
                threshold=active_threshold,
            )
        except Exception as exc:
            logger.error(f"GLiNER batch inference failed: {exc}")
            return text

        redacted_chunks = []
        for chunk, entities in zip(chunks, all_entities):
            if not entities:
                redacted_chunks.append(chunk)
                continue

            redacted_chunk = chunk
            # Sort entities by start index in reverse order to preserve substring offsets
            for entity in sorted(entities, key=lambda e: e["start"], reverse=True):
                start = entity["start"]
                end = entity["end"]
                label_tag = f"[{entity['label'].upper()}]"
                redacted_chunk = redacted_chunk[:start] + label_tag + redacted_chunk[end:]
            redacted_chunks.append(redacted_chunk)

        return "".join(redacted_chunks)

    def scrub_dict(
        self,
        data: dict[str, Any],
        labels: list[str] | None = None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        """Recursively traverses and redacts PII within nested dictionary structures."""
        scrubbed = {}
        for key, value in data.items():
            if isinstance(value, str):
                scrubbed[key] = self.scrub_text(value, labels=labels, threshold=threshold)
            elif isinstance(value, dict):
                scrubbed[key] = self.scrub_dict(value, labels=labels, threshold=threshold)
            elif isinstance(value, list):
                scrubbed[key] = self._scrub_list(value, labels=labels, threshold=threshold)
            else:
                scrubbed[key] = value
        return scrubbed

    def _scrub_list(
        self,
        items: list[Any],
        labels: list[str] | None = None,
        threshold: float | None = None,
    ) -> list[Any]:
        """Recursively redacts PII in list elements."""
        result = []
        for item in items:
            if isinstance(item, str):
                result.append(self.scrub_text(item, labels=labels, threshold=threshold))
            elif isinstance(item, dict):
                result.append(self.scrub_dict(item, labels=labels, threshold=threshold))
            elif isinstance(item, list):
                result.append(self._scrub_list(item, labels=labels, threshold=threshold))
            else:
                result.append(item)
        return result

    def scrub(
        self,
        input_data: Union[str, dict[str, Any]],
        labels: list[str] | None = None,
        threshold: float | None = None,
    ) -> Union[str, dict[str, Any]]:
        """Convenience entry point for scrubbing either raw strings, JSON strings, or dicts."""
        if isinstance(input_data, dict):
            return self.scrub_dict(input_data, labels=labels, threshold=threshold)

        if isinstance(input_data, str):
            trimmed = input_data.strip()
            if (trimmed.startswith("{") and trimmed.endswith("}")) or (trimmed.startswith("[") and trimmed.endswith("]")):
                try:
                    parsed = json.loads(input_data)
                    if isinstance(parsed, dict):
                        scrubbed_dict = self.scrub_dict(parsed, labels=labels, threshold=threshold)
                        return json.dumps(scrubbed_dict, ensure_ascii=False)
                    if isinstance(parsed, list):
                        scrubbed_list = self._scrub_list(parsed, labels=labels, threshold=threshold)
                        return json.dumps(scrubbed_list, ensure_ascii=False)
                except Exception:
                    pass
            return self.scrub_text(input_data, labels=labels, threshold=threshold)

        return input_data


if __name__ == "__main__":
    scrubber = Scrub()
    sample_text = """
Patient John Smith (DOB: 03/15/1982, SSN: 123-45-6789) was seen at
Mayo Clinic on January 10, 2024. Contact: john.smith@email.com,
+1-555-867-5309. Insurance ID: BC-9876543. His home address is
742 Evergreen Terrace, Springfield, IL 62704.
"""
    print("Original Text:\n", sample_text)
    print("Redacted Output:\n", scrubber.scrub_text(sample_text))