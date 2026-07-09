import os
import sys
import pytest
import requests
import urllib3
from utils.utils import fetch_pipeline_config, download_pdf, take_screenshot

# Ensure extraction_workers/utils is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))


@pytest.mark.asyncio
async def test_fetch_pipeline_config_from_env(monkeypatch, mocker):
    # Set mock environment variables
    monkeypatch.setenv("START_URL", "https://env.example.com")
    monkeypatch.setenv("DOCUMENT_TYPE", "pdf")
    monkeypatch.setenv("CAT_SELECTOR", ".cat")
    monkeypatch.setenv("DOC_SELECTOR", ".doc")
    monkeypatch.setenv("ALLOW_INSECURE_HTTPS", "True")
    monkeypatch.setenv("ALLOW_INSECURE_REQUESTS", "True")
    monkeypatch.setenv("USE_PROXY", "False")
    monkeypatch.setenv("EXTRACTION_PARAMS", '{"keyword": "mining"}')

    # Mock disable_warnings to check if it's called
    mock_disable = mocker.patch("urllib3.disable_warnings")

    config = await fetch_pipeline_config("test_pipeline")

    assert config["start_url"] == "https://env.example.com"
    assert config["document_type"] == "pdf"
    assert config["allow_insecure_https"] is True
    assert config["allow_insecure_requests"] is True
    assert config["use_proxy"] is False
    assert config["extraction_params"] == {"keyword": "mining"}
    mock_disable.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_pipeline_config_fallback_to_api(monkeypatch, mocker):
    # Ensure environment variables are clear
    monkeypatch.delenv("START_URL", raising=False)
    monkeypatch.delenv("DOCUMENT_TYPE", raising=False)

    mock_blueprint = {
        "pipeline_id": "test_pipeline",
        "name": "Test Pipeline",
        "scraper_type": "sedarplus",
        "is_active": True,
        "schedule": "0 0 * * *",
        "metadata": {
            "industry": "Finance",
            "document_type": "pdf",
        },
        "phase_1_ingestion": {
            "start_url": "https://api.example.com",
            "allow_insecure_https": False,
            "allow_insecure_requests": True,
            "use_proxy": True,
        },
        "phase_2_extraction": {
            "requires_extraction": True,
            "engine": "ollama/phi4-mini",
            "expected_schema": "GenericDocumentExtraction",
            "extraction_instructions": "Extract data.",
            "extraction_params": {"api_param": 123},
        },
        "phase_3_loading": {"table_name": "api_table"},
    }

    # Mock the API response
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"pipelines": [mock_blueprint]}
    mock_get = mocker.patch("requests.get", return_value=mock_response)
    mock_disable = mocker.patch("urllib3.disable_warnings")

    config = await fetch_pipeline_config("test_pipeline")

    mock_get.assert_called_once()
    assert config["start_url"] == "https://api.example.com"
    assert config["document_type"] == "pdf"
    assert config["use_proxy"] is True
    assert config["extraction_params"] == {"api_param": 123}
    mock_disable.assert_called_once()


def test_download_pdf_success(tmp_path, mocker):
    save_dir = str(tmp_path)
    file_name = "Test Case Decision 123.pdf"
    
    # Mock requests.get response stream
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.iter_content.return_value = [b"pdf data chunk 1", b"chunk 2"]
    
    mocker.patch("requests.get", return_value=mock_response)

    download_pdf("https://download.com/test.pdf", save_dir, file_name)

    # Verify that the filename was sanitized (spaces replaced with underscores)
    expected_path = os.path.join(save_dir, "Test_Case_Decision_123.pdf")
    assert os.path.exists(expected_path)
    with open(expected_path, "rb") as f:
        content = f.read()
    assert content == b"pdf data chunk 1chunk 2"


def test_download_pdf_skips_existing(tmp_path, mocker):
    save_dir = str(tmp_path)
    file_name = "AlreadyExists.pdf"
    file_path = os.path.join(save_dir, "AlreadyExists.pdf")
    
    # Pre-create the file
    with open(file_path, "w") as f:
        f.write("existing content")

    mock_get = mocker.patch("requests.get")

    download_pdf("https://download.com/exists.pdf", save_dir, file_name)

    # Verify requests.get was NOT called because file exists
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_take_screenshot(tmp_path, mocker):
    screenshot_dir = os.path.join(str(tmp_path), "screenshots")
    
    mock_page = mocker.Mock()
    # Mock screenshot method to be awaitable
    async def dummy_screenshot(**kwargs):
        pass
    mock_page.screenshot = mocker.Mock(side_effect=dummy_screenshot)

    await take_screenshot(mock_page, screenshot_dir, "test_shot")

    mock_page.screenshot.assert_called_once()
    # Check that it saves within screenshot_dir
    path_arg = mock_page.screenshot.call_args[1]["path"]
    assert path_arg.startswith(screenshot_dir)
    assert path_arg.endswith("_test_shot.png")
