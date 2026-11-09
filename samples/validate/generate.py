"""Generate the SYNTHETIC validation sample for JOB_SALES_DAILY (fictional Fernhill Retail).

legacy/      CSV exports as the legacy job would leave them (';' delimited, text, '' for NULL)
databricks/  Parquet outputs of the converted pipeline
Run: .venv/bin/python samples/validate/generate.py   (needs duckdb)
"""
from pathlib import Path

import duckdb

HERE = Path(__file__).parent / "JOB_SALES_DAILY"
(HERE / "legacy").mkdir(parents=True, exist_ok=True)
(HERE / "databricks").mkdir(parents=True, exist_ok=True)
con = duckdb.connect()


def legacy(name: str, header: str, rows: list[str]) -> None:
    (HERE / "legacy" / f"{name}.csv").write_text(header + "\n" + "\n".join(rows) + "\n")


def new(name: str, sql: str) -> None:
    con.execute(f"COPY ({sql}) TO '{HERE / 'databricks' / name}.parquet' (FORMAT parquet)")


# 1. STG_ORDERS: equal within tolerance (0.004 rounding, trailing space, '' vs NULL) -> PASS
legacy("stg_orders", "ORDER_ID;ORDER_DATE;CUSTOMER_ID;NET_AMOUNT;CURRENCY;SALES_ORG", [
    "O1;2026-10-30;C1 ;100.004;EUR;1000", "O2;2026-10-30;C2;55.50;EUR;", "O3;2026-10-31;C1;12.00;EUR;1000"])
new("stg_orders", """SELECT * FROM (VALUES
    ('O1', DATE '2026-10-30', 'C1', 100.00::DECIMAL(15,2), 'EUR', '1000'),
    ('O2', DATE '2026-10-30', 'C2', 55.50::DECIMAL(15,2), 'EUR', NULL),
    ('O3', DATE '2026-10-31', 'C1', 12.00::DECIMAL(15,2), 'EUR', '1000'))
    t(ORDER_ID, ORDER_DATE, CUSTOMER_ID, NET_AMOUNT, CURRENCY, SALES_ORG)""")

# 2. STG_ORDER_ITEMS: one LINE_NET off by 0.02 (waived), one row only in legacy, one only in Databricks -> FAIL
legacy("stg_order_items", "ORDER_ID;LINE_NO;PRODUCT_ID;QTY;LINE_NET", [
    "O1;10;P1;2.000;60.00", "O1;20;P2;1.000;40.00", "O2;10;P1;1.000;55.50", "O3;10;P3;3.000;12.00"])
new("stg_order_items", """SELECT * FROM (VALUES
    ('O1', 10, 'P1', 2.000::DECIMAL(13,3), 60.00::DECIMAL(15,2)),
    ('O1', 20, 'P2', 1.000::DECIMAL(13,3), 40.02::DECIMAL(15,2)),
    ('O2', 10, 'P1', 1.000::DECIMAL(13,3), 55.50::DECIMAL(15,2)),
    ('O4', 10, 'P9', 1.000::DECIMAL(13,3), 9.99::DECIMAL(15,2)))
    t(ORDER_ID, LINE_NO, PRODUCT_ID, QTY, LINE_NET)""")

# 3. FACT_SALES: reads STG_ORDER_ITEMS (failed) -> cascading. Surrogate keys differ by design (ignored).
legacy("fact_sales", "ORDER_ID;LINE_NO;ORDER_DATE;CUSTOMER_KEY;PRODUCT_KEY;QTY;LINE_AMOUNT", [
    "O1;10;2026-10-30;501;9001;2.000;60.00", "O1;20;2026-10-30;501;9002;1.000;40.00",
    "O2;10;2026-10-30;502;9001;1.000;55.50"])
new("fact_sales", """SELECT * FROM (VALUES
    ('O1', 10, DATE '2026-10-30', 1, 1, 2.000::DECIMAL(13,3), 60.00::DECIMAL(15,2)),
    ('O1', 20, DATE '2026-10-30', 1, 2, 1.000::DECIMAL(13,3), 40.02::DECIMAL(15,2)),
    ('O2', 10, DATE '2026-10-30', 2, 1, 1.000::DECIMAL(13,3), 55.50::DECIMAL(15,2)))
    t(ORDER_ID, LINE_NO, ORDER_DATE, CUSTOMER_KEY, PRODUCT_KEY, QTY, LINE_AMOUNT)""")

# 4. DIM_CUSTOMER (SCD2): history rows and keys differ, current rows equal -> PASS with the IS_CURRENT filter
legacy("dim_customer", "CUSTOMER_KEY;CUSTOMER_ID;CUSTOMER_NAME;COUNTRY;CITY;VALID_FROM;VALID_TO;IS_CURRENT", [
    "501;C1;ANNA;NL;Utrecht;2024-01-01;2026-03-01;0", "777;C1;ANNA;NL;Amsterdam;2026-03-01;9999-12-31;1",
    "502;C2;BOB;DE;Berlin;2024-01-01;9999-12-31;1"])
new("dim_customer", """SELECT * FROM (VALUES
    (1, 'C1', 'ANNA', 'NL', 'Utrecht', DATE '2026-11-01', DATE '2026-11-02', '0'),
    (3, 'C1', 'ANNA', 'NL', 'Amsterdam', DATE '2026-11-02', DATE '9999-12-31', '1'),
    (2, 'C2', 'BOB', 'DE', 'Berlin', DATE '2026-11-01', DATE '9999-12-31', '1'))
    t(CUSTOMER_KEY, CUSTOMER_ID, CUSTOMER_NAME, COUNTRY, CITY, VALID_FROM, VALID_TO, IS_CURRENT)""")

# 5. AGG_SALES_DAILY (no keys): rows equal, but TOTAL_AMOUNT has 1 decimal where legacy has 2 -> schema FAIL
legacy("agg_sales_daily", "ORDER_DATE;PRODUCT_KEY;TOTAL_QTY;TOTAL_AMOUNT;ORDER_COUNT", [
    "2026-10-30;9001;3.000;115.50;2", "2026-10-30;9002;1.000;40.00;1"])
new("agg_sales_daily", """SELECT * FROM (VALUES
    (DATE '2026-10-30', 9001, 3.000::DECIMAL(15,3), 115.5::DECIMAL(17,1), 2),
    (DATE '2026-10-30', 9002, 1.000::DECIMAL(15,3), 40.0::DECIMAL(17,1), 1))
    t(ORDER_DATE, PRODUCT_KEY, TOTAL_QTY, TOTAL_AMOUNT, ORDER_COUNT)""")

# 6. DIM_PRODUCT: duplicate business key on Databricks (values skipped), extra audit column -> FAIL
legacy("dim_product", "PRODUCT_KEY;PRODUCT_ID;PRODUCT_NAME;PRODUCT_GROUP;PRODUCT_TYPE", [
    "9001;P1;Tea;DRINK;FERT", "9002;P2;Cake;FOOD;FERT"])
new("dim_product", """SELECT * FROM (VALUES
    (1, 'P1', 'Tea', 'DRINK', 'FERT', TIMESTAMP '2026-11-02 01:00:00'),
    (2, 'P2', 'Cake', 'FOOD', 'FERT', TIMESTAMP '2026-11-02 01:00:00'),
    (3, 'P2', 'Cake', 'FOOD', 'FERT', TIMESTAMP '2026-11-02 01:05:00'))
    t(PRODUCT_KEY, PRODUCT_ID, PRODUCT_NAME, PRODUCT_GROUP, PRODUCT_TYPE, LOAD_TS)""")
print(f"wrote {HERE}")
