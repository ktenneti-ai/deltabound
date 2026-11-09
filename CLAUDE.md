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
- `deltabound/bridge.py` — viewer entry points (format detection, load, analysis, lineage).
  Like cli.py it may import parsers. Works on bytes, returns JSON-ready dicts.
- `viewer/` — static web viewer (Vite, React 19, React Flow, dagre, Pyodide 314.0.7). It runs
  deltabound itself in a Web Worker; no lineage or parsing logic is re-implemented in
  TypeScript (only edge traversal). `npm run prepare:py` builds the wheels and copies the
  Pyodide runtime into `public/` (gitignored); everything is self-hosted, no CDN at run time.
  When the IR changes: `cd viewer && npm run gen:types`, and regenerate the fixtures with
  `.venv/bin/python viewer/scripts/gen_fixtures.py` (pytest fails until both are current).
- `deltabound/convert/` — IR -> Databricks notebooks (PySpark + Spark SQL over temp views)
  and an Asset Bundle. Reads the IR only. Rule for every transform kind it converts; anything
  else becomes a TODO cell that raises NotImplementedError (never a pass-through guess).
  Decided 2026-11-09: Delta identity surrogate keys, SCD2 defaults VALID_FROM/VALID_TO/
  CURRENT_FLAG='Y'/9999-12-31 when the export names none, serverless compute.
  Golden output in tests/golden/convert (refresh: DELTABOUND_UPDATE_GOLDEN=1, review the diff).
  tests/test_convert_spark.py runs generated notebooks on local Spark (marker `spark`).
- Next: `validate/`. SSIS is deferred. Design docs are in `docs/design/`.

## Rules
- Unknown or unsupported elements become `opaque` transforms and are listed in
  `Estate.unparsed`. Never silently drop anything; coverage must stay measurable.
- Every feature gets pytest tests using files in `samples/`. Run `pytest -q` before
  declaring a task done; for viewer changes also `cd viewer && npm test && npm run build`,
  and check the change in a real browser.
- Samples are SYNTHETIC only (fictional companies). Never add real client or employer
  data, names, table names or logic.
- The BODS tag names in `parsers/bods.py` are unverified against real exports. Keep all
  tag names in the TAG_* / TRANSFORM_KINDS constants so they are easy to correct.
- Python 3.10+, type hints everywhere, small pure functions, minimal dependencies
  (pydantic, networkx; optional extra `explain` = anthropic; ask before adding others).
- Keep the README quick start working after every change.

## Current state (updated 2026-11-09)
- IR_VERSION 0.2.1. `samples/ir/estate_v0.1.0.json` and `estate_v0.2.0.json` must keep loading;
  when bumping the IR again, add an export from the previous version there.
- Transform names are unique within a dataflow (the parser renames a clashing target to
  `<TABLE>_TARGET`); code may rely on that.
- `samples/bods/` is the multi-file Fernhill Retail estate (15 jobs, 30 workflows, 60
  dataflows). `tests/test_bods_estate.py` asserts its exact counts, dead code and unparsed
  items, so update tests when changing the sample. `samples/bods_mini/` is the original
  small sample.
- Environment: Python 3.12 from Homebrew in `.venv` (system python3 is 3.9, too old).
  Spark tests use Homebrew openjdk (found automatically; Java 25 works with Spark 4.2).
- License: Business Source License 1.1, Licensor Kishore Tenneti, Change Date 2030-11-09,
  Change License Apache-2.0. Production use needs the USD 25K/yr commercial license
  (COMMERCIAL.md). Keep the BSL text unmodified apart from the Parameters block.

## How to work with me
- Before coding a non-trivial task, give a short plan and wait for approval.
- Work in small steps; one feature per commit with a clear message.
- If something about BODS behavior is uncertain, say so and write the assumption in a
  code comment rather than guessing silently.
- At the end of a session, update README.md and this file with anything that changed.
