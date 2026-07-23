import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

def clean_env_var(value: str | None) -> str | None:
    """Removes comments and surrounding quotes from environment variables,
    correctly handling quoted strings that may contain hash characters.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return value

    # Check if the string is wrapped in quotes
    if (value.startswith('"') and '"' in value[1:]) or (value.startswith("'") and "'" in value[1:]):
        quote_char = value[0]
        closing_idx = value.find(quote_char, 1)
        if closing_idx != -1:
            return value[1:closing_idx]

    # Otherwise split on the first # to remove comments, only if it is preceded by a space or tab
    # to avoid splitting password characters like '#'
    for sep in (" #", "\t#"):
        if sep in value:
            value = value.split(sep, 1)[0]
            break
    return value.strip().strip("'\"")

async def get_db_connection() -> asyncpg.Connection:
    """
    Establishes an asynchronous connection to the PostgreSQL instance via the proxy.
    """
    db_host = clean_env_var(os.environ.get("POSTGRES_HOST"))
    db_user = clean_env_var(os.environ.get("POSTGRES_USER"))
    db_pass = clean_env_var(os.environ.get("POSTGRES_PASSWORD"))
    db_name = clean_env_var(os.environ.get("POSTGRES_DB"))
    db_port = int(clean_env_var(os.environ.get("POSTGRES_PORT") or "5432"))

    if not all([db_host, db_user, db_pass, db_name]):
        raise ValueError("Database environment variables are not fully set.")

    return await asyncpg.connect(
        host=db_host, user=db_user, password=db_pass, database=db_name, port=db_port
    )


async def get_db_pool():
    """
    Creates an asynchronous connection pool to the PostgreSQL instance via the proxy.
    """
    db_host = clean_env_var(os.environ.get("POSTGRES_HOST"))
    db_user = clean_env_var(os.environ.get("POSTGRES_USER"))
    db_pass = clean_env_var(os.environ.get("POSTGRES_PASSWORD"))
    db_name = clean_env_var(os.environ.get("POSTGRES_DB"))
    db_port = int(clean_env_var(os.environ.get("POSTGRES_PORT") or "5432"))

    return await asyncpg.create_pool(
        host=db_host, user=db_user, password=db_pass, database=db_name, port=db_port
    )
