# Design: AI job explainer (`deltabound/explain/`)

Status: IMPLEMENTED 2026-11-09, including the viewer integration. Provider: Anthropic only, plus a fake provider for tests. Author: Claude for Kishore Tenneti, 2026-11-09.

## Goal

For one job, produce a plain-English summary with four sections: purpose,
inputs, outputs and business rules. **Every claim cites the IR element it came
from.** Results are cached as JSON, shown in the viewer, and the LLM provider
is configurable.

## Principles

- **Facts first, wording second.** Inputs and outputs come straight from the IR
  without the LLM: source and target tables, file sources and targets, and lookup
  tables. The LLM writes the purpose and the business rules, and phrases the
  inputs and outputs. This leaves less room for invented content.
- **No uncited claims.** Every claim must cite at least one valid citation ID,
  or it is rejected (see Validation).
- **explain/ reads the IR only.** It never imports a parser, the same rule as
  `graph.py`.

## Citation IDs

Each element gets a stable, readable ID that the viewer can link to:

| Element | ID format | Example |
|---|---|---|
| Transform | `df:<DATAFLOW>/<TRANSFORM>` | `df:DF_FACT_POS_SALES/CASE_CHANNEL` |
| Column mapping | `df:<DATAFLOW>/<TRANSFORM>.<COLUMN>` | `df:DF_FACT_SALES/Q_JOIN.PRODUCT_KEY` |
| Call step | `call:<OBJECT>#<order>` | `call:WF_STAGE_SALES#1` |
| Script | `script:<OBJECT>#<n>` | `script:WF_POS_INTRADAY#2` |
| Variable | `var:<OBJECT>/<NAME>` | `var:JOB_SALES_DAILY/$G_FULL_LOAD` |

## Pipeline

```
Estate --collect--> JobContext --(per dataflow)--> LLM --> DataflowNotes (cached)
                                   \--(job level, from notes + call tree)--> LLM --> JobExplanation (cached)
```

1. **collect.py.** Takes the job's call subgraph (`graph.call_graph`
   descendants) and builds a compact JSON context. It contains the call tree with
   conditions and control, variables, scripts, and for each dataflow the
   transforms with joins, where clauses, group by, branches, lookups, column
   expressions and citation IDs.
2. **Map step, per dataflow.** The LLM summarises one dataflow's rules with
   citations. It is cached by dataflow content hash, so a dataflow shared by 8
   jobs is explained once. This also keeps large jobs within the context limit.
3. **Reduce step, per job.** Uses the dataflow notes, the call tree
   (conditions, loops, error handlers) and the facts from the IR to produce the
   final explanation.

## Output model (pydantic, `explain/model.py`)

```python
class Claim(BaseModel):
    text: str
    citations: list[str]          # >= 1 valid citation id

class JobExplanation(BaseModel):
    job: str
    purpose: list[Claim]
    inputs: list[Claim]           # facts from IR, wording from LLM
    outputs: list[Claim]
    business_rules: list[Claim]
    rejected: list[dict] = []     # claims dropped by validation, with reason (kept for audit)
    provider: str; model: str; prompt_version: str
    context_hash: str             # sha256 of JobContext + prompt_version + model
    created_at: str
```

## Validation

- The provider is asked for structured JSON matching the model, through tool
  use or a JSON schema.
- Every citation must exist in that job's context.
- If a claim has no valid citation, the model gets one retry with the list of
  invalid IDs. Claims that still fail move to `rejected` and are never shown as
  explanation.
- Every source and target table from the IR must appear in inputs or outputs.
  Anything missing is added from the facts with a "listed from IR" note.

## Providers (configurable)

```python
class Provider(Protocol):
    name: str
    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...
```

| Provider | Package | Covers |
|---|---|---|
| `anthropic` (default) | `anthropic` | Claude (default model `claude-opus-5-5`, configurable) |
| `fake` | none | Tests: returns fixtures, no network |

The `Provider` interface stays, so another provider can be added later without
touching the rest of explain/. Only Anthropic is implemented now.

Settings come from `deltabound.toml` (`[explain] provider, model, max_tokens`),
overridden by the environment variables `DELTABOUND_LLM_PROVIDER` and
`DELTABOUND_LLM_MODEL`. The API key comes only from `ANTHROPIC_API_KEY` and is
never written to the cache.

The SDK is an optional extra, `pip install deltabound[explain]`, imported only
when used. `anthropic` was approved on 2026-11-09.

## Privacy

ETL logic (table names, expressions, literals) is client IP. It is sent to the
LLM only on an explicit `explain` command.

- `--dry-run` writes the exact prompts to disk without sending anything.
- `redact_literals = true` replaces string and number literals in expressions
  with placeholders.
- For a client estate, check the client allows its ETL logic to be sent to
  Anthropic before running explain.

## Cache and CLI

- Cache files: `.deltabound/explain/dataflows/<hash>.json` and
  `.deltabound/explain/jobs/<JOB>.json`. A cache entry is reused while
  `context_hash` matches, and `--refresh` forces a new call.
- `deltabound explain demo/sap_bods/exports --job JOB_SALES_DAILY [--all] [--refresh] [--dry-run]`
- `deltabound explain ... --bundle explanations.json` writes one file the viewer
  can load next to `estate.json`.

## Viewer integration

- An "Explanation" tab on the job page shows the four sections.
- Each citation is a chip that jumps to the transform, column, call or script
  it names.
- Rejected claims sit in a collapsed "not shown: failed citation check" panel.

## Tests (no network)

- collect: contents of the context and citation IDs for `JOB_SALES_DAILY`, and
  shared dataflows are deduplicated.
- validation: invalid citations are retried and then rejected, and missing IR
  tables are added back.
- cache: a hit, a miss after the IR changes, and `--refresh`.
- config: precedence of file versus environment variables, and a clear error
  for an unknown provider.
- An end-to-end run with the `fake` provider on `demo/sap_bods/exports`.

## Decisions

- 2026-11-09: Anthropic is the only provider; `anthropic` is approved as an
  optional extra.
