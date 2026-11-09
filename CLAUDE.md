# DeltaBound — project context

## What this is
DeltaBound is an open-source tool that helps teams migrate legacy ETL (SAP Data Services /
BODS first) to Databricks. It parses a legacy tool's export files into one open,
tool-neutral graph format (the IR), then answers: what does each job run, what is dead
code, where does a column come from, how complex is each job, and in what order should
jobs be migrated. Later phases add conversion to PySpark/Databricks Workflows and
step-level data validation.

Python package and CLI name: `deltabound`.

## Architecture (keep these boundaries)
- `deltabound/ir.py` — Pydantic models for the IR. The core asset. Every change must be
  backward compatible or bump IR_VERSION, and must stay tool-neutral (no BODS-only fields;
  use generic names and keep tool-specific details in optional fields).
- `deltabound/parsers/<tool>.py` — one parser per legacy tool. Parsers only read export
  files and produce an `Estate`. They never connect to a live system.
- `deltabound/graph.py` — analysis over the IR only. Never imports a parser.
- `deltabound/report.py` — markdown estate report; reads the IR and graph.py only.
- `deltabound/explain/` — AI job explanations. Reads the IR only. Anthropic is the only
  provider (decided 2026-11-09); `fake` is for tests and offline demos. Bump
  `prompts.PROMPT_VERSION` whenever a prompt or schema changes (it is part of the cache key).
  Tests must never call the network.
- `deltabound/cli.py` — thin command-line layer over graph, report and explain functions.
- Next: `viewer/` (Vite + React + React Flow; runs the Python parser in the browser via
  Pyodide, approved 2026-11-09). Later: `convert/`, `validate/`. SSIS is deferred. Design
  docs for all of these are in `docs/design/`.

## Rules
- Unknown or unsupported elements become `opaque` transforms and are listed in
  `Estate.unparsed`. Never silently drop anything; coverage must stay measurable.
- Every feature gets pytest tests using files in `samples/`. Run `pytest -q` before
  declaring a task done.
- Samples are SYNTHETIC only (fictional companies). Never add real client or employer
  data, names, table names or logic.
- The BODS tag names in `parsers/bods.py` are unverified against real exports. Keep all
  tag names in the TAG_* / TRANSFORM_KINDS constants so they are easy to correct.
- Python 3.10+, type hints everywhere, small pure functions, minimal dependencies
  (pydantic, networkx; optional extra `explain` = anthropic; ask before adding others).
- Keep the README quick start working after every change.

## Current state (updated 2026-11-09)
- IR_VERSION 0.2.0. `samples/ir/estate_v0.1.0.json` must keep loading; when bumping the IR
  again, add an export from the previous version there.
- `samples/bods/` is the multi-file Fernhill Retail estate (15 jobs, 30 workflows, 60
  dataflows). `tests/test_bods_estate.py` asserts its exact counts, dead code and unparsed
  items, so update tests when changing the sample. `samples/bods_mini/` is the original
  small sample.
- Environment: Python 3.12 from Homebrew in `.venv` (system python3 is 3.9, too old).
- License: Business Source License 1.1, Licensor Kishore Tenneti, Change Date 2030-11-09,
  Change License Apache-2.0. Production use needs the USD 25K/yr commercial license
  (COMMERCIAL.md). Keep the BSL text unmodified apart from the Parameters block.

## How to work with me
- Before coding a non-trivial task, give a short plan and wait for approval.
- Work in small steps; one feature per commit with a clear message.
- If something about BODS behavior is uncertain, say so and write the assumption in a
  code comment rather than guessing silently.
- At the end of a session, update README.md and this file with anything that changed.
