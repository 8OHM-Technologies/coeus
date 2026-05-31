### ADR 003: Configuration-Driven Pipeline Orchestration
**Date**: April 2026
**Status**: Accepted

**Context**:
The business goal is to map the entire global mining industry. Writing and maintaining a bespoke Python script for every individual data source (hundreds of exchanges and company sites) is an unscalable anti-pattern that leads to high technical debt.

**Decision**:
We will implement a Configuration-Driven Dynamic DAG architecture using Apache Airflow. The core scraping and LLM extraction logic will be written generically. Pipeline definitions (target URLs, CSS selectors, LLM prompt templates) will be stored as JSON objects in a PostgreSQL configurations table. Airflow will dynamically generate the DAGs at runtime by querying this database.

**Consequences**:

**Pros**:

Extreme Scalability: New data sources can be added by analysts or non-engineers via a UI without requiring a PR or code deployment.

Standardization: Every pipeline follows the exact same execution path (Ingest -> Extract -> Load), ensuring uniform logging, error handling, and alerting.

Idempotency: By decoupling the configuration from the execution, we can replay failed jobs easily with updated CSS selectors if a target website changes its layout.

**Cons**:

Initial Complexity: Building the "Factory" that generates dynamic DAGs is more complex upfront than writing a single static Airflow DAG.

Debugging: When a dynamic pipeline fails, tracing the error requires checking both the Airflow logs and the JSON configuration state at the time of failure.
