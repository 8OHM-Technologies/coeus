import sys
import os
import pytest
from unittest.mock import AsyncMock

# Ensure extraction_workers is importable
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../extraction_workers")))

from db import get_db_connection, get_db_pool


@pytest.mark.asyncio
async def test_get_db_connection(monkeypatch, mocker):
    monkeypatch.setenv("POSTGRES_HOST", "test-db-host")
    monkeypatch.setenv("POSTGRES_USER", "test-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("POSTGRES_DB", "test-database")

    # Mock running inside Docker
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("socket.gethostbyname", return_value="127.0.0.1")

    # Mock asyncpg.connect
    mock_connect = mocker.patch("asyncpg.connect", new_callable=AsyncMock)

    conn = await get_db_connection()

    mock_connect.assert_called_once_with(
        host="test-db-host",
        user="test-user",
        password="test-password",
        database="test-database",
        port=5432,
        command_timeout=60.0,
    )
    assert conn is not None


@pytest.mark.asyncio
async def test_get_db_connection_missing_vars(monkeypatch, mocker):
    # Clear environment variables
    monkeypatch.delenv("POSTGRES_USER", raising=False)

    with pytest.raises(ValueError) as excinfo:
        await get_db_connection()

    assert "Database environment variables are not fully set." in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_db_pool(monkeypatch, mocker):
    monkeypatch.setenv("POSTGRES_HOST", "test-db-host")
    monkeypatch.setenv("POSTGRES_USER", "test-user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "test-password")
    monkeypatch.setenv("POSTGRES_DB", "test-database")

    # Mock running inside Docker
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("socket.gethostbyname", return_value="127.0.0.1")

    # Mock asyncpg.create_pool
    mock_create_pool = mocker.patch("asyncpg.create_pool", new_callable=AsyncMock)

    pool = await get_db_pool()

    mock_create_pool.assert_called_once_with(
        host="test-db-host",
        user="test-user",
        password="test-password",
        database="test-database",
        port=5432,
        command_timeout=60.0,
    )
    assert pool is not None


