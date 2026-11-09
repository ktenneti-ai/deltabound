# Demo source: SQL Server Integration Services (Halden Outfitters)

SYNTHETIC. Halden Outfitters is a fictional company; no client data.

| Path | What |
|---|---|
| `deltabound.toml` | All settings for this source |
| `exports/` | 4 packages: `MASTER_NIGHTLY` (runs the others in parallel, failure mail, OnError handler), `PKG_SALES` (derived columns, lookup with a no-match output, conditional split, union, Foreach loop over files, sort, aggregate), `PKG_FINANCE` (merge join, a Script Component, a conditional SQL task), `PKG_OLD_EXPORT` (calls a package that is not in the export) |
| `generate.py` | Regenerates `exports/` |

```bash
deltabound summary  demo/ssis
deltabound tree     demo/ssis --job MASTER_NIGHTLY
deltabound lineage  demo/ssis --dataflow "PKG_SALES\DFT Sales Lines" --column DWH.stg.SalesLines.LineAmount
deltabound viewer   demo/ssis
deltabound explain  demo/ssis --job PKG_SALES --plan
```

How SSIS maps to the IR: packages are jobs, containers are workflows (Foreach and For loops
are marked as loops), data flow tasks are dataflows, other tasks are `task` objects, precedence
constraints become `depends_on` + `trigger`, connection managers become datastores (passwords
removed). `deltabound convert` does not handle SSIS yet: its expression rules are for SAP
Data Services syntax, so it refuses SSIS instead of mistranslating.
