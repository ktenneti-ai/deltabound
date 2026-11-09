"""validate/: manifest, checks on the synthetic JOB_SALES_DAILY sample, waivers, cascading, report, CLI."""
import datetime as dt
import json
import tomllib
from functools import lru_cache
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from deltabound import cli  # noqa: E402
from deltabound.parsers import bods  # noqa: E402
from deltabound.validate import (DuckDBEngine, ValidateError, load_manifest, render_html, render_markdown,  # noqa: E402
                                 run_validation, scaffold)
from deltabound.validate.engines import classify  # noqa: E402
from deltabound.validate.manifest import Source  # noqa: E402

ROOT = Path(__file__).parent.parent
SAMPLE = ROOT / "demo" / "sap_bods" / "validation" / "JOB_SALES_DAILY"
TODAY = dt.date(2026, 11, 9)


@lru_cache(maxsize=1)
def estate():
    return bods.parse_files(sorted((ROOT / "demo" / "sap_bods" / "exports").glob("*.xml")))


@lru_cache(maxsize=None)
def result(waivers: bool = True):
    m = load_manifest(SAMPLE / "validation.toml", SAMPLE / "waivers.toml" if waivers else None)
    return run_validation(estate(), m, DuckDBEngine(), today=TODAY)


def step(name: str, waivers: bool = True):
    return next(s for s in result(waivers).steps if s.dataflow == name)


def check(name: str, c: str, waivers: bool = True):
    return next(x for x in step(name, waivers).checks if x.name == c)


# ---------- manifest ----------

def test_manifest_loads_with_waivers():
    m = load_manifest(SAMPLE / "validation.toml", SAMPLE / "waivers.toml")
    assert m.job == "JOB_SALES_DAILY" and len(m.steps) == 6 and len(m.waivers) == 2
    assert m.steps[0].legacy.format == "csv" and m.steps[0].databricks.format == "parquet"
    assert m.base == SAMPLE.resolve()


def test_manifest_errors(tmp_path):
    with pytest.raises(ValidateError, match="not found"):
        load_manifest(tmp_path / "nope.toml")
    (tmp_path / "bad.toml").write_text('job = "J"\n[[steps]]\ndataflow = "D"\ntarget = "T"\nlegacy = { path = "a.xlsx" }\n'
                                       'databricks = { path = "b.parquet" }\n')
    with pytest.raises(ValidateError, match="cannot infer format"):
        load_manifest(tmp_path / "bad.toml")
    with pytest.raises(ValueError, match="exactly one of path or table"):
        Source(path="a.csv", table="t")


def test_column_rule_overrides():
    m = load_manifest(SAMPLE / "validation.toml")
    from deltabound.validate.manifest import RuleOverride
    s = m.steps[0].model_copy(update={"columns": {"net_amount": RuleOverride(numeric_abs=0.5)}})
    assert s.rules(m.defaults, "NET_AMOUNT").numeric_abs == 0.5
    assert s.rules(m.defaults, "OTHER").numeric_abs == 0.005


def test_type_classification():
    assert (classify("DECIMAL(15,2)").family, classify("DECIMAL(15,2)").scale) == ("decimal", 2)
    assert classify("decimal(17,1)").precision == 17
    assert [classify(t).family for t in ("VARCHAR", "string", "BIGINT", "int", "DOUBLE", "DATE", "TIMESTAMP_NS", "timestamp")] \
        == ["string", "string", "integer", "integer", "float", "date", "timestamp", "timestamp"]


# ---------- checks on the sample ----------

def test_order_and_overall_status():
    r = result()
    assert [s.dataflow for s in r.steps] == ["DF_STG_ORDERS_DELTA", "DF_STG_ORDER_ITEMS", "DF_DIM_CUSTOMER",
                                             "DF_DIM_PRODUCT", "DF_FACT_SALES", "DF_AGG_SALES_DAILY"]  # call order
    assert r.status == "fail"
    assert [s.status for s in r.steps] == ["pass", "fail", "pass", "fail", "fail", "fail"]


def test_tolerances_trim_and_empty_is_null_pass():
    s = step("DF_STG_ORDERS_DELTA")
    assert s.status == "pass"
    v = check("DF_STG_ORDERS_DELTA", "values")
    assert v.metrics["matched_rows"] == 3
    assert v.metrics["columns"]["NET_AMOUNT"] == "within 0.005 absolute"
    assert v.metrics["columns"]["SALES_ORG"] == "trimmed, '' = NULL"


def test_tolerance_can_be_turned_off(tmp_path):
    m = load_manifest(SAMPLE / "validation.toml")
    m.defaults.numeric_abs = 0
    m.defaults.empty_is_null = False
    m.steps = [s for s in m.steps if s.dataflow == "DF_STG_ORDERS_DELTA"]
    r = run_validation(estate(), m, DuckDBEngine(), today=TODAY)
    f = next(c for c in r.steps[0].checks if c.name == "values").findings
    assert [x.column for x in f] == ["NET_AMOUNT"]  # '' vs NULL: CSV readers already return NULL for ''
    assert f[0].samples[0]["legacy"] == "100.004"  # widened, not rounded to DECIMAL(15,2)


def test_keys_only_in_one_side_with_samples():
    k = check("DF_STG_ORDER_ITEMS", "keys")
    assert k.status == "fail" and k.metrics["only_legacy"] == 1 and k.metrics["only_databricks"] == 1
    assert {(x["only_in"], x["ORDER_ID"]) for x in k.samples} == {("legacy", "O3"), ("databricks", "O4")}


def test_value_mismatch_waived_with_lineage_hint():
    v = check("DF_STG_ORDER_ITEMS", "values")
    f = v.findings[0]
    assert (v.status, f.column, f.status) == ("waived", "LINE_NET", "waived")
    assert "Agreed with finance" in f.waiver
    assert f.samples == [{"ORDER_ID": "O1", "LINE_NO": 20, "legacy": "40", "databricks": "40.02"}]
    assert f.hints == ["computed in DF_STG_ORDER_ITEMS: Q_ITEMS.LINE_NET = VBAP.NETWR"]


def test_without_waivers_the_value_mismatch_fails():
    assert check("DF_STG_ORDER_ITEMS", "values", waivers=False).status == "fail"


def test_expired_waiver_is_not_applied():
    s = check("DF_AGG_SALES_DAILY", "schema")
    assert s.status == "fail"
    assert s.findings[0].issue == "DECIMAL(17,1) has fewer decimals than legacy decimal(17,2)"
    w = next(w for w in result().waivers if w["dataflow"] == "DF_AGG_SALES_DAILY")
    assert w["expired"] is True


def test_scd2_current_rows_and_ignored_surrogate_key_pass():
    s = step("DF_DIM_CUSTOMER")
    assert s.status == "pass"
    assert s.rules["ignored"] == ["customer_key", "valid_from", "valid_to"]
    assert check("DF_DIM_CUSTOMER", "row_count").metrics == {"legacy": 2, "databricks": 2}  # filter applied


def test_duplicate_keys_skip_values_and_extra_column_warns():
    k = check("DF_DIM_PRODUCT", "keys")
    assert (k.metrics["duplicate_legacy"], k.metrics["duplicate_databricks"]) == (0, 1)
    assert check("DF_DIM_PRODUCT", "values").status == "skipped"
    sch = check("DF_DIM_PRODUCT", "schema")
    assert sch.status == "warn" and sch.findings[0].column == "LOAD_TS"


def test_keyless_multiset_compare():
    m = check("DF_AGG_SALES_DAILY", "multiset")
    assert m.status == "pass" and m.metrics["only_legacy"] == 0
    assert not any(c.name == "keys" for c in step("DF_AGG_SALES_DAILY").checks)


def test_cascading_points_at_earliest_failing_upstream():
    assert step("DF_FACT_SALES").cascading_from == "DF_STG_ORDER_ITEMS"  # also reads failing DIM_PRODUCT (lookup)
    assert step("DF_AGG_SALES_DAILY").cascading_from == "DF_FACT_SALES"
    assert step("DF_STG_ORDER_ITEMS").cascading_from is None


def test_inputs_are_fingerprinted():
    s = step("DF_STG_ORDERS_DELTA")
    assert s.inputs["legacy"].startswith("sha256:") and s.inputs["databricks"].startswith("sha256:")
    assert result().inputs["ir"].startswith("sha256:")


def test_masking(tmp_path):
    m = load_manifest(SAMPLE / "validation.toml")
    m.defaults.mask_columns = ["ORDER_ID"]
    m.steps = [s for s in m.steps if s.dataflow == "DF_STG_ORDER_ITEMS"]
    r = run_validation(estate(), m, DuckDBEngine(), today=TODAY)
    samples = next(c for c in r.steps[0].checks if c.name == "keys").samples
    assert all(x["ORDER_ID"].startswith("#") and len(x["ORDER_ID"]) == 11 for x in samples)


def test_bad_inputs_become_step_errors():
    m = load_manifest(SAMPLE / "validation.toml")
    m.steps = [m.steps[1].model_copy(update={"keys": ["NOPE"]}),
               m.steps[2].model_copy(update={"legacy": Source(path="legacy/missing.csv")})]
    r = run_validation(estate(), m, DuckDBEngine(), today=TODAY)
    assert [s.status for s in r.steps] == ["fail", "fail"]
    assert "key column(s) ['nope'] not on both sides" in r.steps[0].error
    assert "Input not found" in r.steps[1].error


def test_duckdb_rejects_tables():
    m = load_manifest(SAMPLE / "validation.toml")
    m.steps = [m.steps[1].model_copy(update={"databricks": Source(table="main.stg.t")})]
    r = run_validation(estate(), m, DuckDBEngine(), today=TODAY)
    assert "needs --engine spark" in r.steps[0].error


def test_conversion_todos_are_linked():
    report = {"dataflows": [{"dataflow": "DF_FACT_SALES",
                             "transforms": [{"cite": "df:DF_FACT_SALES/Q_JOIN", "todos": ["something"]}]}]}
    m = load_manifest(SAMPLE / "validation.toml")
    r = run_validation(estate(), m, DuckDBEngine(), today=TODAY, conversion_report=report)
    assert next(s for s in r.steps if s.dataflow == "DF_FACT_SALES").convert_todos == ["df:DF_FACT_SALES/Q_JOIN: something"]


# ---------- report ----------

def test_markdown_report():
    md = render_markdown(result())
    assert "**Status: FAIL** (2 of 6 steps pass; 4 fail (2 root cause(s), 2 cascading); first failing step: DF_STG_ORDER_ITEMS)" in md
    assert "**FAIL (cascading from DF_STG_ORDER_ITEMS)**" in md
    assert "— **EXPIRED, not applied**" in md
    assert "| Validated by | | | |" in md and "| Approved by (data owner) | | | |" in md


def test_html_report_is_static_and_escaped():
    page = render_html(result())
    assert page.startswith("<!doctype html>") and "<script" not in page
    assert "cascading from DF_STG_ORDER_ITEMS" in page and "prefers-color-scheme:dark" in page
    assert "Legacy rounds line net per unit" in page


# ---------- scaffold ----------

def test_scaffold_guesses_keys_ignores_and_filters():
    text = scaffold(estate(), "JOB_SALES_DAILY")
    data = tomllib.loads(text)
    steps = {s["dataflow"]: s for s in data["steps"]}
    cust = steps["DF_DIM_CUSTOMER"]
    assert cust["keys"] == ["CUSTOMER_ID"]
    assert cust["ignore_columns"] == ["CUSTOMER_KEY", "VALID_FROM", "VALID_TO"]
    assert cust["legacy"]["filter"] == "IS_CURRENT = '1'"
    assert steps["DF_FACT_SALES"]["ignore_columns"] == ["CUSTOMER_KEY", "PRODUCT_KEY"]
    assert "# ignored PRODUCT_KEY: surrogate key looked up from DS_DWH.MART.DIM_PRODUCT" in text
    assert len(data["steps"]) == 15


def test_scaffold_uses_convert_datastore_mapping():
    from deltabound.convert import ConvertConfig
    text = scaffold(estate(), "JOB_CUSTOMER_SYNC", ConvertConfig(datastores={"DS_DWH.MART": "main.mart"}))
    assert 'databricks = { table = "main.mart.dim_customer"' in text


# ---------- CLI ----------

def test_cli_run_writes_reports_and_fails(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", "run", str(SAMPLE / "validation.toml"), "--waivers", str(SAMPLE / "waivers.toml"),
                  "--out", str(tmp_path)])
    assert e.value.code == 1
    assert {p.name for p in tmp_path.iterdir()} == {"result.json", "report.md", "report.html"}
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "fail"
    assert capsys.readouterr().out.startswith("FAIL: 2 of 6 steps pass")


def test_cli_run_passes_on_clean_steps(tmp_path):
    text = (SAMPLE / "validation.toml").read_text()
    keep = [b for b in text.split("[[steps]]")[1:] if "DF_STG_ORDERS_DELTA" in b or "DF_DIM_CUSTOMER" in b]
    manifest = SAMPLE / "_clean_test.toml"
    try:
        manifest.write_text(text.split("[[steps]]")[0] + "".join("[[steps]]" + b for b in keep))
        cli.main(["validate", "run", str(manifest), "--out", str(tmp_path)])  # exits 0: no SystemExit
    finally:
        manifest.unlink()
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "pass"


def test_cli_scaffold(tmp_path, capsys):
    out = tmp_path / "v.toml"
    cli.main(["validate", "scaffold", str(ROOT / "demo" / "sap_bods" / "exports"), "--job", "JOB_SALES_DAILY", "--out", str(out),
              "--business-date", "2026-10-31"])
    assert tomllib.loads(out.read_text())["business_date"] == "2026-10-31"
    assert "15 steps" in capsys.readouterr().out
