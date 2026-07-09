import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def get_db_connection() -> asyncpg.Connection:
    """
    Establishes an asynchronous connection to the PostgreSQL instance via the proxy.
    """
    db_host = os.environ.get("POSTGRES_HOST")
    db_user = os.environ.get("POSTGRES_USER")
    db_pass = os.environ.get("POSTGRES_PASSWORD")
    db_name = os.environ.get("POSTGRES_DB")

    if not all([db_host, db_user, db_pass, db_name]):
        raise ValueError("Database environment variables are not fully set.")

    return await asyncpg.connect(
        host=db_host, user=db_user, password=db_pass, database=db_name, port=5432
    )


async def get_db_pool():
    """
    Creates an asynchronous connection pool to the PostgreSQL instance via the proxy.
    """
    db_host = os.environ.get("POSTGRES_HOST")
    db_user = os.environ.get("POSTGRES_USER")
    db_pass = os.environ.get("POSTGRES_PASSWORD")
    db_name = os.environ.get("POSTGRES_DB")

    return await asyncpg.create_pool(
        host=db_host, user=db_user, password=db_pass, database=db_name, port=5432
    )
