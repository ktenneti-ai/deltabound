# API governance

DeltaBound calls an external API in exactly one place: `deltabound explain`, which sends ETL
logic to Anthropic's Claude API. The parsers, the viewer, `convert` and `validate` make no
outside calls. A test (`tests/test_governance.py`) reads the whole package's source and
fails if any other module imports an API or network client.

No call is ever made without a person approving it:

| Stage | What happens |
|---|---|
| Plan | `explain` works out every request a run would send. Answers already in the cache are reused and are free. Each request gets an input-token estimate (about 3 characters per token, deliberately high), and the run gets a **worst-case** price: every request retried once, and every answer using all of `max_tokens`. |
| Limits | `[explain.limits]` in the source's `deltabound.toml` caps calls, input tokens, output tokens and dollars per run. A plan over any cap is refused before anyone is asked. Raising a cap is a deliberate edit by the owner. |
| Approval | In a terminal, the plan is printed and the person types `APPROVE`. Anything else cancels. In automation, `--approve <token>` is required, and the token is a hash of exactly that plan (jobs, model, `max_tokens`, every request, the ceilings). It cannot approve a different or larger run. `--plan` prints the plan and token without sending anything. |
| Enforcement | Every request goes through `GovernedProvider`. Before each call it checks the calls used, the input tokens, and that the next call's worst case still fits the approved dollar ceiling. It refuses rather than exceed them, using real token counts from each response. A provider that is not marked `external = False` cannot be used without an approved plan, including one passed in from Python. |
| Audit | Every approval, call (purpose, model requested and served, tokens, cost, running total) and refusal is appended to `audit.jsonl` in the explain cache folder of the source. |

```bash
deltabound explain demo/sap_bods --job JOB_CUSTOMER_SYNC --plan          # review: nothing is sent
deltabound explain demo/sap_bods --job JOB_CUSTOMER_SYNC                 # interactive: type APPROVE
deltabound explain demo/sap_bods --job JOB_CUSTOMER_SYNC --approve 3dff50c1ce1a --approver "Name"   # CI
```

```toml
[explain.limits]          # per run; defaults shown
max_calls = 100
max_input_tokens = 1000000
max_output_tokens = 2000000
max_cost_usd = 25.0
# price_input_per_mtok / price_output_per_mtok: required for models without a built-in price
```

Prices for current Claude models are built in (`explain/governance.py`, as of 2026-10). For
any other model, set the prices in `[explain.limits]`, or the run is refused because its cost
could not be capped.

Not covered: the SDK retries a failed request up to `max_retries` (default 2) on rate-limit
and server errors. Failed attempts are not billed. `--dry-run` writes the exact prompts to
disk for review and sends nothing.
