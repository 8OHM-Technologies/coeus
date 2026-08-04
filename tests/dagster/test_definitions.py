import os
import sys
import json
import pytest
from unittest.mock import MagicMock

# Ensure dagster directory is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../dagster")))

import definitions
from definitions import to_bool, _blueprint_to_run_config, _build_container_env, fetch_blueprints


def test_to_bool():
    assert to_bool(True) is True
    assert to_bool(False) is False
    assert to_bool(None) is False
    assert to_bool("True") is True
    assert to_bool("  true  ") is True
    assert to_bool("1") is True
    assert to_bool("yes") is True
    assert to_bool("on") is True
    assert to_bool("False") is False
    assert to_bool(0) is False
    assert to_bool(1.5) is True
    assert to_bool([]) is False


def test_blueprint_to_run_config():
    blueprint = {
        "pipeline_id": "test_pipe",
        "scraper_type": "saflii",
        "metadata": {"document_type": "pdf"},
        "phase_1_ingestion": {
            "start_url": "https://start.com",
            "allow_insecure_https": True,
            "allow_insecure_requests": False,
            "use_proxy": True,
        },
        "phase_2_extraction": {
            "requires_extraction": True,
            "engine": "ollama/phi4-mini",
            "expected_schema": "BaseSchema",
            "extraction_instructions": "Do something.",
            "extraction_params": {"param1": "val1"},
        },
    }

    config = _blueprint_to_run_config(blueprint)
    
    ops = config["ops"]
    raw_config = ops["raw_scraped_pages"]["config"]
    assert raw_config["scraper_type"] == "saflii"
    assert raw_config["start_url"] == "https://start.com"
    assert raw_config["allow_insecure_https"] is True
    assert raw_config["allow_insecure_requests"] is False
    assert raw_config["use_proxy"] is True
    assert json.loads(raw_config["extraction_params"]) == {"param1": "val1"}

    extracted_config = ops["extracted_structured_data"]["config"]
    assert extracted_config["requires_extraction"] is True
    assert extracted_config["expected_schema"] == "BaseSchema"
    assert extracted_config["llm_engine"] == "ollama/phi4-mini"
    assert extracted_config["extraction_instructions"] == "Do something."


def test_build_container_env(monkeypatch):
    monkeypatch.setenv("DAGSTER_POSTGRES_USER", "user")
    monkeypatch.setenv("DAGSTER_POSTGRES_PASSWORD", "pwd")
    monkeypatch.setenv("COEUS_API_URL", "http://api")
    monkeypatch.setenv("OTHER_VAR", "ignored")

    env = _build_container_env()
    assert env["DAGSTER_POSTGRES_USER"] == "user"
    assert env["DAGSTER_POSTGRES_PASSWORD"] == "pwd"
    assert env["COEUS_API_URL"] == "http://api"
    assert "OTHER_VAR" not in env


def test_fetch_blueprints_from_cache(tmp_path, monkeypatch, mocker):
    cache_file = tmp_path / "cache.json"
    
    # Patch the module attributes directly since they are evaluated at import time
    monkeypatch.setattr(definitions, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(definitions, "CACHE_TTL", 60)
    
    blueprints_data = [{"pipeline_id": "cached_pipe"}]
    with open(cache_file, "w") as f:
        json.dump(blueprints_data, f)
        
    mock_get = mocker.patch("requests.get")

    # Fetch blueprints. Since cache file exists and is fresh, requests.get should not be called
    res = fetch_blueprints()
    assert res == blueprints_data
    mock_get.assert_not_called()


def test_fetch_blueprints_fallback_to_expired_cache(tmp_path, monkeypatch, mocker):
    cache_file = tmp_path / "cache.json"
    
    # Patch the module attributes directly
    monkeypatch.setattr(definitions, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(definitions, "CACHE_TTL", 0)  # Make it expire immediately
    
    blueprints_data = [{"pipeline_id": "expired_pipe"}]
    with open(cache_file, "w") as f:
        json.dump(blueprints_data, f)

    # Mock requests.get to fail to trigger fallback to expired cache
    mocker.patch("requests.get", side_effect=Exception("API offline"))

    res = fetch_blueprints()
    assert res == blueprints_data


def test_scrubbed_extracted_records_asset_exists():
    from definitions import defs
    
    # Get all asset keys from the definitions
    asset_graph = defs.resolve_asset_graph()
    asset_keys = {key.to_string() for key in asset_graph.get_all_asset_keys()}
    
    # Assert the new asset is registered
    assert '["scrubbed_extracted_records"]' in asset_keys
    
    # Check dependency relation: scrubbed_extracted_records depends on extracted_structured_data
    scrub_key = [key for key in asset_graph.get_all_asset_keys() if key.to_string() == '["scrubbed_extracted_records"]'][0]
    parent_keys = {parent.to_string() for parent in asset_graph.get(scrub_key).parent_keys}
    
    assert '["extracted_structured_data"]' in parent_keys


def test_sabinet_job_and_sensors_exist():
    from definitions import defs
    
    assert defs.get_job_def("sabinet_scrubbing_job").name == "sabinet_scrubbing_job"
    assert defs.get_sensor_def("sabinet_sync_sensor").name == "sabinet_sync_sensor"
    assert defs.get_sensor_def("scrubbed_sensor").name == "scrubbed_sensor"
