# Demo sources

One folder per legacy source type. Each folder holds everything for that source, and its
`deltabound.toml` holds all settings (parser, datastore mapping, conversion, AI limits,
validation). Every command takes the folder.

| Folder | Source | Status |
|---|---|---|
| [`sap_bods/`](sap_bods/) | SAP Data Services repository exports (XML) | supported, full demo |
| `ssis/` | SQL Server Integration Services packages (.dtsx) | parser in progress |
| [`sql_server/`](sql_server/) | SQL Server Agent jobs + T-SQL | planned |

```
demo/<source>/
  deltabound.toml   [source] type and exports, [convert], [explain] (+ [explain.limits]), [validate]
  README.md         what the source is and how to run it
  exports/          the legacy tool's export files
  validation/       validation manifests, waivers, saved legacy and Databricks outputs
```

All demo content is SYNTHETIC (fictional companies). Never add client or employer data.
