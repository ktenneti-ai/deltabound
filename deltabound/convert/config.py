"""Conversion settings: [convert] in deltabound.toml (all optional)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from ..ir import TableRef

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


class ConvertError(Exception):
    pass


class ScdDefaults(BaseModel):
    """Used when the IR does not name a history column (decided 2026-11-09)."""

    valid_from: str = "VALID_FROM"
    valid_to: str = "VALID_TO"
    current_flag: str = "CURRENT_FLAG"
    current_value: str = "Y"
    noncurrent_value: Optional[str] = None  # default: "N", or "0" when current_value is "1"
    open_end: str = "9999-12-31"
    effective_date: str = "current_date()"  # Spark SQL expression for VALID_FROM of new versions


class ConvertConfig(BaseModel):
    # "DATASTORE.OWNER" or "DATASTORE" -> "catalog.schema" (or "schema" for a two-level metastore).
    datastores: dict[str, str] = Field(default_factory=dict)
    scd: ScdDefaults = Field(default_factory=ScdDefaults)
    compute: str = "serverless"  # serverless | job_cluster (decided 2026-11-09: serverless)
    bundle_prefix: str = "deltabound"

    def table(self, ref: TableRef) -> tuple[str, bool]:
        """(Spark table name, mapped?) for a table reference. Unmapped falls back to lower-case names."""
        for key in (f"{ref.datastore}.{ref.owner}", f"{ref.datastore}"):
            if key in self.datastores:
                return f"{self.datastores[key]}.{ref.table.lower()}", True
        parts = [p.lower() for p in (ref.datastore, ref.owner, ref.table) if p]
        return ".".join(parts), False


def load_convert_config(path: Optional[Path] = None) -> ConvertConfig:
    toml_path = path or Path("deltabound.toml")
    values: dict = {}
    if toml_path.exists():
        if tomllib is None:
            raise ConvertError(f"Reading {toml_path} needs Python 3.11+.")
        with toml_path.open("rb") as f:
            values = dict(tomllib.load(f).get("convert", {}))
    elif path is not None:
        raise ConvertError(f"Config file not found: {path}")
    try:
        cfg = ConvertConfig(**values)
    except ValidationError as e:
        raise ConvertError(f"Invalid [convert] settings: {e}") from e
    if cfg.compute not in ("serverless", "job_cluster"):
        raise ConvertError(f"compute must be serverless or job_cluster, not {cfg.compute!r}")
    return cfg
