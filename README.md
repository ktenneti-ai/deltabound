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

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q

deltabound summary  samples/bods
deltabound tree     samples/bods --job JOB_SALES_DAILY
deltabound deadcode samples/bods
deltabound lineage  samples/bods --dataflow DF_STG_ORDERS_DELTA --column DS_DWH.STG.STG_ORDERS.NET_AMOUNT
deltabound waves    samples/bods
deltabound export   samples/bods --out estate.json
```

Example output of `tree`:

```
job: JOB_SALES_DAILY
  workflow: WF_STAGE_SALES
    dataflow: DF_STG_ORDERS_FULL   [if $G_FULL_LOAD = 'Y']
    dataflow: DF_STG_ORDERS_DELTA   [if NOT ($G_FULL_LOAD = 'Y')]
    dataflow: DF_STG_ORDER_ITEMS
  workflow: WF_LOAD_MART
    workflow: WF_DIMENSIONS
      dataflow: DF_DIM_CUSTOMER
      dataflow: DF_DIM_PRODUCT
    dataflow: DF_FACT_SALES
```

## Layout

```
deltabound/
  ir.py             # the open IR (Pydantic models) - the core asset
  parsers/bods.py   # SAP Data Services XML export -> IR
  graph.py          # call tree, dead code, column lineage, complexity, waves
  cli.py            # command line
samples/bods/       # SYNTHETIC sample estate (fictional retail company)
tests/
```

## Status and important notes

- **The BODS parser is unverified against real exports.** Tag names follow the
  general shape of BODS XML exports but must be checked against real files and
  the `TAG_*` constants adjusted. Unknown elements become `opaque` transforms
  and are reported by `summary`, so coverage is always measurable.
- **Never commit client or employer exports.** Use synthetic samples, files
  from design partners with written permission, or SAP trial systems whose
  license allows it.

## Next steps

1. Validate the parser against a real (permitted) BODS export; expand transform coverage.
2. Parse Query join conditions, Case/Merge branches, Lookup functions and custom functions.
3. Static web graph viewer reading `estate.json`.
4. AI explainer: per-job plain-English summaries linked to source transforms.
5. SSIS parser to prove the IR is tool-neutral.

## License

Created by **Kishore Tenneti**. Copyright (c) 2026 Kishore Tenneti.

DeltaBound is licensed under the [Business Source License 1.1](LICENSE):
free for development, testing and evaluation; **production use requires a
commercial license (USD 25,000 per year)**, see [COMMERCIAL.md](COMMERCIAL.md).
Each version converts to Apache-2.0 on its Change Date (four years after release
at the latest). The BSL is source-available, not an OSI-approved open-source license.
