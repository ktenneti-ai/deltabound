"""Run generated notebooks on local Spark + Delta and check the rows they produce.

Needs the optional extra: pip install -e ".[spark]" and a Java runtime (JAVA_HOME,
/usr/libexec/java_home, or Homebrew openjdk). Skipped otherwise. Slow (Spark start ~10 s).
"""
import datetime as dt
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.spark
pyspark = pytest.importorskip("pyspark")
delta = pytest.importorskip("delta")

from deltabound.convert import ConvertConfig, convert_dataflow, convert_job  # noqa: E402
from deltabound.parsers import bods  # noqa: E402

ROOT = Path(__file__).parent.parent
# Two-level names for the local metastore (Unity Catalog would use catalog.schema).
CFG = ConvertConfig(datastores={"DS_ERP.SAPSR3": "erp", "DS_DWH.STG": "stg", "DS_DWH.MART": "mart",
                                "DS_DWH.CTL": "ctl", "DS_CRM.LOYALTY": "crm", "DS_POS.POS": "pos"})


def _java_home() -> str | None:
    if os.environ.get("JAVA_HOME"):
        return os.environ["JAVA_HOME"]
    try:
        return subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    if shutil.which("brew"):
        prefix = subprocess.run(["brew", "--prefix", "openjdk"], capture_output=True, text=True).stdout.strip()
        home = Path(prefix) / "libexec" / "openjdk.jdk" / "Contents" / "Home"
        if home.exists():
            return str(home)
    return None


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    java = _java_home()
    if not java:
        pytest.skip("no Java runtime found")
    os.environ["JAVA_HOME"] = java
    os.environ["PYSPARK_PYTHON"] = sys.executable  # workers must use this interpreter, not the system python3
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable
    from delta import configure_spark_with_delta_pip
    from pyspark.sql import SparkSession

    builder = (SparkSession.builder.master("local[1]").appName("deltabound-tests")
               .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
               .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
               .config("spark.sql.warehouse.dir", str(tmp_path_factory.mktemp("warehouse")))
               .config("spark.ui.enabled", "false").config("spark.ui.showConsoleProgress", "false")
               .config("spark.sql.shuffle.partitions", "1").config("spark.sql.session.timeZone", "UTC"))
    session = configure_spark_with_delta_pip(builder).getOrCreate()
    session.sparkContext.setLogLevel("ERROR")
    for schema in ("erp", "stg", "mart", "ctl", "crm", "pos"):
        session.sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    yield session
    session.stop()


@lru_cache(maxsize=1)
def estate():
    return bods.parse_files(sorted((ROOT / "samples" / "bods").glob("*.xml")))


class Widgets:
    def __init__(self, params: dict):
        self.params, self.values = params, {}

    def text(self, name, default):
        self.values[name] = self.params.get(name, default)

    def get(self, name):
        return self.values[name]


class TaskValues:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


class DbUtils:
    def __init__(self, params):
        self.widgets = Widgets(params)
        self.jobs = type("Jobs", (), {})()
        self.jobs.taskValues = TaskValues()


def run(spark, source: str, **params) -> DbUtils:
    """Execute generated notebook source as Databricks would (spark + dbutils globals)."""
    dbutils = DbUtils(params)
    exec(compile(source, "<notebook>", "exec"), {"spark": spark, "dbutils": dbutils})
    return dbutils


def run_df(spark, name: str, **params) -> DbUtils:
    return run(spark, convert_dataflow(estate(), name, CFG)[0], **params)


def table(spark, rows, schema: str, name: str):
    spark.sql(f"DROP TABLE IF EXISTS {name}")
    spark.createDataFrame(rows, schema).write.saveAsTable(name)


def rows(spark, sql: str) -> list[tuple]:
    return [tuple(r) for r in spark.sql(sql).collect()]


@pytest.fixture(scope="module")
def setup(spark):
    """Run the generated setup notebooks: they create every target table (with identity keys)."""
    for job in ("JOB_SALES_DAILY", "JOB_LOYALTY_POINTS", "JOB_POS_INTRADAY", "JOB_CUSTOMER_SYNC"):
        run(spark, convert_job(estate(), job, CFG).files["notebooks/00_setup_tables.py"])
    return spark


def test_query_filter_types_and_session_variable(setup):
    spark = setup
    table(spark, [("4711", "20261030", "C1", Decimal("100.456"), "EUR", "1000"),
                  ("4712", "20261031", "C2", Decimal("5.004"), "EUR", "1000"),
                  ("4000", "20261001", "C1", Decimal("1.00"), "EUR", "1000")],
          "VBELN string, ERDAT string, KUNNR string, NETWR decimal(15,3), WAERK string, VKORG string", "erp.vbak")
    spark.sql("DELETE FROM stg.stg_orders")
    run_df(spark, "DF_STG_ORDERS_DELTA", G_LOAD_DATE="2026-10-31")
    got = rows(spark, "SELECT ORDER_ID, ORDER_DATE, NET_AMOUNT FROM stg.stg_orders ORDER BY ORDER_ID")
    assert got == [("4711", dt.date(2026, 10, 30), Decimal("100.46")), ("4712", dt.date(2026, 10, 31), Decimal("5.00"))]


def test_join_and_lookups_with_defaults_and_max_policy(setup):
    spark = setup
    table(spark, [("O1", dt.date(2026, 10, 30), "C1", Decimal("10"), "EUR", "1000"),
                  ("O2", dt.date(2026, 10, 30), "C9", Decimal("10"), "EUR", "1000")],
          "ORDER_ID string, ORDER_DATE date, CUSTOMER_ID string, NET_AMOUNT decimal(15,2), CURRENCY string, "
          "SALES_ORG string", "stg.stg_orders_src")
    spark.sql("DELETE FROM stg.stg_orders")
    spark.sql("INSERT INTO stg.stg_orders SELECT * FROM stg.stg_orders_src")
    table(spark, [("O1", 1, "P1", Decimal("2"), Decimal("20")), ("O2", 1, "P404", Decimal("1"), Decimal("5")),
                  ("O3", 1, "P1", Decimal("1"), Decimal("5"))],
          "ORDER_ID string, LINE_NO int, PRODUCT_ID string, QTY decimal(13,3), LINE_NET decimal(15,2)", "stg.stg_order_items")
    spark.sql("DROP TABLE IF EXISTS mart.dim_customer")
    table(spark, [(1, "C1"), (7, "C1")], "CUSTOMER_KEY bigint, CUSTOMER_ID string", "mart.dim_customer")  # two versions
    spark.sql("DROP TABLE IF EXISTS mart.dim_product")
    table(spark, [(3, "P1")], "PRODUCT_KEY bigint, PRODUCT_ID string", "mart.dim_product")
    spark.sql("DROP TABLE IF EXISTS mart.fact_sales")
    run_df(spark, "DF_FACT_SALES")
    got = rows(spark, "SELECT ORDER_ID, CUSTOMER_KEY, PRODUCT_KEY, LINE_AMOUNT FROM mart.fact_sales ORDER BY ORDER_ID")
    # O3 has no order header (inner join); C1 -> latest key 7 (MAX); C9 and P404 -> default -1.
    assert got == [("O1", 7, 3, Decimal("20.00")), ("O2", -1, -1, Decimal("5.00"))]


def test_case_branches_first_match_nulls_and_merge(setup):
    spark = setup
    table(spark, [("T1", "S001", "P1", Decimal("1"), Decimal("100"), "STORE"),
                  ("T2", "S001", "P1", Decimal("1"), Decimal("100"), "KIOSK"),
                  ("T3", "S001", "P1", Decimal("1"), Decimal("100"), "WEB"),
                  ("T4", "S999", "P1", Decimal("1"), Decimal("100"), None)],
          "TXN_ID string, STORE_CODE string, PRODUCT_ID string, QTY decimal(13,3), AMOUNT decimal(15,2), CHANNEL string",
          "stg.stg_pos_txn")
    table(spark, [(11, "S001")], "STORE_KEY bigint, STORE_CODE string", "mart.dim_store")
    spark.sql("DROP TABLE IF EXISTS mart.fact_pos_sales")
    run_df(spark, "DF_FACT_POS_SALES")
    got = rows(spark, "SELECT TXN_ID, STORE_KEY, AMOUNT, CHANNEL FROM mart.fact_pos_sales ORDER BY TXN_ID")
    assert got == [("T1", 11, Decimal("100.00"), "STORE"), ("T2", 11, Decimal("98.00"), "KIOSK"),
                   ("T3", -1, Decimal("100.00"), "OTHER"), ("T4", -1, Decimal("100.00"), "OTHER")]  # NULL -> default branch


def test_scd2_history_with_identity_keys(setup):
    spark = setup
    target = "mart.dim_customer"
    spark.sql(f"DROP TABLE IF EXISTS {target}")
    run(spark, convert_job(estate(), "JOB_CUSTOMER_SYNC", CFG).files["notebooks/00_setup_tables.py"])
    schema = "CUSTOMER_ID string, CUSTOMER_NAME string, COUNTRY string, CITY string, POSTCODE string"
    table(spark, [("C1", "ANNA", "NL", "utrecht", "1234"), ("C2", "BOB", "DE", "berlin", "1000")], schema, "stg.stg_customer")
    run_df(spark, "DF_DIM_CUSTOMER")
    first = rows(spark, f"SELECT CUSTOMER_ID, CITY, IS_CURRENT FROM {target} ORDER BY CUSTOMER_ID")
    assert first == [("C1", "Utrecht", "1"), ("C2", "Berlin", "1")]

    table(spark, [("C1", "ANNA", "NL", "amsterdam", "1234"), ("C2", "BOB", "DE", "berlin", "1000"),
                  ("C3", "CAS", "FR", "paris", "7500")], schema, "stg.stg_customer")
    run_df(spark, "DF_DIM_CUSTOMER")
    got = rows(spark, f"SELECT CUSTOMER_ID, CITY, IS_CURRENT, VALID_TO = DATE'9999-12-31' FROM {target} "
                      "ORDER BY CUSTOMER_ID, IS_CURRENT")
    assert got == [("C1", "Utrecht", "0", False), ("C1", "Amsterdam", "1", True), ("C2", "Berlin", "1", True),
                   ("C3", "Paris", "1", True)]
    keys = rows(spark, f"SELECT COUNT(DISTINCT CUSTOMER_KEY), COUNT(*), MIN(CUSTOMER_KEY) IS NOT NULL FROM {target}")
    assert keys == [(4, 4, True)]  # identity column filled every version with a distinct key

    run_df(spark, "DF_DIM_CUSTOMER")  # same input again: nothing changes
    assert rows(spark, f"SELECT COUNT(*) FROM {target}") == [(4,)]


def test_scd1_updates_in_place_and_keeps_key(setup):
    spark = setup
    target = "mart.dim_product"
    spark.sql(f"DROP TABLE IF EXISTS {target}")
    run(spark, convert_job(estate(), "JOB_SALES_DAILY", CFG).files["notebooks/00_setup_tables.py"])
    schema = "PRODUCT_ID string, PRODUCT_NAME string, PRODUCT_GROUP string, PRODUCT_TYPE string, UOM string"
    table(spark, [("P1", "Tea", "DRINK", "FERT", "EA")], schema, "stg.stg_product")
    run_df(spark, "DF_DIM_PRODUCT")
    key = rows(spark, f"SELECT PRODUCT_KEY FROM {target}")[0][0]
    table(spark, [("P1", "Green tea", "DRINK", "FERT", "EA"), ("P2", "Cake", "FOOD", "FERT", "EA")], schema, "stg.stg_product")
    run_df(spark, "DF_DIM_PRODUCT")
    got = rows(spark, f"SELECT PRODUCT_ID, PRODUCT_NAME, PRODUCT_KEY = {key} FROM {target} ORDER BY PRODUCT_ID")
    assert got == [("P1", "Green tea", True), ("P2", "Cake", False)]


def test_lookup_transform_and_decimal_variable(setup):
    spark = setup
    table(spark, [("X1", "M1", dt.date(2026, 10, 30), 10), ("X2", "M9", dt.date(2026, 10, 30), 4)],
          "TXN_ID string, MEMBER_ID string, TXN_DATE date, POINTS int", "stg.stg_loyalty_txn")
    spark.sql("DROP TABLE IF EXISTS mart.dim_loyalty_member")
    table(spark, [(5, "M1")], "MEMBER_KEY bigint, MEMBER_ID string", "mart.dim_loyalty_member")
    spark.sql("DROP TABLE IF EXISTS mart.fact_loyalty_points")
    run_df(spark, "DF_FACT_LOYALTY_POINTS", G_POINTS_RATE="1.5")
    got = rows(spark, "SELECT TXN_ID, POINTS, MEMBER_KEY FROM mart.fact_loyalty_points ORDER BY TXN_ID")
    assert got == [("X1", 15, 5), ("X2", 6, None)]  # Lookup transform default is NULL


def test_group_by_aggregation(setup):
    spark = setup
    spark.sql("DROP TABLE IF EXISTS mart.fact_sales")
    table(spark, [("O1", 1, dt.date(2026, 10, 30), 1, 3, Decimal("2"), Decimal("20")),
                  ("O1", 2, dt.date(2026, 10, 30), 1, 3, Decimal("1"), Decimal("5")),
                  ("O2", 1, dt.date(2026, 10, 30), 2, 3, Decimal("1"), Decimal("7")),
                  ("O3", 1, dt.date(2026, 10, 1), 2, 3, Decimal("9"), Decimal("99"))],
          "ORDER_ID string, LINE_NO int, ORDER_DATE date, CUSTOMER_KEY int, PRODUCT_KEY int, QTY decimal(13,3), "
          "LINE_AMOUNT decimal(15,2)", "mart.fact_sales")
    spark.sql("DROP TABLE IF EXISTS mart.agg_sales_daily")
    run_df(spark, "DF_AGG_SALES_DAILY", G_LOAD_DATE="2026-10-31")
    got = rows(spark, "SELECT PRODUCT_KEY, TOTAL_QTY, TOTAL_AMOUNT, ORDER_COUNT FROM mart.agg_sales_daily")
    assert got == [(3, Decimal("4.000"), Decimal("32.00"), 2)]


def test_script_sets_variable_and_task_value(setup):
    nb = convert_job(estate(), "JOB_SALES_DAILY", CFG).files["notebooks/scripts/JOB_SALES_DAILY__script__1.py"]
    dbutils = run(setup, nb)
    assert dbutils.jobs.taskValues.values["G_LOAD_DATE"] == dt.date.today().isoformat()


def test_condition_notebook(setup):
    r = convert_job(estate(), "JOB_SALES_DAILY", CFG)
    path = next(p for p in r.files if p.startswith("notebooks/conditions/"))
    assert run(setup, r.files[path], G_LOAD_DATE="2026-11-01").jobs.taskValues.values["result"] == "true"
    assert run(setup, r.files[path], G_LOAD_DATE="2026-11-02").jobs.taskValues.values["result"] == "false"


def test_todo_steps_raise(setup):
    with pytest.raises(NotImplementedError, match="TODO\\(deltabound\\): df:DF_ECOM_SCORE/PY_BASKET_SCORE"):
        table(setup, [(1,)], "x int", "mart.fact_ecom_sales")
        run_df(setup, "DF_ECOM_SCORE")
