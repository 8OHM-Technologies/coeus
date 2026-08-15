#!/usr/bin/env python3
"""
Coeus Data Analysis Tool (DuckDB Powered)
=========================================
Connects DuckDB directly to the PostgreSQL database (`coeus`) using the DuckDB PostgreSQL scanner.
Allows high-performance analytical queries, JSON data extraction, text searches, aggregations,
and exporting results to CSV, Parquet, or JSON.

Usage Examples:
    # Summary of record types and statuses:
    python scripts/analyze_duckdb.py --summary

    # Search for "ORDER" in first 2000 characters of detailed saflii_courts records:
    python scripts/analyze_duckdb.py --record-type saflii_courts --status detailed --search-heading "ORDER" --char-limit 2000

    # Run custom DuckDB SQL query:
    python scripts/analyze_duckdb.py --query "SELECT target_name, count(*) FROM coeus.extracted_records er JOIN coeus.targets t ON er.target_id = t.id WHERE er.record_type = 'saflii_courts' AND er.status = 'detailed' GROUP BY target_name ORDER BY 2 DESC"

    # Start interactive REPL mode:
    python scripts/analyze_duckdb.py --interactive

    # Export analysis results to CSV:
    python scripts/analyze_duckdb.py --query "..." --export output.csv
"""

import argparse
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

import duckdb

# Load environment variables from .env
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
else:
    load_dotenv(override=True)


def clean_env(value: str | None) -> str:
    """Cleans surrounding quotes and spaces from env values."""
    if not value:
        return ""
    val = value.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    return val.strip()


def get_duckdb_connection() -> duckdb.DuckDBPyConnection:
    """
    Initializes a DuckDB connection and attaches PostgreSQL database coeus.
    """
    host = clean_env(os.environ.get("POSTGRES_HOST")) or "localhost"
    port = clean_env(os.environ.get("POSTGRES_PORT")) or "5432"
    user = clean_env(os.environ.get("POSTGRES_USER")) or "postgres"
    password = clean_env(os.environ.get("POSTGRES_PASSWORD"))
    dbname = clean_env(os.environ.get("POSTGRES_DB")) or "coeus"

    if not password:
        print("[ERROR] POSTGRES_PASSWORD is not set in environment or .env file.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting DuckDB to PostgreSQL ({host}:{port}/{dbname})...")

    con = duckdb.connect()
    try:
        con.sql("INSTALL postgres; LOAD postgres;")
    except Exception as e:
        print(f"[ERROR] Failed to install/load DuckDB postgres extension: {e}", file=sys.stderr)
        sys.exit(1)

    conn_str = f"host={host} port={port} dbname={dbname} user={user} password={password}"
    attach_sql = f"ATTACH '{conn_str}' AS coeus (TYPE POSTGRES, READ_ONLY TRUE);"

    try:
        con.sql(attach_sql)
        print("Successfully attached database 'coeus' as READ_ONLY.\n")
    except Exception as e:
        print(f"[ERROR] Could not attach PostgreSQL database: {e}", file=sys.stderr)
        sys.exit(1)

    return con


def print_relation(rel):
    """Prints DuckDB relation directly or as formatted text table."""
    if rel is None:
        print("No results.")
        return
    # Use DuckDB's built-in print renderer
    rel.show()


def run_summary(con: duckdb.DuckDBPyConnection):
    """Prints a high-level summary of records in the database."""
    print("=== RECORD TYPE & STATUS BREAKDOWN ===")
    query = """
    SELECT 
        record_type, 
        status, 
        COUNT(*) AS record_count
    FROM coeus.extracted_records
    GROUP BY record_type, status
    ORDER BY record_type, status;
    """
    rel = con.sql(query)
    print_relation(rel)

    print("\n=== TOP TARGETS BY RECORD COUNT ===")
    query_targets = """
    SELECT 
        e.name AS entity_name,
        t.target_name,
        er.record_type,
        er.status,
        COUNT(er.id) AS record_count
    FROM coeus.extracted_records er
    LEFT JOIN coeus.targets t ON er.target_id = t.id
    LEFT JOIN coeus.entities e ON t.entity_id = e.id
    GROUP BY e.name, t.target_name, er.record_type, er.status
    ORDER BY record_count DESC
    LIMIT 20;
    """
    rel_targets = con.sql(query_targets)
    print_relation(rel_targets)


def run_heading_search(
    con: duckdb.DuckDBPyConnection,
    record_type: str,
    status: str,
    heading: str,
    char_limit: int,
    exclude_rolls: bool = True,
    from_bottom: bool = False,
):
    """Searches for heading/word occurrences in full_text (from top or bottom of document)."""
    func_name = "RIGHT" if from_bottom else "LEFT"
    direction_str = "last" if from_bottom else "first"

    print(f"=== HEADING SEARCH: '{heading}' in {direction_str} {char_limit} chars ===")
    print(f"Filters: record_type='{record_type}', status='{status}', exclude_rolls={exclude_rolls}, from_bottom={from_bottom}")

    rolls_filter = "AND t.target_name NOT LIKE '%Rolls%'" if exclude_rolls else ""

    query = f"""
    SELECT 
        COUNT(*) as total_matched_targets_records,
        COUNT(CASE WHEN regexp_matches({func_name}(data->>'full_text', {char_limit}), '\\b{heading}\\b') THEN 1 END) as exact_word_heading_count,
        COUNT(CASE WHEN regexp_matches({func_name}(data->>'full_text', {char_limit}), '(?m)^\\\\s*{heading}') THEN 1 END) as line_start_heading_count,
        COUNT(CASE WHEN {func_name}(data->>'full_text', {char_limit}) LIKE '%{heading}%' THEN 1 END) as uppercase_substring_count,
        COUNT(CASE WHEN {func_name}(data->>'full_text', {char_limit}) ILIKE '%{heading}%' THEN 1 END) as case_insensitive_count
    FROM coeus.extracted_records er
    JOIN coeus.targets t ON er.target_id = t.id
    WHERE er.record_type = '{record_type}' 
      AND er.status = '{status}'
      {rolls_filter};
    """

    rel = con.sql(query)
    print_relation(rel)


def run_interactive_repl(con: duckdb.DuckDBPyConnection):
    """Starts an interactive SQL REPL for DuckDB."""
    print("=== DUCKDB INTERACTIVE REPL ===")
    print("Type your DuckDB SQL queries against table 'coeus.extracted_records', 'coeus.targets', etc.")
    print("Type 'exit' or 'quit' to end.\n")

    while True:
        try:
            cmd = input("duckdb> ").strip()
            if not cmd:
                continue
            if cmd.lower() in ("exit", "quit", "q"):
                print("Exiting REPL.")
                break
            res = con.sql(cmd)
            if res is not None:
                print_relation(res)
                print()
        except KeyboardInterrupt:
            print("\nExiting REPL.")
            break
        except Exception as e:
            print(f"[SQL ERROR] {e}\n")


def main():
    parser = argparse.ArgumentParser(description="Coeus DuckDB Analytics CLI")
    parser.add_argument("--summary", action="store_true", help="Print overall database breakdown summary")
    parser.add_argument("--query", "-q", type=str, help="Execute custom DuckDB SQL query")
    parser.add_argument("--search-heading", type=str, help="Search for a specific heading keyword in full_text")
    parser.add_argument("--char-limit", type=int, default=2000, help="Character limit for heading search window (default: 2000)")
    parser.add_argument("--from-bottom", action="store_true", help="Search within the last N characters (from bottom of document) instead of top")
    parser.add_argument("--record-type", type=str, default="saflii_courts", help="Filter by record_type (default: saflii_courts)")
    parser.add_argument("--status", type=str, default="detailed", help="Filter by status (default: detailed)")
    parser.add_argument("--include-rolls", action="store_true", help="Include target_names containing 'Rolls'")
    parser.add_argument("--export", type=str, help="Export query result to file path (.csv, .json, .parquet)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Start interactive DuckDB SQL REPL")

    args = parser.parse_args()

    con = get_duckdb_connection()

    if args.summary:
        run_summary(con)
    elif args.search_heading:
        run_heading_search(
            con,
            record_type=args.record_type,
            status=args.status,
            heading=args.search_heading,
            char_limit=args.char_limit,
            exclude_rolls=not args.include_rolls,
            from_bottom=args.from_bottom,
        )
    elif args.query:
        print(f"Executing query:\n{args.query}\n")
        res = con.sql(args.query)
        if args.export:
            export_path = args.export
            if export_path.endswith(".csv"):
                con.sql(f"COPY ({args.query}) TO '{export_path}' (HEADER, DELIMITER ',');")
            elif export_path.endswith(".parquet"):
                con.sql(f"COPY ({args.query}) TO '{export_path}' (FORMAT PARQUET);")
            elif export_path.endswith(".json"):
                con.sql(f"COPY ({args.query}) TO '{export_path}' (FORMAT JSON);")
            else:
                con.sql(f"COPY ({args.query}) TO '{export_path}';")
            print(f"Successfully exported results to {export_path}")
        else:
            print_relation(res)
    elif args.interactive:
        run_interactive_repl(con)
    else:
        # Default behavior if no flags provided
        parser.print_help()


if __name__ == "__main__":
    main()
