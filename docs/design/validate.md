# Design: step-level data validation (`deltabound/validate/`)

Status: IMPLEMENTED 2026-11-09 (milestones 1-4, Spark engine of 5). Decisions and departures:
- Decided: DuckDB as optional extra; exact integers, decimals within 0.005; surrogate keys ignored
  when the IR shows Key Generation (also when looked up from a dimension); sign-off in the report.
- Manifest and waivers are TOML, not YAML: read by the standard library, no new dependency.
- Column profiles for keyless steps were not built; keyless steps use the exact multiset check.
- Not built yet: the Databricks notebook wrapper (use the Python API with SparkEngine there).
- Numbers are compared after widening both sides to DECIMAL(38,9)/DOUBLE: casting legacy values
  to the new column type first would round real differences away.
- CSV readers return NULL for empty fields, so '' vs NULL cannot be distinguished for CSV inputs.

## 1. Goal

Prove that a converted Databricks pipeline produces the same data as the
legacy job, **step by step**, and produce a sign-off report a data owner can
approve.

- **Inputs:**
  - saved legacy outputs (CSV or Parquet) for each dataflow step
  - the matching outputs of the converted pipeline
  - the IR, which provides the step order, schemas, keys and lineage
- **Checks:** schema, row counts, keys, and value differences with tolerances.
- **Output:**
  - a machine-readable result (JSON)
  - a human sign-off report (Markdown, plus a static HTML page)
  - an audit trail: what was compared, with which tolerances, and who waived
    what

**Not in scope:**
- Capturing legacy outputs from BODS. This doc gives guidance (section 9) but
  no tooling.
- Continuous data-quality monitoring after go-live.

## 2. Principles

- **Step level, in call order.** Steps are compared in the order the job runs
  them. The first failing step is the root-cause candidate. Downstream failures
  that read from it are marked `cascading` rather than reported as separate root
  causes.
- **Explicit tolerances.** Every tolerance is declared in the manifest and
  printed in the report. Nothing is "fuzzy" by default.
- **Sampling is never hidden.** If a comparison samples, the report says so on
  that step and the step cannot reach a plain `pass`, only `pass (sampled)`.
- **IR only.** Like the other modules, it reads the IR and never a parser.

## 3. Manifest (`validation.yml`)

`deltabound validate scaffold` generates a first draft from the IR. It lists the
job's dataflows in call order, with targets, columns and types, and key guesses
from Table Comparison or Key Generation where available.

```yaml
job: JOB_SALES_DAILY
business_date: 2026-10-31
estate: estate.json
defaults:
  numeric: { abs: 0.005, rel: 0 }        # |a-b| <= abs OR |a-b| <= rel*|a|
  timestamp_seconds: 0
  string: { trim: true, case_sensitive: true, empty_is_null: true }   # BODS/Oracle '' = NULL
  ignore_columns: [LOAD_TS, BATCH_ID]
  max_sample_rows: 20
  mask_columns: []                       # values replaced by hashes in samples
steps:
  - dataflow: DF_STG_ORDERS_DELTA
    target: DS_DWH.STG.STG_ORDERS
    legacy:   { path: legacy/2026-10-31/stg_orders.parquet }
    databricks: { table: silver.stg.stg_orders, filter: "ORDER_DATE >= '2026-10-30'" }
    keys: [ORDER_ID]
  - dataflow: DF_DIM_CUSTOMER
    target: DS_DWH.MART.DIM_CUSTOMER
    legacy: { path: legacy/2026-10-31/dim_customer.csv, csv: { delimiter: ";", header: true } }
    databricks: { table: gold.mart.dim_customer }
    keys: [CUSTOMER_ID, VALID_FROM]
    ignore_columns: [CUSTOMER_KEY]       # surrogate keys differ by design
    columns: { NET_AMOUNT: { numeric: { abs: 0.01 } } }
```

## 4. Engines

The checks run against an engine interface, so the same code works for small
and large data:

```python
class Engine(Protocol):
    def load(self, source: SourceSpec) -> Relation: ...
    def schema(self, rel) -> list[ColumnInfo]: ...
    def count(self, rel) -> int: ...
    def anti_join(self, a, b, keys) -> Relation: ...
    def compare(self, a, b, keys, rules) -> Relation: ...   # per-column mismatch flags
    def profile(self, rel, cols) -> dict: ...               # sums, min/max, nulls, distinct
    def sample(self, rel, n) -> list[dict]: ...
```

| Engine | Use | Dependency |
|---|---|---|
| `duckdb` | local runs, CI, data up to tens of GB; reads CSV and Parquet natively | `duckdb` (optional extra `[validate]`) |
| `spark` | on Databricks for full volumes; reads Delta tables and files in cloud storage | pyspark (already on Databricks) |

Comparisons are expressed as SQL generated once and run on either engine, which
keeps the two consistent. **New optional dependency `duckdb`: needs approval.**

## 5. Checks per step (cheap ones first)

1. **Schema.**
   - Missing, extra and renamed columns (renames are suggested from the IR
     lineage).
   - Type compatibility. Expected types come from the IR column `data_type` and
     are mapped to Spark types: `varchar(n)` to string (warn if the max length
     is above n), `decimal(p,s)` with precision and scale checked, `int` to
     int or bigint.
   - Nullability. Results are `fail` or `warn` per item.
2. **Row count.** Exact match, or a declared tolerance (absolute or percent).
3. **Keys.**
   - Each side must have unique keys; duplicates are reported with a sample.
   - Rows only in legacy, and rows only in Databricks (anti joins), with counts
     and samples.
4. **Values on matched keys.**
   - Each column is compared under its rule: numeric abs or rel tolerance,
     timestamp within ±n seconds, string trim and case, `empty_is_null`, and
     NULL equal to NULL.
   - Results are mismatch count and rate per column, plus sample rows showing
     key, legacy value, new value and difference.
5. **Keyless steps** (no keys declared or found).
   - Multiset comparison of normalised row hashes: count per hash, on each side.
   - Column profiles (sum, min, max, null count, approximate distinct) compared
     within tolerance.
   - This is weaker evidence than a key comparison, and the report says so.

The status of each check is `pass`, `warn`, `fail` or `skipped`, and a step's
status is the worst of its checks.

## 6. Root cause using the IR

- **Ordering.** Steps follow `graph.call_graph` order for the job.
- **Cascading.** A step whose source tables are written by an earlier failing
  step is marked `cascading`, so the first failure is easy to spot.
- **Column hints.** For a failing target column, `graph.trace_column` lists its
  upstream transforms and expressions in the report. For example, "NET_AMOUNT
  differs → computed in DF_STG_ORDERS_DELTA/Q_ORDERS as `round(VBAK.NETWR, 2)`".
  This points reviewers at the likely conversion rule.
- **Link to `convert/`.** The conversion report's TODO list is joined in, so a
  step that failed and contains a TODO transform is flagged.

## 7. Results and sign-off report

```python
class CheckResult(BaseModel):  name: str; status: str; metrics: dict; samples: list[dict] = []
class StepResult(BaseModel):   dataflow: str; target: str; status: str; cascading_from: str | None; checks: list[CheckResult]
class ValidationRun(BaseModel):
    job: str; business_date: str; engine: str
    inputs: dict          # file checksums, table versions (Delta history version), IR hash, deltabound version, git commit
    tolerances: dict      # effective rules per step/column
    steps: list[StepResult]
    waivers: list[Waiver]
    status: str           # pass | pass_with_waivers | fail
```

**Waivers** (`waivers.yml`) are accepted, explained differences:
`{step, check, column?, reason, approved_by, expires}`. An expired waiver stops
applying. Waived failures show as `waived` and are never hidden.

**Report** (`report.md` and `report.html`, static with no scripts, easy to
attach to a ticket):

1. Summary: overall status, steps passed, failed and waived, the first failing
   step.
2. A table of steps in call order with the status of each check.
3. Details for each failing or warning step: metrics, mismatch rates per column,
   masked samples, and lineage hints.
4. Tolerances and waivers in effect.
5. Inputs and their checksums, so the run can be reproduced.
6. A sign-off block with "Validated by", "Approved by" and dates, left blank to
   fill in.

**Privacy.** Samples can contain business data. `mask_columns` and
`max_sample_rows` limit them, and `--no-samples` produces a report with metrics
only.

## 8. CLI

```
deltabound validate scaffold demo/sap_bods/exports --job JOB_SALES_DAILY --out validation.yml
deltabound validate run validation.yml --engine duckdb --out validation_out/
deltabound validate report validation_out/result.json --format md,html
```

On Databricks, a generated notebook runs `validate run --engine spark` with the
same manifest.

## 9. Capturing outputs (guidance, not tooling)

- **Legacy side.**
  - Run the BODS job for a fixed business date in a non-production repository.
  - Export each step's target table to Parquet, or to CSV with an explicit
    format.
  - For intermediate steps that are not persisted, add temporary Template Table
    targets in the legacy repository, or validate only persisted steps.
  - Freeze the source data snapshot used.
- **Databricks side.** The notebooks from `convert/` can take a
  `validation_mode` parameter that also writes each step's output to a
  `validation` schema (see convert.md, open question 5). Run them on the same
  source snapshot.

## 10. Testing

- **Fixtures.** `demo/sap_bods/validation/` holds synthetic legacy and new CSV/Parquet
  pairs with seeded differences:
  - an identical step
  - a rounding difference within tolerance
  - a value change outside tolerance
  - a missing row and an extra row
  - duplicate keys
  - schema drift (an extra column and a narrowed decimal)
  - `''` against NULL
  - a keyless table
  - a cascading failure
- **Expected results.** A golden `result.json` and a report snapshot.
- **Engines.** The DuckDB engine is tested fully. The Spark engine reuses the
  same SQL and is tested only when pyspark is installed.

## 11. Milestones

1. Manifest model and `scaffold` from the IR.
2. DuckDB engine with schema, count, key and value checks.
3. Results model, Markdown report and waivers.
4. Root-cause ordering, cascading detection and lineage hints.
5. Spark engine, HTML report and the Databricks notebook wrapper.

## 12. Open questions

1. Can I add `duckdb` as an optional extra for the local engine?
2. What should be the default numeric tolerance? I propose exact for integers
   and `abs: 0.005` for decimals.
3. Should surrogate keys be ignored by default whenever the IR shows a Key
   Generation transform on that target?
4. Must sign-off be recorded only in the report, or also in a shared system
   such as a ticket or a document?
