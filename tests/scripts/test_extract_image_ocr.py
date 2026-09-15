import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from scripts.extract_image_ocr import (
    extract_text_from_image,
    preprocess_image,
)


@pytest.fixture
def sample_ocr_image(tmp_path: Path) -> Path:
    """Generates a clean synthetic image containing known text."""
    img_path = tmp_path / "test_ocr_sample.png"
    width, height = 400, 150
    image = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)

    # Use default bitmap font or basic text drawing
    draw.text((20, 30), "COEUS PIPELINE", fill=(0, 0, 0))
    draw.text((20, 80), "OCR TEST 2026", fill=(0, 0, 0))

    image.save(img_path)
    return img_path


def test_extract_text_python_api(sample_ocr_image: Path):
    """Tests text extraction via Python function call."""
    result = extract_text_from_image(
        image_path=sample_ocr_image,
        engine="rapidocr",
        preprocess="none",
    )

    assert result.engine == "rapidocr"
    assert "COEUS" in result.text.upper()
    assert len(result.lines) >= 1
    assert result.lines[0].confidence > 0.0
    assert result.lines[0].box is not None


def test_preprocessing_modes(sample_ocr_image: Path):
    """Tests that image preprocessing modes return valid PIL images."""
    with Image.open(sample_ocr_image) as img:
        for mode in ["none", "grayscale", "binarize", "denoise"]:
            processed = preprocess_image(img, mode=mode)
            assert processed is not None
            assert processed.size == img.size


def test_cli_execution_text_stdout(sample_ocr_image: Path):
    """Tests CLI execution with plain text stdout output."""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "extract_image_ocr.py"

    cmd = [sys.executable, str(script_path), str(sample_ocr_image), "--quiet"]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=True)

    assert "COEUS" in completed.stdout.upper()


def test_cli_execution_json_format(sample_ocr_image: Path):
    """Tests CLI execution with structured JSON output."""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "extract_image_ocr.py"

    cmd = [
        sys.executable,
        str(script_path),
        str(sample_ocr_image),
        "--format",
        "json",
        "--quiet",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=True)

    data = json.loads(completed.stdout)
    assert "lines" in data
    assert "engine" in data
    assert data["engine"] == "rapidocr"
    assert any("COEUS" in line["text"].upper() for line in data["lines"])


def test_cli_output_to_file(sample_ocr_image: Path, tmp_path: Path):
    """Tests writing CLI output directly to a target file."""
    script_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "extract_image_ocr.py"
    output_file = tmp_path / "extracted_output.txt"

    cmd = [
        sys.executable,
        str(script_path),
        str(sample_ocr_image),
        "-o",
        str(output_file),
        "--quiet",
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)

    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert "COEUS" in content.upper()


def test_nonexistent_image():
    """Tests error handling for nonexistent image paths."""
    with pytest.raises(FileNotFoundError):
        extract_text_from_image("nonexistent_path_to_image_12345.png")
