"""What a conversion produced and what still needs a person."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TransformResult(BaseModel):
    cite: str  # citation id, e.g. "df:DF_FACT_SALES/Q_JOIN"
    kind: str
    rule: Optional[str] = None  # rule that converted it, None for a TODO
    status: str = "converted"  # converted | todo
    todos: list[str] = Field(default_factory=list)  # blocking: the generated step raises until fixed
    notes: list[str] = Field(default_factory=list)  # non-blocking: review these assumptions


class DataflowResult(BaseModel):
    dataflow: str
    notebook: str  # path inside the output folder
    transforms: list[TransformResult] = Field(default_factory=list)
    variables: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)  # Spark tables written

    @property
    def todo_count(self) -> int:
        return sum(1 for t in self.transforms if t.status == "todo")


class TaskResult(BaseModel):
    task_key: str
    kind: str  # dataflow | script | condition | setup
    source: str  # citation id of the call/script it came from
    todos: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ConversionResult(BaseModel):
    job: str
    files: dict[str, str] = Field(default_factory=dict)  # relative path -> content
    dataflows: list[DataflowResult] = Field(default_factory=list)
    tasks: list[TaskResult] = Field(default_factory=list)

    @property
    def todo_count(self) -> int:
        return sum(d.todo_count for d in self.dataflows) + sum(1 for t in self.tasks if t.todos)
