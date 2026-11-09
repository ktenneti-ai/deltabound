# DeltaBound

Understand, plan, convert and validate legacy ETL migrations to Databricks. Sources: SAP Data
Services (BODS) and SQL Server Integration Services (SSIS); SQL Server Agent jobs + T-SQL planned.

Legacy ETL estates are hard to migrate because nobody fully understands them:
jobs call workflows that call nested workflows and dataflows, with conditions,
global variables and scripts spread across them. `deltabound` parses the tool's
export files into one open, tool-neutral graph format (the IR) and answers the
questions a migration team asks first:

- What does this job actually run, and under which conditions?
- What is never called and can be skipped?
- Where does this target column come from?
- Which jobs are easiest to migrate first?

Code conversion is out of scope for now. The IR is designed to feed a converter
(Lakebridge, an LLM or a person) and, later, a step-level data validator.

## Demo sources

Each legacy source has its own folder under `demo/`. One `deltabound.toml` in the folder
holds all settings for that source: the source type and export location, the datastore to
Unity Catalog mapping, conversion, AI explanation limits and validation manifests. Every
command accepts the folder, for example `deltabound summary demo/sap_bods`. See
[demo/README.md](demo/README.md).

## Quick start

Requires Python 3.10+ (macOS: `brew install python@3.12`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

deltabound summary  demo/sap_bods
deltabound tree     demo/sap_bods --job JOB_SALES_DAILY
deltabound deadcode demo/sap_bods
deltabound lineage  demo/sap_bods --dataflow DF_FACT_POS_SALES --column DS_DWH.MART.FACT_POS_SALES.AMOUNT
deltabound waves    demo/sap_bods
deltabound report   demo/sap_bods --out estate_report.md
deltabound export   demo/sap_bods --out estate.json
```

Example output of `tree` (conditions come from if/else, while loops and catch blocks):

```
job: JOB_SALES_DAILY
  workflow: WF_SALES_DAILY_MAIN
    workflow: WF_JOB_START
      dataflow: DF_CTL_JOB_START
    workflow: WF_STAGE_SALES
      dataflow: DF_STG_ORDERS_FULL   [if $G_FULL_LOAD = 'Y']
      dataflow: DF_STG_ORDERS_DELTA   [if NOT ($G_FULL_LOAD = 'Y')]
      dataflow: DF_STG_ORDER_ITEMS
      dataflow: DF_STG_INVOICES
    workflow: WF_LOAD_SALES_MART
      workflow: WF_SHARED_DIMENSIONS
        workflow: WF_DIM_CUSTOMER_SCD
          dataflow: DF_STG_CUSTOMER
          dataflow: DF_DIM_CUSTOMER
        dataflow: DF_DIM_PRODUCT
        dataflow: DF_DIM_STORE
        dataflow: DF_DIM_DATE   [if day_in_month($G_LOAD_DATE) = 1]
      workflow: WF_SALES_FACTS
        dataflow: DF_FACT_SALES
        dataflow: DF_FACT_INVOICE
        dataflow: DF_AGG_SALES_DAILY   [if $G_REGION = 'ALL']
    dataflow: DF_CTL_JOB_ERROR   [on error (ALL)]
    workflow: WF_JOB_END
      dataflow: DF_CTL_JOB_END
```

`report` writes one markdown file with object counts, parser coverage (every
unparsed item listed), dead code and missing references, the 5 most complex
jobs and the migration waves.

## Web viewer

A static web app in `viewer/` (no back end). Open BODS XML exports directly,
or `estate.json`, `explanations.json`, a folder, or a `.zip` of any of these.
It runs the same Python parser and analysis as the CLI inside the browser
(Pyodide), so files never leave the machine.

**Hosted:** https://ktenneti-ai.github.io/deltabound/ (GitHub Pages, rebuilt by
`.github/workflows/pages.yml` on every push to `main` that touches the viewer or the package).
It is the same static app: imported files are read in your browser and never uploaded.

```bash
deltabound viewer demo/sap_bods    # opens http://127.0.0.1:8765 with that source preloaded
deltabound viewer                  # empty viewer: use "Import files" / "Import folder"
```

`deltabound viewer` serves the built app on this machine only (127.0.0.1). It shares only the
files of the source you name, and the analyzer runs in the browser with no API calls. Use
**Import files** or **Import folder** in the header to load XML, JSON, `.dtsx` or `.zip` exports
at any time. Build the app once first:

```bash
cd viewer
npm install
npm run build      # static site in viewer/dist/ (used by `deltabound viewer`)
npm run dev        # development server on http://localhost:5173
npm test           # Vitest; Python-side tests run with pytest
```

- **Requires** Node 20+ and the project `.venv`. The build packs deltabound
  into a wheel; set `PYTHON` to use another interpreter.
- **Screens:**
  - an overview with coverage and waves
  - expandable job and workflow call trees with if, loop and error-handler
    conditions
  - per-dataflow transform graphs: click a column to trace where it comes
    from and where it goes
  - dead code and missing references
  - a load report for every file
  - job explanations with citation links, when `explanations.json` is loaded
- **Size and speed:** the first visit downloads about 20 MB of Python runtime,
  cached afterwards. Startup takes about 2 seconds, and loading the 60-dataflow
  sample takes about 0.3 seconds.

## Conversion to Databricks

`deltabound convert` turns a job into a Databricks Asset Bundle:
- one notebook per dataflow, written in PySpark with Spark SQL
- the job definition with its control flow
- a setup notebook that creates the target tables
- a conversion report

```bash
deltabound convert demo/sap_bods --job JOB_SALES_DAILY --out build/JOB_SALES_DAILY
deltabound convert demo/sap_bods --all --out build/        # one bundle per job
cd build/JOB_SALES_DAILY && databricks bundle deploy      # after reviewing conversion_report.md
```

- **Rules:**
  - Query: joins, WHERE, GROUP BY, ORDER BY into file targets, typed output
    columns.
  - Lookups (`lookup_ext`, `lookup`, the Lookup transform): deduplicated left
    joins with the legacy default value.
  - Case: first-match branches; NULL conditions count as false.
  - Merge: union by column name.
  - Table Comparison, History Preserving and Key Generation chains: Delta
    `MERGE` for SCD type 1 and 2, with an identity surrogate key.
  - Sources and targets.
  - BODS expressions are translated function by function, and variables become
    Spark session variables.
- **No guessing:** anything without a rule is marked
  `TODO(deltabound)`. Notebook steps raise `NotImplementedError`, so a
  half-converted pipeline fails loudly.
- **Control flow:**
  - if/else becomes `condition_task`s.
  - Retry loops become `max_retries`; other loops are flagged as TODOs.
  - Catch blocks run with `run_if: AT_LEAST_ONE_FAILED`.
  - Scripts become notebooks that pass variables on as task values.
- **Settings:** optional, in `deltabound.toml`:

  ```toml
  [convert]
  compute = "serverless"              # or "job_cluster"
  [convert.datastores]                # legacy datastore(.owner) -> Unity Catalog catalog.schema
  "DS_ERP.SAPSR3" = "bronze.sap_erp"
  "DS_DWH" = "main.dwh"
  [convert.scd]                       # used when the export does not name SCD2 columns
  current_flag = "CURRENT_FLAG"
  current_value = "Y"
  ```

On the sample estate, 205 of 226 transforms convert (91%). The rest are file
I/O, Validation, Map Operation, unmodeled transforms and one lookup whose table
is only known at run time. The tests run the generated notebooks on local Spark
with Delta: `pip install -e ".[spark]"`, Java 17+, then
`pytest -m spark`. `pytest -m "not spark"` skips them.

## Validation and sign-off

`deltabound validate` compares the legacy job's saved outputs with the converted
pipeline's outputs, step by step and in call order. It then writes a sign-off
report.

```bash
pip install -e ".[validate]"                               # DuckDB for local runs
deltabound validate scaffold demo/sap_bods --job JOB_SALES_DAILY --out validation.toml
deltabound validate run demo/sap_bods/validation/JOB_SALES_DAILY/validation.toml \
    --waivers demo/sap_bods/validation/JOB_SALES_DAILY/waivers.toml --out validation_out
# -> validation_out/report.html, report.md, result.json; exit code 1 when a step fails
```

- **Checks per step:**
  - Schema: missing or extra columns, and types and decimal scale against the IR.
  - Row counts.
  - Keys: duplicates, and rows found on only one side.
  - Values on matched keys: decimals within 0.005 by default, integers exact,
    strings trimmed, `''` treated as NULL.
  - Without keys, whole rows are compared as multisets.
- **What the scaffold guesses from the IR:**
  - Business keys, taken from Table Comparison keys.
  - Columns to ignore: surrogate keys (including ones looked up from dimensions)
    and SCD2 date columns.
  - A "current rows only" filter for SCD2 tables.
- **Root causes:** a failing step that reads an earlier failing step's table is
  marked *cascading*, and the report points at the first failing step. Column
  differences come with lineage hints showing where the column is computed.
- **Waivers:** each has a reason, an approver and an expiry date. Expired
  waivers are listed but not applied.
- **Report:** inputs are fingerprinted (file SHA-256 or Delta table version),
  and the report ends with a sign-off block.
- **Engines:** `--engine duckdb` reads CSV and Parquet files. `--engine spark`
  also reads tables, for use on Databricks. Both run the same SQL, and a test
  checks that their results are identical.

## Job explanations (AI)

`deltabound explain` writes a plain-English summary of a job (purpose, inputs,
outputs, business rules) using Claude. Every claim cites the IR element it came
from, such as `df:DF_FACT_SALES/Q_JOIN` or `call:WF_STAGE_SALES#1`. A claim with
a citation that does not exist in the job is retried once and then dropped, never
shown. Input and output tables come from the IR, not the model, and any the model
leaves out are added back and marked `[from IR]`.

```bash
pip install -e ".[explain]"
export ANTHROPIC_API_KEY=...            # or: ant auth login
deltabound explain demo/sap_bods --job JOB_SALES_DAILY --plan       # what would be sent, worst-case cost; sends nothing
deltabound explain demo/sap_bods --job JOB_SALES_DAILY              # asks you to type APPROVE first
deltabound explain demo/sap_bods --all --bundle explanations.json   # one file for the viewer
deltabound explain demo/sap_bods --job JOB_SALES_DAILY --dry-run    # write prompts only, send nothing
DELTABOUND_LLM_PROVIDER=fake deltabound explain demo/sap_bods --job JOB_SALES_DAILY   # offline demo
```

- **How it runs:** each dataflow is summarised once, then the job summary is
  built from those notes and the call tree.
- **Cache:** results are cached in `.deltabound/explain/` and reused until the IR,
  the model or the prompt changes. `--refresh` forces a new run.
- **Settings:** optional, in `deltabound.toml`:

  ```toml
  [explain]
  model = "claude-opus-5-5"   # default
  effort = "high"             # low | medium | high | xhigh | max
  fallbacks = true            # if the model declines, the API retries on a fallback model
  redact_literals = false     # true replaces string/number literals before sending
  ```

  `DELTABOUND_LLM_MODEL` and `DELTABOUND_LLM_PROVIDER` override the file.
- **Governed API use:** no request is sent without a person approving a priced plan
  (type `APPROVE`, or `--approve <token>` in automation). Hard caps in
  `[explain.limits]` are enforced on every call, and every approval, call and refusal
  is written to an audit log. `--plan` shows the plan and sends nothing. See
  [docs/governance.md](docs/governance.md).
- **Privacy:** explain sends ETL logic (table names, expressions) to Anthropic.
  For client estates, confirm that is allowed first, and use `--dry-run` to
  review exactly what would be sent.

## What the IR captures (v0.3.0)

- Jobs, workflows and dataflows; calls with their condition and the enclosing
  control construct (`branch`, `loop`, `error_handler`); variables, parameters, scripts.
- Transforms with inputs, tables, column mappings and source columns, WHERE,
  joins (type and condition), GROUP BY, ORDER BY, Case branches, Merge inputs,
  lookups (`lookup_ext()`, `lookup()` and the Lookup transform) and tool-specific
  attributes in `options`.
- Column lineage follows lookups and passes through Table Comparison, History
  Preserving, Key Generation, Case, Merge, Validation and Map Operation; it
  stops at opaque transforms.
- Lookup defaults, return policy and cache hint; Table Comparison key and compare
  columns; SCD2 column settings; generated key columns; where each script sits
  among the calls (0.2.1).
- SSIS (0.3.0): task objects (Execute SQL, Script, ...), precedence constraints
  (`depends_on`, `trigger`), variable expressions, connection managers as datastores
  (secrets removed), SQL-command sources, and transforms that pass every input column on.
- IR 0.1.0, 0.2.0 and 0.2.1 JSON files still load (all new fields have defaults).

## Layout

```
deltabound/
  ir.py             # the open IR (Pydantic models) - the core asset
  parsers/bods.py   # SAP Data Services XML export -> IR
  parsers/ssis.py   # SSIS .dtsx packages -> IR
  graph.py          # call tree, dead code, column lineage, call depth, complexity, waves
  report.py         # markdown estate report
  explain/          # AI job explanations with citations (Anthropic), cached
  bridge.py         # entry points for the web viewer (bytes in, JSON out)
  serve.py          # `deltabound viewer`: local-only server for the built viewer
  convert/          # IR -> Databricks notebooks + Asset Bundle + conversion report
  validate/         # step-level legacy vs Databricks comparison + sign-off report
viewer/             # static web viewer: Vite + React + React Flow + Pyodide
  cli.py            # command line
demo/               # one folder per legacy source, each with a deltabound.toml (see demo/README.md)
  sap_bods/         # SYNTHETIC Fernhill Retail: exports/ (15 jobs, 30 workflows, 60 dataflows), validation/
  ssis/             # SYNTHETIC Halden Outfitters: 4 SSIS packages
  sql_server/       # planned: SQL Server Agent jobs + T-SQL
samples/bods_mini/  # SYNTHETIC small estate used by the original tests
samples/ir/         # IR files from older versions (compatibility tests)
tests/
```

## Status and important notes

- **The SSIS parser is unverified against real packages** in the same way: its element names
  and component class IDs are constants in `parsers/ssis.py`. `convert` handles SAP Data
  Services only and refuses SSIS rather than mistranslate its expressions.
- **The BODS parser is unverified against real exports.** Tag names follow the
  general shape of BODS XML exports but must be checked against real files and
  the `TAG_*` constants adjusted. This includes the assumed layouts of joins,
  GROUP BY/ORDER BY, Case branches, try/catch and lookup function arguments.
  Unknown elements become `opaque` transforms and are reported by `summary`
  and `report`, so coverage is always measurable.
- **Never commit client or employer exports.** Use synthetic samples, files
  from design partners with written permission, or SAP trial systems whose
  license allows it.

## Next steps

1. Validate the parser against a real (permitted) BODS export; expand transform coverage.
2. Model custom functions, Validation rules, Pivot and Data Transfer; weight joins and lookups in complexity;
   conversion rules for file sources/targets, Validation and Map Operation.
3. Conversion rules for SSIS (expression translator for the SSIS expression language).
4. SQL Server Agent jobs + T-SQL source (demo/sql_server is laid out).

## License

Created by **Kishore Tenneti**. Copyright (c) 2026 Kishore Tenneti.

DeltaBound is licensed under the [Business Source License 1.1](LICENSE):
free for development, testing and evaluation; **production use requires a
commercial license (USD 25,000 per year)**, see [COMMERCIAL.md](COMMERCIAL.md).
Each version converts to Apache-2.0 on its Change Date (four years after release
at the latest). The BSL is source-available, not an OSI-approved open-source license.
