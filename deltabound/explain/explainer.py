"""Explain jobs: per-dataflow notes (map), then a job summary (reduce), all cached."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel

from ..ir import Estate, ObjectKind
from . import context as cx
from . import prompts
from .config import ExplainConfig, ExplainError
from .governance import Approval, AuditLog, GovernedProvider, Plan, PlannedCall, estimate_tokens
from .model import Claim, DataflowNotes, ExplanationBundle, JobExplanation, RejectedClaim, RunInfo
from .provider import Provider, get_provider

M = TypeVar("M", bound=BaseModel)


class Explainer:
    def __init__(self, estate: Estate, cfg: ExplainConfig, provider: Optional[Provider] = None,
                 dry_run: bool = False, approval: Optional[Approval] = None):
        self.estate = estate
        self.cfg = cfg
        self.dry_run = dry_run
        self._provider = provider
        self._provider_ready = False
        self.approval = approval
        self.cache = Path(cfg.cache_dir)
        self.audit = AuditLog(self.cache / "audit.jsonl")
        self.stats = {"llm_calls": 0, "cache_hits": 0, "retries": 0}

    @property
    def provider(self) -> Provider:
        """The provider, wrapped in governance when it would send data to an outside API.

        Applies to injected providers too: anything not marked external = False is governed.
        """
        if not self._provider_ready:  # created lazily: dry runs and cache hits need no credentials
            inner = self._provider or get_provider(self.cfg)
            if getattr(inner, "external", True) and not isinstance(inner, GovernedProvider):
                if self.approval is None:
                    self.audit.write("refused", reason="no approved plan", provider=inner.name)
                    raise ExplainError("API calls need an approved plan: run `deltabound explain ... --plan`, "
                                       "review it, then approve it (type APPROVE or pass --approve <token>).")
                inner = GovernedProvider(inner, self.cfg, self.approval, self.audit)
            self._provider = inner
            self._provider_ready = True
        return self._provider

    def plan(self, jobs: list[str], refresh: bool = False) -> Plan:
        """Every request a run would send (cache hits excluded), priced at worst case. Sends nothing."""
        p = Plan(jobs=jobs, provider=self.cfg.provider, model=self.cfg.model, max_tokens=self.cfg.max_tokens)
        seen: set[str] = set()
        for job in jobs:
            job_ctx = cx.job_context(self.estate, job, self.cfg.redact_literals)
            dataflows = cx.job_dataflows(self.estate, job)
            df_ctxs = {d: cx.dataflow_context(self.estate, d, self.cfg.redact_literals) for d in dataflows}
            for d, ctx in df_ctxs.items():
                if d in seen:
                    continue
                seen.add(d)
                cached = not refresh and self._load(self.cache / "dataflows" / f"{d}.json", DataflowNotes,
                                                    self._hash(ctx), count=False)
                if cached:
                    p.cached += 1
                else:
                    p.calls.append(PlannedCall(purpose=f"dataflow {d}", input_tokens=estimate_tokens(
                        prompts.DATAFLOW_SYSTEM + prompts.user_prompt(ctx))))
            key = self._hash(job_ctx, [self._hash(c) for c in df_ctxs.values()])
            if not refresh and self._load(self.cache / "jobs" / f"{job}.json", JobExplanation, key, count=False):
                p.cached += 1
                continue
            notes_allowance = 700 * len(dataflows)  # dataflow notes are added to the job prompt
            p.calls.append(PlannedCall(purpose=f"job {job}", input_tokens=estimate_tokens(
                prompts.JOB_SYSTEM + prompts.user_prompt(job_ctx, [])) + notes_allowance))
        return p.finalize(self.cfg, self.cfg.limits)

    # ---------- public ----------

    def dataflow(self, name: str, refresh: bool = False) -> Optional[DataflowNotes]:
        ctx = cx.dataflow_context(self.estate, name, self.cfg.redact_literals)
        key = self._hash(ctx)
        path = self.cache / "dataflows" / f"{name}.json"
        if not refresh and (cached := self._load(path, DataflowNotes, key)) is not None:
            return cached
        user = prompts.user_prompt(ctx)
        if self.dry_run:
            self._write_prompt(f"dataflow-{name}", prompts.DATAFLOW_SYSTEM, user)
            return None
        claims, rejected, model = self._ask(prompts.DATAFLOW_SYSTEM, user, prompts.DATAFLOW_SCHEMA,
                                             prompts.DATAFLOW_SECTIONS, cx.citation_ids(ctx), f"dataflow {name}")
        notes = DataflowNotes(dataflow=name, **claims, rejected=rejected, run=self._run(model, key))
        self._save(path, notes)
        return notes

    def job(self, name: str, refresh: bool = False) -> Optional[JobExplanation]:
        job_ctx = cx.job_context(self.estate, name, self.cfg.redact_literals)
        dataflows = cx.job_dataflows(self.estate, name)
        df_ctxs = [cx.dataflow_context(self.estate, d, self.cfg.redact_literals) for d in dataflows]
        key = self._hash(job_ctx, [self._hash(c) for c in df_ctxs])
        path = self.cache / "jobs" / f"{name}.json"
        if not refresh and (cached := self._load(path, JobExplanation, key)) is not None:
            return cached

        all_notes = [self.dataflow(d, refresh) for d in dataflows]
        if self.dry_run:
            placeholder = [{"dataflow": d, "note": "dataflow notes are inserted here"} for d in dataflows]
            self._write_prompt(f"job-{name}", prompts.JOB_SYSTEM, prompts.user_prompt(job_ctx, placeholder))
            return None
        notes = [n for n in all_notes if n is not None]
        notes_json = [{"dataflow": n.dataflow,
                       "summary": [c.model_dump(include={"text", "citations"}) for c in n.summary],
                       "rules": [c.model_dump(include={"text", "citations"}) for c in n.rules]} for n in notes]
        valid = cx.citation_ids(job_ctx, *df_ctxs)
        claims, rejected, model = self._ask(prompts.JOB_SYSTEM, prompts.user_prompt(job_ctx, notes_json),
                                             prompts.JOB_SCHEMA, prompts.JOB_SECTIONS, valid, f"job {name}")
        _add_missing_facts(claims, job_ctx.get("facts", []))
        result = JobExplanation(job=name, **claims, rejected=rejected, dataflows=dataflows,
                                run=self._run(model, key))
        self._save(path, result)
        return result

    def bundle(self, jobs: list[str], refresh: bool = False) -> ExplanationBundle:
        b = ExplanationBundle(ir_version=self.estate.ir_version)
        for j in jobs:
            exp = self.job(j, refresh)
            if exp is None:
                continue
            b.jobs[j] = exp
            for d in exp.dataflows:
                notes = self.dataflow(d)
                if notes is not None:
                    b.dataflows[d] = notes
        return b

    # ---------- internals ----------

    def _ask(self, system: str, user: str, schema: dict, sections: tuple[str, ...],
             valid: set[str], purpose: str = "request") -> tuple[dict[str, list[Claim]], list[RejectedClaim], str]:
        """Call the model; retry once if any claim cites ids outside the context."""
        if hasattr(self.provider, "purpose"):
            self.provider.purpose = purpose  # type: ignore[attr-defined]
        completion = self.provider.complete_json(system, user, schema)
        self.stats["llm_calls"] += 1
        claims, invalid = _split(completion.data, sections, valid)
        if invalid:
            self.stats["retries"] += 1
            feedback = prompts.retry_feedback({c.text: c.citations for _, c in invalid})
            completion = self.provider.complete_json(system, user + feedback, schema)
            self.stats["llm_calls"] += 1
            claims, invalid = _split(completion.data, sections, valid)
        rejected = [
            RejectedClaim(section=s, text=c.text, citations=c.citations,
                          reason="no citations" if not c.citations else
                          "cites ids not in the context: " + ", ".join(i for i in c.citations if i not in valid))
            for s, c in invalid
        ]
        return claims, rejected, completion.model

    def _hash(self, *parts: object) -> str:
        return cx.context_hash(parts, self.cfg.provider, self.cfg.model, prompts.PROMPT_VERSION,
                               self.cfg.redact_literals)

    def _run(self, model: str, key: str) -> RunInfo:
        return RunInfo(provider=self.cfg.provider, model=model, prompt_version=prompts.PROMPT_VERSION,
                       context_hash=key, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def _load(self, path: Path, cls: type[M], key: str, count: bool = True) -> Optional[M]:
        if not path.exists():
            return None
        try:
            obj = cls.model_validate_json(path.read_text())
        except ValueError:
            return None  # unreadable or old-format cache entry: recompute
        if obj.run.context_hash != key:  # type: ignore[attr-defined]
            return None
        if count:
            self.stats["cache_hits"] += 1
        return obj

    def _save(self, path: Path, obj: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(obj.model_dump_json(indent=2))

    def _write_prompt(self, name: str, system: str, user: str) -> None:
        path = self.cache / "prompts" / f"{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n")


def _split(data: dict, sections: tuple[str, ...], valid: set[str]
           ) -> tuple[dict[str, list[Claim]], list[tuple[str, Claim]]]:
    good: dict[str, list[Claim]] = {s: [] for s in sections}
    bad: list[tuple[str, Claim]] = []
    for s in sections:
        for raw in data.get(s) or []:
            claim = Claim(text=str(raw.get("text", "")).strip(), citations=list(raw.get("citations") or []))
            if claim.citations and all(c in valid for c in claim.citations):
                good[s].append(claim)
            else:
                bad.append((s, claim))
    return good, bad


def _add_missing_facts(claims: dict[str, list[Claim]], facts: list[dict]) -> None:
    """Every table or file the IR says the job reads or writes must appear in inputs/outputs."""
    for f in facts:
        section = "inputs" if f["role"] == "input" else "outputs"
        if any(f["id"] in c.citations for c in claims[section]):
            continue
        verb = {"input": "Reads", "output": "Writes", "intermediate": "Writes and later reads"}[f["role"]]
        refs = f.get("read_by", []) if f["role"] == "input" else f.get("written_by", [])
        claims[section].append(Claim(
            text=f"{verb} {f['name']} (listed from the IR; not described by the model).",
            citations=[f["id"], *refs], origin="ir",
        ))


def explainable_jobs(estate: Estate) -> list[str]:
    return sorted(j.name for j in estate.by_kind(ObjectKind.JOB))
