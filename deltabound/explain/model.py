"""Explanation results. Every claim cites the IR elements it came from."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Claim(BaseModel):
    text: str
    citations: list[str]  # citation ids, e.g. "df:DF_FACT_SALES/Q_JOIN"
    origin: str = "llm"  # "llm" or "ir" (added from IR facts when the model missed it)


class RejectedClaim(BaseModel):
    section: str
    text: str
    citations: list[str]
    reason: str


class RunInfo(BaseModel):
    provider: str
    model: str  # model that actually answered (may differ when a fallback ran)
    prompt_version: str
    context_hash: str
    created_at: str


class DataflowNotes(BaseModel):
    dataflow: str
    summary: list[Claim] = Field(default_factory=list)
    rules: list[Claim] = Field(default_factory=list)
    rejected: list[RejectedClaim] = Field(default_factory=list)
    run: RunInfo


class JobExplanation(BaseModel):
    job: str
    purpose: list[Claim] = Field(default_factory=list)
    inputs: list[Claim] = Field(default_factory=list)
    outputs: list[Claim] = Field(default_factory=list)
    business_rules: list[Claim] = Field(default_factory=list)
    rejected: list[RejectedClaim] = Field(default_factory=list)
    dataflows: list[str] = Field(default_factory=list)  # dataflows whose notes fed this summary
    run: RunInfo


class ExplanationBundle(BaseModel):
    """One file the viewer loads next to estate.json."""

    format: str = "deltabound-explanations"
    version: int = 1
    ir_version: Optional[str] = None
    jobs: dict[str, JobExplanation] = Field(default_factory=dict)
    dataflows: dict[str, DataflowNotes] = Field(default_factory=dict)
