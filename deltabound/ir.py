"""ETL Graph IR: a tool-neutral description of a legacy ETL estate.

Every source parser (BODS first, then SSIS, Informatica, ...) produces an
`Estate`. Everything downstream (graph, lineage, explanations, planning,
conversion, validation) reads only this model, never the source files.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

IR_VERSION = "0.1.0"


class ObjectKind(str, Enum):
    JOB = "job"
    WORKFLOW = "workflow"
    DATAFLOW = "dataflow"


class Variable(BaseModel):
    name: str
    scope: str = "global"  # global | local | parameter
    data_type: Optional[str] = None
    default: Optional[str] = None


class CallStep(BaseModel):
    """One object calling another (job -> workflow, workflow -> dataflow...)."""

    target: str
    target_kind: Optional[ObjectKind] = None
    condition: Optional[str] = None  # set when the call sits inside an if/while
    order: int = 0


class TableRef(BaseModel):
    datastore: Optional[str] = None
    owner: Optional[str] = None
    table: str

    @property
    def qualified(self) -> str:
        parts = [p for p in (self.datastore, self.owner, self.table) if p]
        return ".".join(parts)


class ColumnMapping(BaseModel):
    """Output column of a transform and the expression that produces it."""

    column: str
    data_type: Optional[str] = None
    expression: Optional[str] = None
    source_columns: list[str] = Field(default_factory=list)  # "INPUT.COL"


class Transform(BaseModel):
    name: str
    kind: str  # query, table_source, table_target, lookup, case, merge, ... or "opaque"
    inputs: list[str] = Field(default_factory=list)
    table: Optional[TableRef] = None
    columns: list[ColumnMapping] = Field(default_factory=list)
    where: Optional[str] = None
    raw_tag: Optional[str] = None  # original element name, kept for opaque nodes


class EtlObject(BaseModel):
    name: str
    kind: ObjectKind
    description: Optional[str] = None
    variables: list[Variable] = Field(default_factory=list)
    calls: list[CallStep] = Field(default_factory=list)
    transforms: list[Transform] = Field(default_factory=list)
    scripts: list[str] = Field(default_factory=list)


class Estate(BaseModel):
    ir_version: str = IR_VERSION
    source_tool: str
    source_version: Optional[str] = None
    objects: dict[str, EtlObject] = Field(default_factory=dict)
    unparsed: list[str] = Field(default_factory=list)  # tags we could not model

    def by_kind(self, kind: ObjectKind) -> list[EtlObject]:
        return [o for o in self.objects.values() if o.kind == kind]
