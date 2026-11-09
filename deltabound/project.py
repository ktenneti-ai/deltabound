"""Source projects: one folder per legacy source with a deltabound.toml that holds everything.

    demo/sap_bods/
      deltabound.toml   [source] type, exports; [convert]; [explain] (+ limits); [validate]
      exports/          the legacy tool's export files
      validation/       validation manifests and saved outputs

Every CLI command accepts the folder (or its deltabound.toml) in place of export paths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

from .ir import Estate

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

MANIFEST = "deltabound.toml"

# Source type -> (file patterns, how to parse). Planned types have no parser yet.
SOURCE_TYPES: dict[str, dict] = {
    "sap_bods": {"label": "SAP Data Services", "patterns": ["*.xml"]},
    "ssis": {"label": "SQL Server Integration Services", "patterns": ["*.dtsx"]},
    "sql_server": {"label": "SQL Server Agent jobs and T-SQL", "patterns": ["*.sql"]},
}


class ProjectError(Exception):
    pass


class SourceSection(BaseModel):
    name: str
    type: str
    status: str = "supported"  # supported | planned
    exports: list[str] = Field(default_factory=lambda: ["exports"])  # relative to the manifest
    description: Optional[str] = None
    input: Optional[str] = None  # what the exports contain


class Project(BaseModel):
    manifest: Path
    source: SourceSection
    validate_manifests: list[str] = Field(default_factory=list)

    @property
    def base(self) -> Path:
        return self.manifest.parent

    def export_files(self) -> list[Path]:
        patterns = SOURCE_TYPES[self.source.type]["patterns"]
        files: list[Path] = []
        for rel in self.source.exports:
            p = self.base / rel
            if p.is_dir():
                files += sorted(f for pat in patterns for f in p.rglob(pat))
            elif p.exists():
                files.append(p)
        return files

    def load_estate(self) -> Estate:
        if self.source.status != "supported":
            raise ProjectError(f"{self.source.name}: this source type is {self.source.status} (no parser yet); "
                               f"see {self.base / 'README.md'}")
        files = self.export_files()
        if not files:
            raise ProjectError(f"{self.source.name}: no export files found in {', '.join(self.source.exports)}")
        if self.source.type == "sap_bods":
            from .parsers import bods

            return bods.parse_files(files)
        if self.source.type == "ssis":
            from .parsers import ssis

            return ssis.parse_files(files)
        raise ProjectError(f"no parser for source type {self.source.type}")  # pragma: no cover


def find_manifest(path: Path) -> Optional[Path]:
    """The deltabound.toml for a project folder or manifest path; None for plain export paths."""
    if path.is_dir() and (path / MANIFEST).exists():
        return path / MANIFEST
    if path.is_file() and path.suffix == ".toml" and _read(path).get("source"):
        return path
    return None


def _read(path: Path) -> dict:
    if tomllib is None:
        raise ProjectError("Reading TOML needs Python 3.11+.")
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"{path}: invalid TOML: {e}") from e


def load_project(path: Path) -> Project:
    manifest = find_manifest(path)
    if manifest is None:
        raise ProjectError(f"{path} is not a source folder (no {MANIFEST} with a [source] section)")
    data = _read(manifest)
    try:
        source = SourceSection(**data.get("source", {}))
    except ValidationError as e:
        raise ProjectError(f"{manifest}: [source]: {e}") from e
    if source.type not in SOURCE_TYPES:
        raise ProjectError(f"{manifest}: unknown source type {source.type!r}; known: {', '.join(SOURCE_TYPES)}")
    return Project(manifest=manifest.resolve(), source=source,
                   validate_manifests=list(data.get("validate", {}).get("manifests", [])))
