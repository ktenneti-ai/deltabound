"""The Spark engine runs the same generated SQL as DuckDB: results must match step by step."""
import datetime as dt
from pathlib import Path

import pytest

pytestmark = pytest.mark.spark
pytest.importorskip("duckdb")
pytest.importorskip("pyspark")

from deltabound.parsers import bods  # noqa: E402
from deltabound.validate import DuckDBEngine, SparkEngine, load_manifest, run_validation  # noqa: E402

from .test_convert_spark import spark  # noqa: E402,F401  (shared local Spark session fixture)

ROOT = Path(__file__).parent.parent
SAMPLE = ROOT / "demo" / "sap_bods" / "validation" / "JOB_SALES_DAILY"


def summary(run):
    return [(s.dataflow, s.status, s.cascading_from,
             [(c.name, c.status, sorted((f.column, f.status) for f in c.findings)) for c in s.checks]) for s in run.steps]


def test_spark_matches_duckdb(spark):  # noqa: F811
    e = bods.parse_files(sorted((ROOT / "demo" / "sap_bods" / "exports").glob("*.xml")))
    m = load_manifest(SAMPLE / "validation.toml", SAMPLE / "waivers.toml")
    today = dt.date(2026, 11, 9)
    duck = run_validation(e, m, DuckDBEngine(), today=today)
    sp = run_validation(e, m, SparkEngine(spark), today=today)
    assert [s.error for s in sp.steps] == [None] * 6
    assert summary(sp) == summary(duck)
    assert sp.status == duck.status == "fail"
