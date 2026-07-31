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
