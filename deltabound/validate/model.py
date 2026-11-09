"""Validation results: per check, per step, per run."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

RANK = {"skipped": 0, "pass": 1, "waived": 2, "warn": 3, "fail": 4}


def worst(statuses: list[str]) -> str:
    real = [s for s in statuses if s != "skipped"]
    return max(real, key=RANK.__getitem__) if real else "skipped"


class Finding(BaseModel):
    """One problem inside a check (a column, a schema issue)."""

    column: Optional[str] = None
    issue: str
    status: str  # fail | warn | waived
    metrics: dict[str, Any] = Field(default_factory=dict)
    samples: list[dict[str, Any]] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)  # lineage: where the column is computed
    waiver: Optional[str] = None  # reason + approver when waived


class CheckResult(BaseModel):
    name: str  # schema | row_count | keys | values | multiset
    status: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    samples: list[dict[str, Any]] = Field(default_factory=list)


class StepResult(BaseModel):
    dataflow: str
    target: str
    status: str
    checks: list[CheckResult] = Field(default_factory=list)
    cascading_from: Optional[str] = None  # earlier failing step whose output this step reads
    convert_todos: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)  # side -> fingerprint
    rules: dict[str, Any] = Field(default_factory=dict)  # effective tolerances, for the record
    error: Optional[str] = None


class ValidationRun(BaseModel):
    job: str
    business_date: Optional[str] = None
    engine: str
    created_at: str
    status: str  # pass | pass_with_waivers | fail
    steps: list[StepResult] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    waivers: list[dict[str, Any]] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)  # manifest, IR, deltabound version
