"""validation.toml: what to compare, how strictly, and accepted differences (waivers).

TOML rather than YAML: the standard library reads it (Python 3.11+), so no new dependency.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class ValidateError(Exception):
    pass


class Rules(BaseModel):
    """How two values count as equal. Defaults decided 2026-11-09: exact integers, decimals within 0.005."""

    numeric_abs: float = 0.005  # decimals/floats: |legacy - new| <= numeric_abs ...
    numeric_rel: float = 0.0  # ... or <= numeric_rel * |legacy|
    timestamp_seconds: float = 0.0
    trim: bool = True
    case_sensitive: bool = True
    empty_is_null: bool = True  # BODS/Oracle treat '' as NULL. CSV readers (DuckDB, Spark) already read
    # an empty CSV field as NULL, so for CSV inputs '' and NULL cannot be told apart either way.


class RuleOverride(BaseModel):
    numeric_abs: Optional[float] = None
    numeric_rel: Optional[float] = None
    timestamp_seconds: Optional[float] = None
    trim: Optional[bool] = None
    case_sensitive: Optional[bool] = None
    empty_is_null: Optional[bool] = None


class Defaults(Rules):
    ignore_columns: list[str] = Field(default_factory=list)
    max_sample_rows: int = 20
    mask_columns: list[str] = Field(default_factory=list)  # sample values replaced by a hash
    row_count_abs: int = 0
    row_count_pct: float = 0.0


class Source(BaseModel):
    path: Optional[str] = None  # CSV or Parquet file (relative to the manifest)
    table: Optional[str] = None  # table name (spark engine)
    format: Optional[str] = None  # csv | parquet; inferred from the path
    delimiter: str = ","
    header: bool = True
    filter: Optional[str] = None  # SQL condition in the engine's dialect

    @model_validator(mode="after")
    def _one_location(self) -> "Source":
        if bool(self.path) == bool(self.table):
            raise ValueError("give exactly one of path or table")
        if self.path and not self.format:
            suffix = Path(self.path).suffix.lower()
            self.format = {".csv": "csv", ".txt": "csv", ".parquet": "parquet", ".pq": "parquet"}.get(suffix)
            if self.format is None:
                raise ValueError(f"cannot infer format of {self.path}; set format = 'csv' or 'parquet'")
        return self

    def label(self) -> str:
        return self.path or f"table {self.table}"


class Step(BaseModel):
    dataflow: str
    target: str  # qualified legacy table, e.g. DS_DWH.STG.STG_ORDERS
    keys: list[str] = Field(default_factory=list)  # empty: keyless comparison
    ignore_columns: list[str] = Field(default_factory=list)
    legacy: Source
    databricks: Source
    columns: dict[str, RuleOverride] = Field(default_factory=dict)

    def rules(self, defaults: Defaults, column: str) -> Rules:
        base = Rules(**defaults.model_dump(include=set(Rules.model_fields)))
        over = next((v for k, v in self.columns.items() if k.lower() == column.lower()), None)
        if over:
            base = base.model_copy(update={k: v for k, v in over.model_dump().items() if v is not None})
        return base


class Waiver(BaseModel):
    dataflow: str
    check: str  # schema | row_count | keys | values | multiset
    column: Optional[str] = None  # for values/schema findings on one column
    reason: str
    approved_by: str
    expires: Optional[dt.date] = None

    def applies(self, dataflow: str, check: str, column: Optional[str], today: dt.date) -> bool:
        if self.expires is not None and self.expires < today:
            return False
        return (self.dataflow == dataflow and self.check == check
                and (self.column is None or (column or "").lower() == self.column.lower()))


class Manifest(BaseModel):
    job: str
    business_date: Optional[str] = None
    estate: list[str] = Field(default_factory=list)  # legacy export files/folders, relative to the manifest
    defaults: Defaults = Field(default_factory=Defaults)
    steps: list[Step] = Field(default_factory=list)
    waivers: list[Waiver] = Field(default_factory=list)
    _base: Path = PrivateAttr(default=Path("."))

    @property
    def base(self) -> Path:
        return self._base


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise ValidateError("Reading TOML needs Python 3.11+.")
    if not path.exists():
        raise ValidateError(f"File not found: {path}")
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValidateError(f"{path}: invalid TOML: {e}") from e


def load_manifest(path: Path, extra_waivers: Optional[Path] = None) -> Manifest:
    data = _read_toml(path)
    if extra_waivers is not None:
        data.setdefault("waivers", []).extend(_read_toml(extra_waivers).get("waivers", []))
    try:
        m = Manifest(**data)
    except ValidationError as e:
        raise ValidateError(f"{path}: {e}") from e
    m._base = path.parent.resolve()
    return m


# ---------- writing TOML (scaffold) ----------

def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(x)}" for k, x in v.items() if x is not None) + " }"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'
