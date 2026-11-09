# Conversion report: JOB_CUSTOMER_SYNC

- Dataflows: 3, transforms: 13, converted: 12 (92%)
- Tasks in the Databricks job: 4
- **Blocking TODOs: 1.** Steps with a TODO raise `NotImplementedError` (notebooks) or are flagged in the task description (job). The job must not run in production until this list is empty.

## Blocking TODOs

- [ ] `df:DF_CUSTOMER_ADDRESS_CLEANSE/VAL_ADDRESS`: no conversion rule for validation

## Review notes

Assumptions the converter made. Check each one against the legacy behaviour.

- Datastore `DS_DWH` is not mapped in `[convert.datastores]`; 5 table reference(s) use the lower-case legacy names. Map it to a Unity Catalog `catalog.schema`.
- Datastore `DS_ERP` is not mapped in `[convert.datastores]`; 1 table reference(s) use the lower-case legacy names. Map it to a Unity Catalog `catalog.schema`.
- `df:DF_DIM_CUSTOMER/TC_CUST`: converted as part of the merge into DIM_CUSTOMER
- `df:DF_DIM_CUSTOMER/HP_CUST`: converted as part of the merge into DIM_CUSTOMER
- `df:DF_DIM_CUSTOMER/KG_CUST`: converted as part of the merge into DIM_CUSTOMER
- `df:DF_DIM_CUSTOMER/DIM_CUSTOMER`: KG_CUST: key CUSTOMER_KEY becomes a Delta identity column (values differ from legacy keys)

## Dataflows

### DF_STG_CUSTOMER

Notebook `notebooks/dataflows/DF_STG_CUSTOMER.py`; writes `ds_dwh.stg.stg_customer`; parameters none.

| Transform | Kind | Rule | Status |
|---|---|---|---|
| `KNA1` | table_source | table_source | converted |
| `Q_CUSTOMER` | query | query | converted |
| `STG_CUSTOMER` | table_target | table_target | converted |

### DF_DIM_CUSTOMER

Notebook `notebooks/dataflows/DF_DIM_CUSTOMER.py`; writes `ds_dwh.mart.dim_customer`; parameters none.

| Transform | Kind | Rule | Status |
|---|---|---|---|
| `STG_CUSTOMER` | table_source | table_source | converted |
| `Q_CUST` | query | query | converted |
| `TC_CUST` | table_comparison | scd_merge | converted |
| `HP_CUST` | history_preserving | scd_merge | converted |
| `KG_CUST` | key_generation | scd_merge | converted |
| `DIM_CUSTOMER` | table_target | scd2_merge | converted |

### DF_CUSTOMER_ADDRESS_CLEANSE

Notebook `notebooks/dataflows/DF_CUSTOMER_ADDRESS_CLEANSE.py`; writes `ds_dwh.mart.customer_address`; parameters none.

| Transform | Kind | Rule | Status |
|---|---|---|---|
| `STG_CUSTOMER` | table_source | table_source | converted |
| `Q_ADDR` | query | query | converted |
| `VAL_ADDRESS` | validation | - | TODO |
| `CUSTOMER_ADDRESS` | table_target | table_target | converted |

## Job tasks

| Task | Kind | From | TODO |
|---|---|---|---|
| `setup_tables` | setup | `job:JOB_CUSTOMER_SYNC` |  |
| `WF_CUSTOMER_SYNC__WF_DIM_CUSTOMER_SCD__DF_STG_CUSTOMER` | dataflow | `call:WF_DIM_CUSTOMER_SCD#1` |  |
| `WF_CUSTOMER_SYNC__WF_DIM_CUSTOMER_SCD__DF_DIM_CUSTOMER` | dataflow | `call:WF_DIM_CUSTOMER_SCD#2` |  |
| `WF_CUSTOMER_SYNC__DF_CUSTOMER_ADDRESS_CLEANSE` | dataflow | `call:WF_CUSTOMER_SYNC#2` |  |
