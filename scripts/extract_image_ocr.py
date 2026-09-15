#!/usr/bin/env python3
"""
Coeus Image OCR Text Extraction Script
=======================================
Extracts text from images using optical character recognition (OCR).
Defaults to high-performance ONNX-based RapidOCR (pure-Python execution
with zero external C++ dependencies), with optional support for Tesseract.

Usage Examples:
    # Basic text extraction from an image:
    python scripts/extract_image_ocr.py sample.png

    # Structured JSON output with bounding boxes and confidence scores:
    python scripts/extract_image_ocr.py sample.png --format json

    # Save output to file:
    python scripts/extract_image_ocr.py sample.png -o extracted.txt

    # Preprocessing with grayscale and binarization:
    python scripts/extract_image_ocr.py sample.png --preprocess binarize

    # Pipe friendly (quiet mode):
    python scripts/extract_image_ocr.py sample.png --quiet
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

# Set up logger
logger = logging.getLogger("extract_image_ocr")


@dataclass
class OCRLine:
    """Represents an individual extracted line of text with metadata."""
    text: str
    confidence: float
    box: Optional[List[List[float]]] = None


@dataclass
class OCRResult:
    """Full extraction result container."""
    image_path: str
    engine: str
    text: str
    lines: List[OCRLine]
    execution_time_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_path": self.image_path,
            "engine": self.engine,
            "text": self.text,
            "lines": [asdict(line) for line in self.lines],
            "execution_time_seconds": round(self.execution_time_seconds, 4),
        }


def preprocess_image(image: Image.Image, mode: str) -> Image.Image:
    """
    Applies image enhancement/preprocessing filters to improve OCR quality.

    Args:
        image: Source PIL Image.
        mode: Preprocessing technique ('none', 'grayscale', 'binarize', 'denoise').

    Returns:
        Preprocessed PIL Image.
    """
    if mode == "none":
        return image

    # Convert to grayscale first for non-none modes
    processed = ImageOps.grayscale(image)

    if mode == "grayscale":
        # Enhance contrast slightly
        enhancer = ImageEnhance.Contrast(processed)
        return enhancer.enhance(1.5)

    if mode == "binarize":
        # Otsu-like adaptive thresholding / contrast stretch
        enhancer = ImageEnhance.Contrast(processed)
        processed = enhancer.enhance(2.0)
        # Apply thresholding (cutoff at 128)
        threshold = 128
        return processed.point(lambda p: 255 if p > threshold else 0, mode="1")

    if mode == "denoise":
        # Median filter to remove noise artifacts
        processed = processed.filter(ImageFilter.MedianFilter(size=3))
        enhancer = ImageEnhance.Sharpness(processed)
        return enhancer.enhance(1.5)

    return image


def run_rapidocr(image_path: Path | str, min_score: float = 0.0) -> list[OCRLine]:
    """
    Executes OCR using the RapidOCR engine.

    Args:
        image_path: Path to the target image file.
        min_score: Minimum confidence score filter (0.0 to 1.0).

    Returns:
        List of OCRLine objects.
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "RapidOCR is not installed. Install it with: "
            "pip install rapidocr-onnxruntime"
        ) from exc

    engine = RapidOCR()
    raw_result, _ = engine(str(image_path))

    if not raw_result:
        return []

    lines: list[OCRLine] = []
    for item in raw_result:
        # RapidOCR format: [box, text, score]
        box = item[0]
        text = str(item[1]).strip()
        score = float(item[2])

        if score >= min_score and text:
            lines.append(OCRLine(text=text, confidence=round(score, 4), box=box))

    return lines


def run_pytesseract(image_path: Path | str, min_score: float = 0.0) -> list[OCRLine]:
    """
    Executes OCR using the Tesseract OCR engine (via pytesseract).

    Args:
        image_path: Path to the target image file.
        min_score: Minimum confidence score filter (0.0 to 1.0).

    Returns:
        List of OCRLine objects.
    """
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError(
            "pytesseract is not installed. Install it with: pip install pytesseract"
        ) from exc

    try:
        # Extract data with confidence scores and bounding boxes
        data = pytesseract.image_to_data(
            Image.open(image_path), output_type=pytesseract.Output.DICT
        )
    except Exception as exc:
        raise RuntimeError(
            f"Tesseract execution failed: {exc}. Ensure the tesseract binary is installed."
        ) from exc

    n_boxes = len(data["text"])
    lines: list[OCRLine] = []

    for i in range(n_boxes):
        text = str(data["text"][i]).strip()
        conf = float(data["conf"][i])
        # pytesseract returns confidence 0-100 or -1 for blank blocks
        if conf > 0:
            score = conf / 100.0
            if score >= min_score and text:
                x, y, w, h = (
                    data["left"][i],
                    data["top"][i],
                    data["width"][i],
                    data["height"][i],
                )
                box = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
                lines.append(OCRLine(text=text, confidence=round(score, 4), box=box))

    return lines


def extract_text_from_image(
    image_path: Path | str,
    engine: str = "auto",
    preprocess: str = "none",
    min_score: float = 0.0,
    pad: int = 20,
) -> OCRResult:
    """
    Extracts text from an image using the specified engine and preprocessing.

    Args:
        image_path: Path to the image file.
        engine: 'rapidocr', 'tesseract', or 'auto'.
        preprocess: 'none', 'grayscale', 'binarize', or 'denoise'.
        min_score: Minimum confidence score threshold.
        pad: Border margin padding in pixels added around image edges to detect edge-touching text.

    Returns:
        OCRResult with extracted lines and combined text.
    """
    path = Path(image_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image file does not exist: {path}")

    start_time = time.perf_counter()

    # Preprocess image or apply edge padding if requested
    active_image_path = path
    temp_preprocessed_path: Optional[Path] = None

    if preprocess != "none" or pad > 0:
        try:
            with Image.open(path) as img:
                # First apply edge padding if requested
                if pad > 0:
                    processed_img = ImageOps.expand(img, border=pad, fill="white")
                else:
                    processed_img = img.copy()

                # Then apply filter preprocessing
                if preprocess != "none":
                    processed_img = preprocess_image(processed_img, mode=preprocess)

                temp_preprocessed_path = (
                    path.parent / f".tmp_{path.stem}_p{pad}_{preprocess}.png"
                )
                processed_img.save(temp_preprocessed_path, format="PNG")
                active_image_path = temp_preprocessed_path
        except Exception as exc:
            logger.warning(
                f"Image preparation/preprocessing failed ({exc}); proceeding with original image."
            )

    chosen_engine = engine
    lines: list[OCRLine] = []

    try:
        if engine in ("rapidocr", "auto"):
            try:
                lines = run_rapidocr(active_image_path, min_score=min_score)
                chosen_engine = "rapidocr"
            except Exception as e:
                if engine == "auto":
                    logger.info(
                        f"RapidOCR unavailable ({e}), attempting Tesseract fallback..."
                    )
                    lines = run_pytesseract(active_image_path, min_score=min_score)
                    chosen_engine = "tesseract"
                else:
                    raise
        elif engine == "tesseract":
            lines = run_pytesseract(active_image_path, min_score=min_score)
        else:
            raise ValueError(
                f"Unknown OCR engine '{engine}'. Valid options: 'rapidocr', 'tesseract', 'auto'."
            )
    finally:
        # Clean up temporary preprocessed image if created
        if temp_preprocessed_path and temp_preprocessed_path.exists():
            try:
                temp_preprocessed_path.unlink()
            except OSError:
                pass

    # Adjust bounding boxes if padding was applied
    if pad > 0:
        for line in lines:
            if line.box:
                adjusted_box: list[list[float]] = []
                for point in line.box:
                    adjusted_box.append([max(0.0, point[0] - pad), max(0.0, point[1] - pad)])
                line.box = adjusted_box

    elapsed = time.perf_counter() - start_time
    combined_text = "\n".join(line.text for line in lines)

    return OCRResult(
        image_path=str(path),
        engine=chosen_engine,
        text=combined_text,
        lines=lines,
        execution_time_seconds=elapsed,
    )


def build_parser() -> argparse.ArgumentParser:
    """Builds the argument parser for the CLI script."""
    parser = argparse.ArgumentParser(
        description="Extract text from an image using Optical Character Recognition (OCR)."
    )
    parser.add_argument(
        "image",
        nargs="?",
        default=None,
        help="Path to the input image file (JPEG, PNG, WEBP, TIFF, BMP, etc.).",
    )
    parser.add_argument(
        "--image",
        "-i",
        dest="image_opt",
        default=None,
        help="Path to the input image file (alternative to positional argument).",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json", "detailed"],
        default="text",
        help="Output format: 'text' (plain extracted text), 'json' (structured with bounding boxes/confidence), 'detailed' (human-readable list with scores).",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Path to save the output. If not provided, output is printed to stdout.",
    )
    parser.add_argument(
        "--engine",
        "-e",
        choices=["rapidocr", "tesseract", "auto"],
        default="auto",
        help="OCR engine to use. Defaults to 'auto' (prefers RapidOCR).",
    )
    parser.add_argument(
        "--preprocess",
        "-p",
        choices=["none", "grayscale", "binarize", "denoise"],
        default="none",
        help="Image preprocessing mode to enhance OCR accuracy on difficult or low-contrast images.",
    )
    parser.add_argument(
        "--min-score",
        "-s",
        type=float,
        default=0.0,
        help="Minimum confidence threshold between 0.0 and 1.0 (filters out low-confidence detections).",
    )
    parser.add_argument(
        "--pad",
        type=int,
        default=20,
        help="Margin border padding in pixels added around image edges to capture edge-touching text (default: 20, 0 to disable).",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress informational logs and banner output (clean for Unix shell piping).",
    )
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    image_path = args.image_opt or args.image
    if not image_path:
        parser.print_help(sys.stderr)
        print("\nError: Please provide an image file path.", file=sys.stderr)
        return 1

    # Configure logging
    log_level = logging.ERROR if args.quiet else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    try:
        result = extract_text_from_image(
            image_path=image_path,
            engine=args.engine,
            preprocess=args.preprocess,
            min_score=args.min_score,
            pad=args.pad,
        )
    except FileNotFoundError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    except Exception as err:
        print(f"Extraction failed: {err}", file=sys.stderr)
        return 3

    # Format output
    if args.format == "json":
        output_str = json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
    elif args.format == "detailed":
        header = (
            f"=== OCR Results ({result.engine}) ===\n"
            f"Image: {result.image_path}\n"
            f"Execution time: {result.execution_time_seconds:.4f}s\n"
            f"Detected lines: {len(result.lines)}\n"
            f"----------------------------------------"
        )
        body = "\n".join(
            f"[{idx + 1}] (conf: {line.confidence:.2f}) {line.text}"
            for idx, line in enumerate(result.lines)
        )
        output_str = f"{header}\n{body}\n----------------------------------------\nFull Text:\n{result.text}"
    else:
        output_str = result.text

    # Write output to file or stdout
    if args.output:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output_str, encoding="utf-8")
            if not args.quiet:
                logger.info(f"Results successfully saved to {args.output}")
        except Exception as exc:
            print(f"Error writing output to {args.output}: {exc}", file=sys.stderr)
            return 4
    else:
        print(output_str)

    return 0


if __name__ == "__main__":
    sys.exit(main())
