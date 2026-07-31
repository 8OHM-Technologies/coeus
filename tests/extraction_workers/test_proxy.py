import os
import sys
import pytest
from utils.browser_helper import format_sb_proxy, verify_sb_outbound_ip
from utils.debug_helper import log_sb_proxy_ip_sync

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))


def test_format_sb_proxy_with_scheme():
    proxy = "http://username:password@p.webshare.io:80"
    formatted = format_sb_proxy(proxy)
    assert formatted == "username:password@p.webshare.io:80"


def test_format_sb_proxy_without_scheme():
    proxy = "username:password@p.webshare.io:80"
    formatted = format_sb_proxy(proxy)
    assert formatted == "username:password@p.webshare.io:80"


def test_format_sb_proxy_empty_or_none():
    assert format_sb_proxy(None) is None
    assert format_sb_proxy("") is None
    assert format_sb_proxy("   ") is None


def test_verify_sb_outbound_ip_mock(mocker):
    mock_sb = mocker.Mock()
    mock_sb.get_text.return_value = "198.23.243.226"

    ip = verify_sb_outbound_ip(mock_sb, label="Test")
    assert ip == "198.23.243.226"
    mock_sb.open.assert_called_once_with("https://ipv4.webshare.io/")


def test_to_bool_variations():
    from utils.utils import to_bool
    assert to_bool(True) is True
    assert to_bool("true") is True
    assert to_bool("True") is True
    assert to_bool("1") is True
    assert to_bool(1) is True
    assert to_bool("yes") is True
    assert to_bool("on") is True
    assert to_bool("t") is True

    assert to_bool(False) is False
    assert to_bool("false") is False
    assert to_bool("False") is False
    assert to_bool("0") is False
    assert to_bool(0) is False
    assert to_bool(None) is False
    assert to_bool("") is False


@pytest.mark.asyncio
async def test_fetch_pipeline_config_proxy_from_extraction_params(monkeypatch):
    from utils.utils import fetch_pipeline_config
    monkeypatch.setenv("START_URL", "https://example.com")
    monkeypatch.setenv("DOCUMENT_TYPE", "pdf")
    monkeypatch.setenv("USE_PROXY", "false")
    monkeypatch.setenv("EXTRACTION_PARAMS", '{"use_proxy": "true"}')

    config = await fetch_pipeline_config("test_proxy_pipeline")
    assert config["use_proxy"] is True

