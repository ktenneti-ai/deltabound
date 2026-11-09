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

## What the IR captures (v0.2.0)

- Jobs, workflows and dataflows; calls with their condition and the enclosing
  control construct (`branch`, `loop`, `error_handler`); variables, parameters, scripts.
- Transforms with inputs, tables, column mappings and source columns, WHERE,
  joins (type and condition), GROUP BY, ORDER BY, Case branches, Merge inputs,
  lookups (`lookup_ext()`, `lookup()` and the Lookup transform) and tool-specific
  attributes in `options`.
- Column lineage follows lookups and passes through Table Comparison, History
  Preserving, Key Generation, Case, Merge, Validation and Map Operation; it
  stops at opaque transforms.
- IR 0.1.0 JSON files still load (all new fields have defaults).

## Layout

```
deltabound/
  ir.py             # the open IR (Pydantic models) - the core asset
  parsers/bods.py   # SAP Data Services XML export -> IR
  graph.py          # call tree, dead code, column lineage, call depth, complexity, waves
  report.py         # markdown estate report
  explain/          # AI job explanations with citations (Anthropic), cached
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
2. Model custom functions, Validation rules, Pivot and Data Transfer; weight joins and lookups in complexity.
3. Static web viewer reading `estate.json`, raw BODS XML and `explanations.json` (docs/design/viewer.md).
4. Show explanations in the viewer (it loads `explanations.json`).
5. Rule-based conversion to PySpark and Databricks Asset Bundles (docs/design/convert.md).
6. Step-level data validation and sign-off report (docs/design/validate.md).
7. SSIS parser (deferred; docs/design/ssis.md).

## License

Created by **Kishore Tenneti**. Copyright (c) 2026 Kishore Tenneti.

DeltaBound is licensed under the [Business Source License 1.1](LICENSE):
free for development, testing and evaluation; **production use requires a
commercial license (USD 25,000 per year)**, see [COMMERCIAL.md](COMMERCIAL.md).
Each version converts to Apache-2.0 on its Change Date (four years after release
at the latest). The BSL is source-available, not an OSI-approved open-source license.
