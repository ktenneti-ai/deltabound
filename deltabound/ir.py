"""ETL Graph IR: a tool-neutral description of a legacy ETL estate.

Every source parser (BODS first, then SSIS, Informatica, ...) produces an
`Estate`. Everything downstream (graph, lineage, explanations, planning,
conversion, validation) reads only this model, never the source files.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

# 0.2.0: adds Transform.joins/group_by/order_by/branches/lookups/options and
# CallStep.control. All new fields have defaults, so 0.1.0 JSON still loads.
# 0.2.1: adds LookupRef.default_values/policy/cache and Transform.key_columns/
# compare_columns/history/generated_key (needed by convert/). Additive only.
IR_VERSION = "0.2.1"


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
    condition: Optional[str] = None  # set when the call sits inside an if/while/catch
    # Innermost enclosing control construct: None | "branch" | "loop" | "error_handler"
    control: Optional[str] = None
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


class Join(BaseModel):
    left: str  # input name
    right: str
    join_type: str = "inner"  # inner | left_outer | right_outer | full_outer | cross
    condition: Optional[str] = None


class SortKey(BaseModel):
    expression: str
    direction: str = "asc"  # asc | desc


class Branch(BaseModel):
    """One named output of a routing transform (Case, Conditional Split, ...)."""

    name: str
    condition: Optional[str] = None
    is_default: bool = False


class LookupRef(BaseModel):
    """A lookup against a reference table, as a function call or a transform."""

    function: str  # e.g. "lookup_ext", "lookup", "lookup_transform"
    table: Optional[TableRef] = None  # None when it cannot be resolved statically
    return_columns: list[str] = Field(default_factory=list)
    match_columns: list[tuple[str, str]] = Field(default_factory=list)  # (lookup column, input expression)
    output_column: Optional[str] = None  # mapped column that uses it; None for a lookup transform
    default_values: list[str] = Field(default_factory=list)  # value(s) returned when nothing matches, as expressions
    policy: Optional[str] = None  # which row wins when several match: "max", "min", ... (lower case)
    cache: Optional[str] = None  # tool's caching hint, e.g. "PRE_LOAD_CACHE"; informational
    raw: Optional[str] = None  # original call text, always kept


class HistorySettings(BaseModel):
    """Bookkeeping columns of a history-preserving (SCD type 2) transform."""

    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    current_flag: Optional[str] = None
    current_value: Optional[str] = None  # flag value marking the current row
    open_end: Optional[str] = None  # valid_to value of the current row, e.g. "9999-12-31"


class Transform(BaseModel):
    name: str
    kind: str  # query, table_source, table_target, lookup, case, merge, ... or "opaque"
    inputs: list[str] = Field(default_factory=list)
    table: Optional[TableRef] = None
    columns: list[ColumnMapping] = Field(default_factory=list)
    where: Optional[str] = None
    joins: list[Join] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[SortKey] = Field(default_factory=list)
    branches: list[Branch] = Field(default_factory=list)
    lookups: list[LookupRef] = Field(default_factory=list)
    key_columns: list[str] = Field(default_factory=list)  # business key a comparison/SCD transform matches on
    compare_columns: list[str] = Field(default_factory=list)  # columns compared for changes (empty = all non-key)
    history: Optional[HistorySettings] = None  # set on history-preserving (SCD2) transforms
    generated_key: Optional[str] = None  # column a key-generation transform fills
    options: dict[str, str] = Field(default_factory=dict)  # tool-specific extras (load mode, ...)
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
