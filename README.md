# DeltaBound

Understand, plan and validate legacy ETL migrations. SAP Data Services (BODS) first.

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

## Quick start

Requires Python 3.10+ (macOS: `brew install python@3.12`).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

deltabound summary  samples/bods
deltabound tree     samples/bods --job JOB_SALES_DAILY
deltabound deadcode samples/bods
deltabound lineage  samples/bods --dataflow DF_FACT_POS_SALES --column DS_DWH.MART.FACT_POS_SALES.AMOUNT
deltabound waves    samples/bods
deltabound report   samples/bods --out estate_report.md
deltabound export   samples/bods --out estate.json
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
    dataflow: DF_CTL_JOB_ERROR   [if on error (ALL)]
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

```bash
cd viewer
npm install
npm run dev        # http://localhost:5173, then "Load demo estate"
npm run build      # static site in viewer/dist/, works offline and from any path
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
deltabound convert samples/bods --job JOB_SALES_DAILY --out build/JOB_SALES_DAILY
deltabound convert samples/bods --all --out build/        # one bundle per job
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
deltabound explain samples/bods --job JOB_SALES_DAILY
deltabound explain samples/bods --all --bundle explanations.json   # one file for the viewer
deltabound explain samples/bods --job JOB_SALES_DAILY --dry-run    # write prompts only, send nothing
DELTABOUND_LLM_PROVIDER=fake deltabound explain samples/bods --job JOB_SALES_DAILY   # offline demo
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
- **Privacy:** explain sends ETL logic (table names, expressions) to Anthropic.
  For client estates, confirm that is allowed first, and use `--dry-run` to
  review exactly what would be sent.

## What the IR captures (v0.2.1)

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
- IR 0.1.0 and 0.2.0 JSON files still load (all new fields have defaults).

## Layout

```
deltabound/
  ir.py             # the open IR (Pydantic models) - the core asset
  parsers/bods.py   # SAP Data Services XML export -> IR
  graph.py          # call tree, dead code, column lineage, call depth, complexity, waves
  report.py         # markdown estate report
  explain/          # AI job explanations with citations (Anthropic), cached
  bridge.py         # entry points for the web viewer (bytes in, JSON out)
  convert/          # IR -> Databricks notebooks + Asset Bundle + conversion report
viewer/             # static web viewer: Vite + React + React Flow + Pyodide
  cli.py            # command line
samples/bods/       # SYNTHETIC estate, fictional Fernhill Retail: 15 jobs, 30 workflows, 60 dataflows
samples/bods_mini/  # SYNTHETIC small estate used by the original tests
samples/ir/         # IR files from older versions (compatibility tests)
tests/
```

## Status and important notes

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
3. Step-level data validation and sign-off report (docs/design/validate.md).
4. SSIS parser (deferred; docs/design/ssis.md).

## License

Created by **Kishore Tenneti**. Copyright (c) 2026 Kishore Tenneti.

DeltaBound is licensed under the [Business Source License 1.1](LICENSE):
free for development, testing and evaluation; **production use requires a
commercial license (USD 25,000 per year)**, see [COMMERCIAL.md](COMMERCIAL.md).
Each version converts to Apache-2.0 on its Change Date (four years after release
at the latest). The BSL is source-available, not an OSI-approved open-source license.
