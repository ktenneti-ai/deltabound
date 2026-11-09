"""API governance: no external API call without a human-approved, priced plan.

1. Plan:     every call a run would make (cache hits excluded), with a worst-case price:
             each call retried once, every response at max_tokens.
2. Limits:   [explain.limits] in the source manifest caps calls, tokens and dollars per run.
             A plan over a limit is refused before anyone is asked.
3. Approval: a person approves that exact plan (typing APPROVE, or --approve <plan token>).
4. Enforce:  GovernedProvider refuses any call that would exceed the approved budget.
5. Audit:    approvals, calls and refusals are appended to audit.jsonl next to the cache.
"""
from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import math
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .config import ExplainConfig, ExplainError, Limits
from .provider import Completion, Provider

CHARS_PER_TOKEN = 3.0  # conservative: real prompts average closer to 4 characters per token
RETRY_FACTOR = 2  # a citation check may retry each call once

# USD per million tokens (input, output). Source: Anthropic pricing as of 2026-10.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
}


def prices(cfg: ExplainConfig, limits: Limits) -> tuple[float, float]:
    if limits.price_input_per_mtok is not None and limits.price_output_per_mtok is not None:
        return limits.price_input_per_mtok, limits.price_output_per_mtok
    if cfg.model in PRICES:
        return PRICES[cfg.model]
    raise ExplainError(f"No price known for model {cfg.model}: set price_input_per_mtok and "
                       "price_output_per_mtok in [explain.limits] so a cost cap can be enforced.")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class PlannedCall(BaseModel):
    purpose: str  # "dataflow DF_X" or "job JOB_Y"
    input_tokens: int  # estimate for one attempt


class Plan(BaseModel):
    jobs: list[str]
    provider: str
    model: str
    max_tokens: int
    calls: list[PlannedCall] = Field(default_factory=list)
    cached: int = 0  # answers reused from the cache (free)
    max_calls: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_cost_usd: float = 0.0

    def finalize(self, cfg: ExplainConfig, limits: Limits) -> "Plan":
        pin, pout = prices(cfg, limits)
        self.max_calls = len(self.calls) * RETRY_FACTOR
        self.max_input_tokens = sum(c.input_tokens for c in self.calls) * RETRY_FACTOR
        self.max_output_tokens = self.max_calls * self.max_tokens
        self.max_cost_usd = round((self.max_input_tokens * pin + self.max_output_tokens * pout) / 1e6, 2)
        return self

    @property
    def token(self) -> str:
        """Approval token: a hash of exactly what would be sent and at what ceiling."""
        body = self.model_dump(exclude={"cached"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]

    def violations(self, limits: Limits) -> list[str]:
        out = []
        if self.max_calls > limits.max_calls:
            out.append(f"up to {self.max_calls} calls > max_calls {limits.max_calls}")
        if self.max_input_tokens > limits.max_input_tokens:
            out.append(f"up to {self.max_input_tokens:,} input tokens > max_input_tokens {limits.max_input_tokens:,}")
        if self.max_output_tokens > limits.max_output_tokens:
            out.append(f"up to {self.max_output_tokens:,} output tokens > max_output_tokens {limits.max_output_tokens:,}")
        if self.max_cost_usd > limits.max_cost_usd:
            out.append(f"up to ${self.max_cost_usd:.2f} > max_cost_usd ${limits.max_cost_usd:.2f}")
        return out

    def describe(self) -> str:
        lines = [f"Plan {self.token}: {len(self.calls)} request(s) to {self.provider} {self.model} "
                 f"for {', '.join(self.jobs)} ({self.cached} answer(s) reused from cache, free)."]
        for c in self.calls:
            lines.append(f"  - {c.purpose}: about {c.input_tokens:,} input tokens")
        lines.append(f"Worst case (each request retried once, every answer at max_tokens={self.max_tokens:,}): "
                     f"{self.max_calls} calls, {self.max_input_tokens:,} input + {self.max_output_tokens:,} output "
                     f"tokens, at most ${self.max_cost_usd:.2f}. Typical runs cost far less.")
        return "\n".join(lines)


class Approval(BaseModel):
    token: str
    approver: str
    approved_at: str
    plan: Plan


def approve(plan: Plan, approver: Optional[str] = None) -> Approval:
    return Approval(token=plan.token, approver=approver or getpass.getuser(),
                    approved_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), plan=plan)


class AuditLog:
    def __init__(self, path: Path):
        self.path = path

    def write(self, event: str, **fields: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")


class GovernedProvider:
    """Wraps an external provider: every call is checked against the approved budget and audited."""

    def __init__(self, inner: Provider, cfg: ExplainConfig, approval: Approval, audit: AuditLog):
        self.inner, self.cfg, self.approval, self.audit = inner, cfg, approval, audit
        self.name = inner.name
        self.purpose = "request"  # set by the Explainer before each call, for the audit log
        self.pin, self.pout = prices(cfg, cfg.limits)
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        audit.write("approved", token=approval.token, approver=approval.approver, model=cfg.model,
                    jobs=approval.plan.jobs, max_calls=approval.plan.max_calls,
                    max_cost_usd=approval.plan.max_cost_usd)

    def _refuse(self, reason: str, purpose: str) -> None:
        self.audit.write("refused", token=self.approval.token, purpose=purpose, reason=reason)
        raise ExplainError(f"API budget: {reason}. Nothing more was sent; approve a new plan to continue.")

    def complete_json(self, system: str, user: str, schema: dict) -> Completion:
        plan = self.approval.plan
        purpose = self.purpose
        est_in = estimate_tokens(system + user)
        worst = (est_in * self.pin + self.cfg.max_tokens * self.pout) / 1e6
        if self.calls >= plan.max_calls:
            self._refuse(f"approved {plan.max_calls} calls already used", purpose)
        if self.input_tokens + est_in > plan.max_input_tokens:
            self._refuse("approved input tokens would be exceeded", purpose)
        if self.cost + worst > plan.max_cost_usd + 1e-9:
            self._refuse(f"next call could exceed the approved ${plan.max_cost_usd:.2f}", purpose)
        self.calls += 1
        try:
            out = self.inner.complete_json(system, user, schema)
        except ExplainError as e:
            self.audit.write("call_failed", token=self.approval.token, purpose=purpose, model=self.cfg.model, error=str(e))
            raise
        tin = out.usage.get("input_tokens", est_in)
        tout = out.usage.get("output_tokens", self.cfg.max_tokens)
        cost = (tin * self.pin + tout * self.pout) / 1e6
        self.input_tokens += tin
        self.output_tokens += tout
        self.cost += cost
        self.audit.write("call", token=self.approval.token, purpose=purpose, model_requested=self.cfg.model,
                         model_served=out.model,
                         input_tokens=tin, output_tokens=tout, cost_usd=round(cost, 4),
                         run_cost_usd=round(self.cost, 4), call_number=self.calls)
        return out
