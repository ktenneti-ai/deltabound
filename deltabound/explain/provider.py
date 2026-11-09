"""LLM providers. Only Anthropic is implemented; `fake` is for tests and offline demos."""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from .config import ExplainConfig, ExplainError

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Completion(BaseModel):
    data: dict
    model: str  # model that produced the answer
    usage: dict[str, int] = Field(default_factory=dict)


class Provider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict) -> Completion: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, cfg: ExplainConfig):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise ExplainError('The Anthropic SDK is not installed: pip install "deltabound[explain]"') from e
        self._sdk = anthropic
        self.cfg = cfg
        # Credentials resolve from ANTHROPIC_API_KEY or an `ant auth login` profile.
        self.client = anthropic.Anthropic(max_retries=cfg.max_retries)

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        sdk, cfg = self._sdk, self.cfg
        request: dict[str, Any] = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if cfg.fallbacks:
                response = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
            else:
                response = self.client.messages.create(**request)
        except sdk.AuthenticationError as e:
            raise ExplainError("Anthropic rejected the credentials. Set ANTHROPIC_API_KEY or run `ant auth login`.") from e
        except sdk.PermissionDeniedError as e:
            raise ExplainError(f"The API key may not use model {cfg.model}: {e.message}") from e
        except sdk.NotFoundError as e:
            raise ExplainError(f"Unknown model {cfg.model}: {e.message}") from e
        except sdk.BadRequestError as e:
            raise ExplainError(f"Anthropic rejected the request: {e.message}") from e
        except sdk.RateLimitError as e:
            raise ExplainError("Rate limited by Anthropic after retries; try again later.") from e
        except sdk.APIStatusError as e:
            raise ExplainError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except sdk.APIConnectionError as e:
            raise ExplainError("Could not reach the Anthropic API (network error).") from e
        except TypeError as e:
            # Raised by the SDK before sending when no credential source resolves.
            if "authentication method" not in str(e):
                raise
            raise ExplainError("No Anthropic credentials found. Set ANTHROPIC_API_KEY or run `ant auth login`, "
                               "or use --dry-run to see the prompts without sending them.") from e

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise ExplainError(f"The model declined to explain this input (category: {category}).")
        if response.stop_reason == "max_tokens":
            raise ExplainError(f"The answer was cut off at max_tokens={cfg.max_tokens}; raise it in [explain].")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ExplainError("The model returned no text block.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ExplainError("The model returned invalid JSON.") from e
        usage = {k: v for k, v in (("input_tokens", response.usage.input_tokens),
                                   ("output_tokens", response.usage.output_tokens)) if isinstance(v, int)}
        return Completion(data=data, model=response.model, usage=usage)


_CONTEXT = re.compile(r"<context>\n(.*?)\n</context>", re.S)


class FakeProvider:
    """Deterministic provider for tests and offline demos. Never calls a network.

    With `responses`, returns them in order. Otherwise it builds "[fake]" claims
    that cite real ids from the context, so the whole pipeline can run end to end.
    """

    name = "fake"

    def __init__(self, cfg: Optional[ExplainConfig] = None, responses: Optional[list[dict]] = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.responses:
            return Completion(data=self.responses.pop(0), model="fake")
        match = _CONTEXT.search(user)
        ctx = json.loads(match.group(1)) if match else {}
        sections = list(schema["properties"])
        return Completion(data=_fake_answer(ctx, sections), model="fake")


def _fake_answer(ctx: dict, sections: list[str]) -> dict:
    out: dict[str, list[dict]] = {s: [] for s in sections}
    if "dataflow" in ctx:
        ts = ctx.get("transforms", [])
        targets = [t for t in ts if t["kind"].endswith("target")]
        if ts:
            out["summary"].append({"text": f"[fake] {ctx['dataflow']} loads {', '.join(t.get('table', '?') for t in targets) or 'no target'}.",
                                   "citations": [t["id"] for t in targets] or [ts[0]["id"]]})
        for t in ts:
            if any(k in t for k in ("where", "joins", "group_by", "branches", "lookups")) or t["kind"].startswith("opaque"):
                out["rules"].append({"text": f"[fake] {t['kind']} step {t['id'].split('/')[-1]}.", "citations": [t["id"]]})
    else:
        tree = ctx.get("call_tree", [])
        if tree:
            out["purpose"].append({"text": f"[fake] {ctx.get('job')} runs {len(tree)} top-level steps.",
                                   "citations": [tree[0]["id"]]})
        for f in ctx.get("facts", []):
            section = "inputs" if f["role"] == "input" else "outputs"
            out[section].append({"text": f"[fake] {f['role']}: {f['name']}.", "citations": [f["id"]]})
        for node in _flatten(tree):
            if node.get("condition"):
                out["business_rules"].append({"text": f"[fake] {node['target']} runs only {node['condition']}.",
                                              "citations": [node["id"]]})
    return out


def _flatten(nodes: list[dict]) -> list[dict]:
    flat = []
    for n in nodes:
        flat.append(n)
        flat += _flatten(n.get("calls", []))
    return flat


def get_provider(cfg: ExplainConfig) -> Provider:
    if cfg.provider == "anthropic":
        return AnthropicProvider(cfg)
    if cfg.provider == "fake":
        return FakeProvider(cfg)
    raise ExplainError(f"Unknown provider '{cfg.provider}'.")
