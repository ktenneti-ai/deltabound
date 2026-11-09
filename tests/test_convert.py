"""convert/: expression translation, transform rules, job structure, golden output. No Spark needed."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

from deltabound import cli
from deltabound.convert import ConvertConfig, ConvertError, convert_dataflow, convert_job, load_convert_config
from deltabound.convert.expressions import strip_outer_parens, translate
from deltabound.convert.job import split_statements, task_key, to_yaml
from deltabound.ir import Estate, ObjectKind
from deltabound.parsers import bods

ROOT = Path(__file__).parent.parent
ESTATE_DIR = ROOT / "demo" / "sap_bods" / "exports"
GOLDEN = Path(__file__).parent / "golden" / "convert"


@lru_cache(maxsize=1)
def estate() -> Estate:
    return bods.parse_files(sorted(ESTATE_DIR.glob("*.xml")))


def cell(notebook: str, source: str) -> str:
    """The notebook cell generated for one transform."""
    return next(c for c in notebook.split("# COMMAND ----------") if f"# source: {source} " in c)


@lru_cache(maxsize=None)
def job(name: str):
    return convert_job(estate(), name, ConvertConfig())


def tasks(name: str) -> dict[str, dict]:
    """Tasks of a converted job, rebuilt from the builder output (not from YAML text)."""
    r = job(name)
    y = r.files[f"resources/{name.lower()}.job.yml"]
    out = {}
    for block in re.split(r"(?m)^        - task_key: ", y)[1:]:
        key = json.loads(block.split("\n")[0])
        deps = [(json.loads(k), json.loads(o) if o else None)
                for k, o in re.findall(r'(?m)^            - task_key: ("[^"]+")(?:\n\s+outcome: ("\w+"))?', block)]
        run_if = re.search(r'(?m)^          run_if: "(\w+)"', block)
        out[key] = {"deps": deps, "run_if": run_if.group(1) if run_if else None, "text": block}
    return out


# ---------- expressions ----------

@pytest.mark.parametrize("bods_expr,spark", [
    ("VBAK.VBELN", "`VBAK`.`VBELN`"),
    ("to_date(VBAK.ERDAT, 'YYYYMMDD')", "to_date(`VBAK`.`ERDAT`, 'yyyyMMdd')"),
    ("to_char($G_LOAD_DATE - 1, 'YYYY-MM-DD HH24:MI:SS')",
     "date_format((system.session.G_LOAD_DATE - 1), 'yyyy-MM-dd HH:mm:ss')"),
    ("to_char(99999999 - TCURR.GDATU)", "CAST((99999999 - `TCURR`.`GDATU`) AS STRING)"),
    ("nvl(MAKT.MAKTX, 'UNKNOWN')", "coalesce(`MAKT`.`MAKTX`, 'UNKNOWN')"),
    ("ifthenelse(BSEG.SHKZG = 'H', -BSEG.DMBTR, BSEG.DMBTR)",
     "IF((`BSEG`.`SHKZG` = 'H'), -`BSEG`.`DMBTR`, `BSEG`.`DMBTR`)"),
    ("decode(a = 1, 'one', a = 2, 'two', 'many')", "CASE WHEN (`a` = 1) THEN 'one' WHEN (`a` = 2) THEN 'two' ELSE 'many' END"),
    ("count_distinct(F.ORDER_ID)", "COUNT(DISTINCT `F`.`ORDER_ID`)"),
    ("sysdate()", "current_timestamp()"),
    ("day_in_month($G_LOAD_DATE) = 1", "(dayofmonth(system.session.G_LOAD_DATE) = 1)"),
    ("replace_substr(S.POSTCODE, ' ', '')", "replace(`S`.`POSTCODE`, ' ', '')"),
    ("a <> b OR c != d", "((`a` <> `b`) OR (`c` <> `d`))"),
    ("x NOT LIKE 'S%' AND y IS NOT NULL", "((`x` NOT LIKE 'S%') AND (`y` IS NOT NULL))"),
    ("x NOT BETWEEN 1 AND 3", "(`x` NOT BETWEEN 1 AND 3)"),
    ("x IN ('A', 'B')", "(`x` IN ('A', 'B'))"),
    ("'it''s' || 'C:\\dir'", "('it''s' || 'C:\\\\dir')"),
    ("1 + 2 * 3", "(1 + (2 * 3))"),
    ("(1 + 2) * 3", "((1 + 2) * 3)"),
    ("NOT ($G_FULL_LOAD = 'Y')", "(NOT (system.session.G_FULL_LOAD = 'Y'))"),
])
def test_translate(bods_expr, spark):
    r = translate(bods_expr)
    assert r.todos == []
    assert r.sql == spark


def test_translate_collects_variables_and_aggregates():
    r = translate("sum(F.AMOUNT) * $G_RATE + $G_FEE")
    assert r.variables == {"G_RATE", "G_FEE"} and r.has_aggregate
    assert not translate("upper(x)").has_aggregate


@pytest.mark.parametrize("bods_expr,todo", [
    ("match_regex(x, '[0-9]+')", "match_regex() has no Spark mapping"),
    ("to_date(x, $G_FORMAT)", "date format mask is not a literal"),
    ("nvl(x)", "unsupported number of arguments"),
    ("a +", "could not parse expression"),
    ("x # y", "could not parse expression"),
])
def test_translate_reports_todos_instead_of_guessing(bods_expr, todo):
    assert any(todo in t for t in translate(bods_expr).todos)


def test_every_sample_expression_translates():
    exprs = set()
    for o in estate().objects.values():
        for t in o.transforms:
            exprs |= {c.expression for c in t.columns if c.expression and not any(l.output_column == c.column for l in t.lookups)}
            exprs |= {t.where} if t.where else set()
            exprs |= {j.condition for j in t.joins if j.condition} | set(t.group_by)
    assert len(exprs) > 200
    assert [x for x in exprs if translate(x).todos] == []


def test_strip_outer_parens():
    assert strip_outer_parens("(a = b)") == "a = b"
    assert strip_outer_parens("(a) = (b)") == "(a) = (b)"
    assert strip_outer_parens("('(' = x)") == "'(' = x"


# ---------- config ----------

def test_table_mapping_and_fallback():
    from deltabound.ir import TableRef
    cfg = ConvertConfig(datastores={"DS_ERP.SAPSR3": "bronze.sap", "DS_DWH": "main.dwh"})
    assert cfg.table(TableRef(datastore="DS_ERP", owner="SAPSR3", table="VBAK")) == ("bronze.sap.vbak", True)
    assert cfg.table(TableRef(datastore="DS_DWH", owner="STG", table="T")) == ("main.dwh.t", True)
    assert cfg.table(TableRef(datastore="DS_X", owner="O", table="T")) == ("ds_x.o.t", False)


def test_load_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_convert_config().compute == "serverless"
    (tmp_path / "deltabound.toml").write_text('[convert]\ncompute = "job_cluster"\n[convert.datastores]\n"DS_ERP" = "bronze.erp"\n'
                                              '[convert.scd]\ncurrent_value = "1"\n')
    c = load_convert_config()
    assert (c.compute, c.datastores, c.scd.current_value) == ("job_cluster", {"DS_ERP": "bronze.erp"}, "1")
    (tmp_path / "bad.toml").write_text('[convert]\ncompute = "gpu"\n')
    with pytest.raises(ConvertError, match="serverless or job_cluster"):
        load_convert_config(tmp_path / "bad.toml")


# ---------- dataflow rules ----------

def nb(df: str, **cfg):
    return convert_dataflow(estate(), df, ConvertConfig(**cfg))


def test_query_with_join_lookups_and_types():
    text, res, _ = nb("DF_FACT_SALES")
    q = cell(text, "df:DF_FACT_SALES/Q_JOIN")
    assert "INNER JOIN `STG_ORDER_ITEMS` ON `STG_ORDERS`.`ORDER_ID` = `STG_ORDER_ITEMS`.`ORDER_ID`" in q
    assert "CAST(coalesce(`_lk2`.`PRODUCT_KEY`, -1) AS INT) AS `PRODUCT_KEY`" in q
    assert "ROW_NUMBER() OVER (PARTITION BY `PRODUCT_ID` ORDER BY `PRODUCT_KEY` DESC)" in q
    assert "`_lk2`.`PRODUCT_ID` = `STG_ORDER_ITEMS`.`PRODUCT_ID`" in q
    assert "CAST(`STG_ORDER_ITEMS`.`LINE_NET` AS DECIMAL(15,2)) AS `LINE_AMOUNT`" in q
    assert res.writes == ["ds_dwh.mart.fact_sales"] and res.todo_count == 0


def test_left_outer_join_and_multi_condition_lookup():
    q = cell(nb("DF_FACT_MARGIN")[0], "df:DF_FACT_MARGIN/Q_MARGIN")
    assert "LEFT OUTER JOIN `PRODUCT_COST` ON" in q
    assert "`_lk1`.`FROM_CCY` = `PRODUCT_COST`.`CURRENCY` AND `_lk1`.`TO_CCY` = 'EUR'" in q


def test_where_group_by_and_variables():
    text, res, _ = nb("DF_AGG_SALES_DAILY")
    q = cell(text, "df:DF_AGG_SALES_DAILY/Q_AGG_DAY")
    assert "WHERE `FACT_SALES`.`ORDER_DATE` = (system.session.G_LOAD_DATE - 1)" in q
    assert "GROUP BY `FACT_SALES`.`ORDER_DATE`, `FACT_SALES`.`PRODUCT_KEY`" in q
    assert "ORDER BY" not in q  # table target: order dropped, with a note
    assert any("ORDER BY dropped" in n for n in res.transforms[1].notes)
    assert res.variables == ["G_LOAD_DATE"]
    assert '_set_var("G_LOAD_DATE", "DATE", dbutils.widgets.get("G_LOAD_DATE"))' in text


def test_order_by_kept_for_file_targets():
    q = cell(nb("DF_FX_RATES_PUBLISH")[0], "df:DF_FX_RATES_PUBLISH/Q_FX_PUB")
    assert "ORDER BY `STG_FX_RATES`.`FROM_CCY` ASC, `STG_FX_RATES`.`TO_CCY` ASC" in q


def test_case_first_match_and_null_safe_default():
    text, res, _ = nb("DF_FACT_POS_SALES")
    c = cell(text, "df:DF_FACT_POS_SALES/CASE_CHANNEL")
    assert "WHERE coalesce(`Q_POS_IN`.`CHANNEL` = 'STORE', false)\"\"\")" in c
    assert ("WHERE coalesce(`Q_POS_IN`.`CHANNEL` = 'KIOSK', false) AND NOT coalesce(`Q_POS_IN`.`CHANNEL` = 'STORE', false)"
            in c)
    assert "WHERE NOT (coalesce(`Q_POS_IN`.`CHANNEL` = 'STORE', false) OR coalesce(`Q_POS_IN`.`CHANNEL` = 'KIOSK', false))" in c
    assert 'df_CH_OTHER.createOrReplaceTempView("CH_OTHER")' in c
    case = next(t for t in res.transforms if t.kind == "case")
    assert any("first matching branch" in n for n in case.notes)


def test_merge_and_old_style_lookup():
    text, _, _ = nb("DF_FACT_POS_SALES")
    assert "df_MRG_CHANNELS = df_Q_STORE.unionByName(df_Q_KIOSK).unionByName(df_Q_OTHER)" in text
    store = cell(text, "df:DF_FACT_POS_SALES/Q_STORE")
    assert "coalesce(`_lk1`.`STORE_KEY`, -1)" in store and "FROM ds_dwh.mart.dim_store" in store


def test_lookup_transform():
    c = cell(nb("DF_FACT_LOYALTY_POINTS")[0], "df:DF_FACT_LOYALTY_POINTS/LKP_MEMBER_KEY")
    assert "SELECT `Q_TXN`.*," in c and "coalesce(`_lk1`.`MEMBER_KEY`, NULL) AS `MEMBER_KEY`" in c


def test_scd2_chain_uses_export_settings_and_identity_key():
    text, res, ddl = nb("DF_DIM_CUSTOMER")
    c = cell(text, "df:DF_DIM_CUSTOMER/DIM_CUSTOMER")
    assert "TC_CUST -> HP_CUST -> KG_CUST -> DIM_CUSTOMER" in c
    assert "keys=['CUSTOMER_ID'], compare=['CUSTOMER_NAME', 'COUNTRY', 'CITY']" in c
    assert 'flag="IS_CURRENT", current="1", noncurrent="0"' in c
    target = next(t for t in res.transforms if t.kind == "table_target")
    assert target.rule == "scd2_merge" and any("identity column" in n for n in target.notes)
    assert {t.rule for t in res.transforms if t.kind in ("table_comparison", "history_preserving", "key_generation")} == {"scd_merge"}
    table, statement = ddl[0]
    assert table == "ds_dwh.mart.dim_customer"
    assert "`CUSTOMER_KEY` BIGINT GENERATED ALWAYS AS IDENTITY" in statement
    assert "`VALID_FROM` DATE" in statement and "`IS_CURRENT` STRING" in statement


def test_scd2_defaults_when_export_has_no_flag():
    text, res, _ = nb("DF_DIM_LOYALTY_MEMBER")
    assert 'flag="CURRENT_FLAG", current="Y", noncurrent="N"' in text
    target = next(t for t in res.transforms if t.kind == "table_target")
    assert any("current flag not in the export" in n for n in target.notes)
    text, _, _ = nb("DF_DIM_LOYALTY_MEMBER", scd={"current_value": "1"})
    assert 'current="1", noncurrent="0"' in text


def test_scd1_chain():
    text, res, _ = nb("DF_DIM_PRODUCT")
    assert "_scd1_merge(df_Q_PROD, \"ds_dwh.mart.dim_product\", keys=['PRODUCT_ID'], compare=[])" in text
    assert "def _scd2_merge" not in text


def test_unconvertible_steps_raise():
    for df, cite, reason in [
        ("DF_ECOM_SCORE", "df:DF_ECOM_SCORE/PY_BASKET_SCORE", "no conversion rule for opaque (DICustomPythonTransform)"),
        ("DF_STG_POS_TRANSACTIONS", "df:DF_STG_POS_TRANSACTIONS/FF_POS_TXN", "no conversion rule for file_source"),
        ("DF_FACT_RETURNS", "df:DF_FACT_RETURNS/VAL_RETURNS", "no conversion rule for validation"),
        ("DF_AGG_MARGIN_BY_CATEGORY", "df:DF_AGG_MARGIN_BY_CATEGORY/Q_CAT", "only known at run time"),
        ("DF_FACT_STOCK_MOVES", "df:DF_FACT_STOCK_MOVES/TC_MOVES", "no conversion rule for table_comparison"),
    ]:
        text, res, _ = nb(df)
        c = cell(text, cite)
        assert reason in c and f'raise NotImplementedError("TODO(deltabound): {cite} needs manual conversion")' in c
        assert any(t.cite == cite and t.status == "todo" for t in res.transforms)


def test_unmapped_datastore_is_a_note_not_a_todo():
    _, res, _ = nb("DF_STG_ORDERS_DELTA")
    src = res.transforms[0]
    assert src.status == "converted" and "not mapped" in src.notes[0]
    _, res, _ = nb("DF_STG_ORDERS_DELTA", datastores={"DS_ERP.SAPSR3": "bronze.sap"})
    assert res.transforms[0].notes == []


def test_every_dataflow_notebook_compiles_and_starts_correctly():
    for o in estate().by_kind(ObjectKind.DATAFLOW):
        text, res, _ = convert_dataflow(estate(), o.name, ConvertConfig())
        compile(text, o.name, "exec")
        assert text.startswith("# Databricks notebook source\n")
        assert len(res.transforms) == len(o.transforms)


def test_conversion_coverage_on_sample():
    results = [convert_dataflow(estate(), o.name, ConvertConfig())[1] for o in estate().by_kind(ObjectKind.DATAFLOW)]
    statuses = [t.status for r in results for t in r.transforms]
    assert statuses.count("converted") == 205 and statuses.count("todo") == 21


# ---------- job / bundle ----------

def test_bundle_files():
    r = job("JOB_SALES_DAILY")
    assert {"databricks.yml", "resources/job_sales_daily.job.yml", "notebooks/00_setup_tables.py",
            "conversion_report.md", "conversion_report.json"} <= set(r.files)
    assert 'name: "deltabound_job_sales_daily"' in r.files["databricks.yml"]
    for path, text in r.files.items():
        if path.endswith(".py"):
            compile(text, path, "exec")


def test_if_else_becomes_condition_task_with_outcomes():
    t = tasks("JOB_SALES_DAILY")
    cond = t["WF_SALES_DAILY_MAIN__WF_STAGE_SALES__if__1"]["text"]
    assert '"EQUAL_TO"' in cond and '"{{job.parameters.G_FULL_LOAD}}"' in cond and 'right: "Y"' in cond
    assert t["WF_SALES_DAILY_MAIN__WF_STAGE_SALES__DF_STG_ORDERS_FULL"]["deps"] == [("WF_SALES_DAILY_MAIN__WF_STAGE_SALES__if__1", "true")]
    assert t["WF_SALES_DAILY_MAIN__WF_STAGE_SALES__DF_STG_ORDERS_DELTA"]["deps"] == [("WF_SALES_DAILY_MAIN__WF_STAGE_SALES__if__1", "false")]
    after = t["WF_SALES_DAILY_MAIN__WF_STAGE_SALES__DF_STG_ORDER_ITEMS"]
    assert after["run_if"] == "NONE_FAILED" and len(after["deps"]) == 3


def test_complex_condition_is_evaluated_in_a_notebook():
    r, t = job("JOB_SALES_DAILY"), tasks("JOB_SALES_DAILY")
    key = "WF_SALES_DAILY_MAIN__WF_LOAD_SALES_MART__WF_SHARED_DIMENSIONS__eval_if__4"
    nb_text = r.files[f"notebooks/conditions/{key}.py"]
    assert "coalesce((dayofmonth(system.session.G_LOAD_DATE) = 1), false)" in nb_text
    assert '"{{tasks.' + key + '.values.result}}"' in t["WF_SALES_DAILY_MAIN__WF_LOAD_SALES_MART__WF_SHARED_DIMENSIONS__if__4"]["text"]


def test_catch_block_runs_on_failure_and_chains():
    t = tasks("JOB_SALES_DAILY")
    first = t["WF_SALES_DAILY_MAIN__script__1"]
    assert first["run_if"] == "AT_LEAST_ONE_FAILED" and len(first["deps"]) > 5
    assert t["WF_SALES_DAILY_MAIN__DF_CTL_JOB_ERROR"]["deps"] == [("WF_SALES_DAILY_MAIN__script__1", None)]
    assert t["WF_SALES_DAILY_MAIN__script__2"]["deps"] == [("WF_SALES_DAILY_MAIN__DF_CTL_JOB_ERROR", None)]
    # The main path skips the catch block.
    end = t["WF_SALES_DAILY_MAIN__WF_JOB_END__DF_CTL_JOB_END"]["deps"]
    assert all("JOB_ERROR" not in k and "script" not in k for k, _ in end)


def test_scripts_set_task_values_used_downstream():
    r, t = job("JOB_SALES_DAILY"), tasks("JOB_SALES_DAILY")
    script = r.files["notebooks/scripts/JOB_SALES_DAILY__script__1.py"]
    assert 'SELECT CAST(current_timestamp() AS DATE) AS v' in script
    assert 'dbutils.jobs.taskValues.set("G_LOAD_DATE"' in script
    delta = t["WF_SALES_DAILY_MAIN__WF_STAGE_SALES__DF_STG_ORDERS_DELTA"]["text"]
    assert 'G_LOAD_DATE: "{{tasks.JOB_SALES_DAILY__script__1.values.G_LOAD_DATE}}"' in delta


def test_script_statements_raise_and_todo():
    r = job("JOB_SALES_DAILY")
    raise_nb = r.files["notebooks/scripts/WF_SALES_DAILY_MAIN__script__2.py"]
    assert 'raise RuntimeError(spark.sql("""SELECT CAST(system.session.G_ERROR_TEXT AS STRING) AS m""")' in raise_nb
    first = r.files["notebooks/scripts/WF_SALES_DAILY_MAIN__script__1.py"]
    assert "# TODO(deltabound): $G_ERROR_TEXT = error_message()" in first
    assert first.rstrip().endswith('raise NotImplementedError("TODO(deltabound): script:WF_SALES_DAILY_MAIN#1 has '
                                   'statements that need manual conversion")')
    t = next(x for x in r.tasks if x.task_key == "WF_SALES_DAILY_MAIN__script__1")
    assert t.todos == ["$G_ERROR_TEXT: function error_message() has no Spark mapping"]


def test_retry_loop_becomes_max_retries():
    t = tasks("JOB_FX_RATES")
    fx = t["WF_LOAD_FX_RATES__DF_STG_FX_RATES"]["text"]
    assert "max_retries: 2" in fx and "min_retry_interval_millis: 60000" in fx


def test_other_loops_are_todos():
    r = job("JOB_POS_INTRADAY")
    loop = next(x for x in r.tasks if x.task_key == "WF_POS_INTRADAY__DF_STG_POS_TRANSACTIONS")
    assert loop.todos == ["loop not converted, the task runs once: while $L_STORE_IDX <= $L_STORE_COUNT"]
    assert "TODO(deltabound): loop not converted" in tasks("JOB_POS_INTRADAY")["WF_POS_INTRADAY__DF_STG_POS_TRANSACTIONS"]["text"]


def test_missing_object_becomes_failing_task():
    r = job("JOB_DWH_HOUSEKEEPING")
    assert "notebooks/missing/WF_PURGE_AUDIT_LOG.py" in r.files
    missing = next(x for x in r.tasks if x.kind == "missing")
    assert missing.todos == ["WF_PURGE_AUDIT_LOG is not in the export"]


def test_object_called_twice_gets_two_tasks_one_notebook():
    from deltabound.ir import CallStep
    e = bods.parse_files([ROOT / "samples" / "bods_mini" / "retail_demo.xml"])
    e.objects["JOB_SALES_DAILY"].calls.append(CallStep(target="WF_DIMENSIONS", order=3))  # also reached via WF_LOAD_MART
    r = convert_job(e, "JOB_SALES_DAILY", ConvertConfig())
    keys = [x.task_key for x in r.tasks if x.task_key.endswith("DF_DIM_PRODUCT")]
    assert keys == ["WF_LOAD_MART__WF_DIMENSIONS__DF_DIM_PRODUCT", "WF_DIMENSIONS__DF_DIM_PRODUCT"]
    assert sum(1 for p in r.files if p.endswith("DF_DIM_PRODUCT.py")) == 1


def test_setup_notebook_creates_schemas_and_identity_tables():
    setup = job("JOB_CUSTOMER_SYNC").files["notebooks/00_setup_tables.py"]
    assert 'spark.sql("CREATE SCHEMA IF NOT EXISTS ds_dwh.mart")' in setup
    assert "CREATE TABLE IF NOT EXISTS ds_dwh.mart.dim_customer" in setup


def test_job_cluster_option():
    r = convert_job(estate(), "JOB_RETURNS_DAILY", ConvertConfig(compute="job_cluster"))
    y = r.files["resources/job_returns_daily.job.yml"]
    assert 'job_cluster_key: "main"' in y and "${var.node_type_id}" in y
    assert "node_type_id" in r.files["databricks.yml"]


def test_old_ir_without_script_positions_runs_scripts_first_with_a_note():
    old = Estate.model_validate_json((ROOT / "samples" / "ir" / "estate_v0.2.0.json").read_text())
    r = convert_job(old, "JOB_SALES_DAILY", ConvertConfig())
    script = next(x for x in r.tasks if x.kind == "script")
    assert any("script position is not in the IR" in n for n in script.notes)


def test_report_lists_every_todo():
    r = job("JOB_SALES_DAILY")
    md = r.files["conversion_report.md"]
    assert f"**Blocking TODOs: {r.todo_count}.**" in md
    for d in r.dataflows:
        for t in d.transforms:
            for todo in t.todos:
                assert f"- [ ] `{t.cite}`:" in md
    data = json.loads(r.files["conversion_report.json"])
    assert data["todo_count"] == r.todo_count


def test_helpers():
    assert split_statements("$A = 'x;y'; print('hi');") == ["$A = 'x;y'", "print('hi')"]
    assert len(task_key("A" * 200)) == 100 and task_key("a b/c") == "a_b_c"
    assert to_yaml({"a": [{"b": 1, "c": "x"}], "d": True, "e": []}) == 'a:\n  - b: 1\n    c: "x"\nd: true\ne: []'


# ---------- golden output ----------

def test_golden_job_customer_sync():
    """Byte-for-byte expected output. Refresh with DELTABOUND_UPDATE_GOLDEN=1 after reviewing the diff."""
    r = job("JOB_CUSTOMER_SYNC")
    base = GOLDEN / "JOB_CUSTOMER_SYNC"
    if os.environ.get("DELTABOUND_UPDATE_GOLDEN"):
        for rel, text in r.files.items():
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(text)
    expected = {str(p.relative_to(base)): p.read_text() for p in base.rglob("*") if p.is_file()}
    assert expected.keys() == r.files.keys()
    for rel, text in r.files.items():
        assert text == expected[rel], f"{rel} differs from golden output"


# ---------- CLI ----------

def test_cli_convert_job_and_all(tmp_path, capsys):
    cli.main(["convert", str(ESTATE_DIR), "--job", "JOB_RETURNS_DAILY", "--out", str(tmp_path / "one")])
    assert (tmp_path / "one" / "databricks.yml").exists()
    assert "JOB_RETURNS_DAILY: 6/7 transforms converted" in capsys.readouterr().out
    cli.main(["convert", str(ESTATE_DIR), "--all", "--out", str(tmp_path / "all")])
    assert len([p for p in (tmp_path / "all").iterdir() if p.is_dir()]) == 15


def test_cli_convert_rejects_non_job(tmp_path):
    with pytest.raises(SystemExit, match="Not a job"):
        cli.main(["convert", str(ESTATE_DIR), "--job", "DF_FACT_SALES", "--out", str(tmp_path)])
