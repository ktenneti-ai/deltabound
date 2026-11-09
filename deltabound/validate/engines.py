"""Where comparisons run. Both engines execute the same generated SQL (see checks.py).

duckdb: local files (CSV/Parquet), CI and laptops. Optional extra `validate`.
spark:  Databricks or local Spark; also reads tables. pyspark comes with Databricks.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Optional, Protocol

from .manifest import Source, ValidateError

FAMILIES = ("string", "integer", "decimal", "float", "date", "timestamp", "boolean", "other")


class TypeInfo:
    def __init__(self, native: str, family: str, precision: Optional[int] = None, scale: Optional[int] = None):
        self.native, self.family, self.precision, self.scale = native, family, precision, scale

    def __repr__(self) -> str:
        return self.native


def classify(native: str) -> TypeInfo:
    t = native.strip().lower()
    if m := re.fullmatch(r"(decimal|numeric)\((\d+),\s*(\d+)\)", t):
        return TypeInfo(native, "decimal", int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"(n?varchar|varchar2|n?char|string|text)(\(\d+\))?", t):
        return TypeInfo(native, "string")
    if t in ("tinyint", "smallint", "int", "integer", "bigint", "long", "hugeint", "utinyint", "usmallint",
             "uinteger", "ubigint", "short", "byte"):
        return TypeInfo(native, "integer")
    if t in ("double", "float", "real", "float4", "float8"):
        return TypeInfo(native, "float")
    if t == "date":
        return TypeInfo(native, "date")
    if t.startswith("timestamp") or t == "datetime":
        return TypeInfo(native, "timestamp")
    if t in ("boolean", "bool"):
        return TypeInfo(native, "boolean")
    return TypeInfo(native, "other")


class Engine(Protocol):
    name: str

    def register(self, view: str, src: Source, base: Path) -> None: ...
    def columns(self, view: str) -> list[tuple[str, TypeInfo]]: ...
    def rows(self, sql: str) -> list[dict[str, Any]]: ...
    def q(self, ident: str) -> str: ...
    def seconds(self, expr: str) -> str: ...  # timestamp -> epoch seconds
    def fingerprint(self, src: Source, base: Path) -> str: ...


def _file(src: Source, base: Path) -> Path:
    p = Path(src.path)  # type: ignore[arg-type]
    p = p if p.is_absolute() else base / p
    if not p.exists():
        raise ValidateError(f"Input not found: {p}")
    return p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


class DuckDBEngine:
    name = "duckdb"

    def __init__(self) -> None:
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover - environment
            raise ValidateError('DuckDB is not installed: pip install "deltabound[validate]"') from e
        self.con = duckdb.connect()

    def q(self, ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    @staticmethod
    def _lit(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    def register(self, view: str, src: Source, base: Path) -> None:
        if src.table:
            raise ValidateError(f"The duckdb engine reads files only; {src.table!r} needs --engine spark "
                                "(or export the table to Parquet and give its path).")
        path = self._lit(str(_file(src, base)))
        reader = (f"read_csv({path}, header={'true' if src.header else 'false'}, delim={self._lit(src.delimiter)})"
                  if src.format == "csv" else f"read_parquet({path})")
        where = f" WHERE {src.filter}" if src.filter else ""
        self.con.execute(f"CREATE OR REPLACE VIEW {self.q(view)} AS SELECT * FROM {reader}{where}")

    def columns(self, view: str) -> list[tuple[str, TypeInfo]]:
        return [(r[0], classify(r[1])) for r in self.con.execute(f"DESCRIBE {self.q(view)}").fetchall()]

    def rows(self, sql: str) -> list[dict[str, Any]]:
        cur = self.con.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def seconds(self, expr: str) -> str:
        return f"epoch({expr})"

    def fingerprint(self, src: Source, base: Path) -> str:
        return _sha256(_file(src, base))


class SparkEngine:
    name = "spark"

    def __init__(self, spark: Any) -> None:
        self.spark = spark

    def q(self, ident: str) -> str:
        return "`" + ident.replace("`", "``") + "`"

    def register(self, view: str, src: Source, base: Path) -> None:
        if src.table:
            df = self.spark.table(src.table)
        elif src.format == "csv":
            df = (self.spark.read.option("header", src.header).option("sep", src.delimiter)
                  .option("inferSchema", True).csv(str(_file(src, base))))
        else:
            df = self.spark.read.parquet(str(_file(src, base)))
        if src.filter:
            df = df.where(src.filter)
        df.createOrReplaceTempView(view)

    def columns(self, view: str) -> list[tuple[str, TypeInfo]]:
        return [(f.name, classify(f.dataType.simpleString())) for f in self.spark.table(view).schema.fields]

    def rows(self, sql: str) -> list[dict[str, Any]]:
        return [r.asDict() for r in self.spark.sql(sql).collect()]

    def seconds(self, expr: str) -> str:
        return f"unix_timestamp({expr})"

    def fingerprint(self, src: Source, base: Path) -> str:
        if src.table:
            try:
                v = self.spark.sql(f"DESCRIBE HISTORY {src.table} LIMIT 1").collect()[0]["version"]
                return f"delta:{src.table}@v{v}"
            except Exception:  # not a Delta table, or history unavailable
                return f"table:{src.table}"
        return _sha256(_file(src, base))
