# Demo source: SAP Data Services (Fernhill Retail)

SYNTHETIC. Fernhill Retail is a fictional company; no client data.

| Path | What |
|---|---|
| `deltabound.toml` | All settings for this source: parser, datastore mapping, conversion, AI explanations, validation |
| `exports/` | BODS repository exports (7 XML files: 15 jobs, 30 workflows, 60 dataflows) |
| `validation/JOB_SALES_DAILY/` | Validation manifest, waivers, saved legacy (CSV) and Databricks (Parquet) outputs |
| `validation/generate.py` | Regenerates the saved outputs |

```bash
deltabound summary  demo/sap_bods
deltabound tree     demo/sap_bods --job JOB_SALES_DAILY
deltabound report   demo/sap_bods --out estate_report.md
deltabound convert  demo/sap_bods --job JOB_SALES_DAILY --out build/JOB_SALES_DAILY
deltabound validate run demo/sap_bods --out validation_out
deltabound viewer   demo/sap_bods
```
