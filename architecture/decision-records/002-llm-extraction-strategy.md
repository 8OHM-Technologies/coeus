### ADR 002: LLM Extraction and Schema Enforcement Strategy
**Date**: April 2026
**Status**: Accepted

**Context**:
Once technical reports are downloaded and converted to raw text, we must extract structured entities (Asset Name, Classification, Tonnage, Grade, Cut-off parameters). The structure of NI 43-101 footnotes and tables is highly variable, rendering traditional Regex or NLP Named Entity Recognition (NER) models brittle and difficult to maintain.

**Decision**:
We will utilize the OpenAI API (GPT-4o) for the extraction layer, strictly enforcing output schemas using Pydantic and OpenAI's Structured Outputs. Additionally, we will implement an "Agentic Self-Verification" step within the extraction node.

**Consequences**:

**Pros**:

Schema Guarantee: Pydantic ensures the LLM returns exact JSON keys (e.g., tonnage_mt as a float). If the schema is violated, the code raises a standard Python exception that can be routed to a Dead Letter Queue (DLQ) rather than silently corrupting the database.

Nuance Handling: The LLM can interpret domain-specific caveats (e.g., identifying when a resource estimate is "historical" or "unverified" based on footnote context).

Self-Verification: By forcing the LLM to output a data_quality_flags object containing a requires_human_review boolean, we create an automated triage system for the data curation team.

**Cons**:

Cost & Latency: API calls are relatively expensive and introduce network latency.

Hallucination Risk: Mitigation: We counter this via the strict Pydantic schema and by instructing the model to return null if a specific parameter (like NSR cut-off) is not explicitly found in the text.
