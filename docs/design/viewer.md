# Design: static web viewer (`viewer/`)

Status: PLAN (revised 2026-11-09: must read raw export files, not only JSON), awaiting approval. Author: Claude for Kishore Tenneti, 2026-11-09.

## Goal

A static web app (no back end) that opens **any supported file**, either the
`estate.json` written by `deltabound export` or the legacy tool's raw export
files (BODS `.xml` today), and shows:

1. Expandable job trees with call conditions.
2. A column lineage view per dataflow.
3. Highlighted dead code and missing references.

It must work from a static host or `npx vite preview`, and when opened offline.

## Stack

- **Vite + React 18 + TypeScript**, as requested.
- **React Flow (`@xyflow/react` 12)** for the dataflow and lineage graphs.
- **`@dagrejs/dagre`** for automatic left-to-right layout. React Flow does not
  lay out graphs itself. **New dependency: needs your approval.**
- **Pyodide** (Python compiled to WebAssembly, pinned version) runs the real
  `deltabound` parsers and `graph.py` inside the browser. See the next section
  for why. **New dependency: needs your approval.**
- **Vitest** for unit tests. No UI kit; plain CSS modules with light and dark
  themes.

`vite.config.ts` uses `base: './'` so the built `dist/` works from any path.

## Reading raw XML without a back end

Supporting XML means parsing BODS exports in the browser. There are three ways
to do that:

| Option | How | Verdict |
|---|---|---|
| **A. Pyodide (recommended)** | Run the existing Python parser and analysis in the browser | One parser and one analysis for CLI and viewer; nothing to keep in sync |
| B. Port to TypeScript | Re-write `parsers/bods.py` and `graph.py` in TS | Two parsers. Every `TAG_*` correction from a real export must be made twice; lineage logic drifts |
| C. Convert first | User runs `deltabound export`, viewer reads JSON only | Does not meet the requirement |

**With option A:**

- **Packaging.**
  - A build step (`npm run build:py`) packs the `deltabound` package as a
    pure-Python wheel into `viewer/public/`.
  - Pyodide loads `pydantic` and `networkx` from its own package set, and then
    the deltabound wheel.
- **No drift.** The viewer calls the same `bods.parse_files`, `graph.*` and
  `report.job_stats` functions as the CLI. Fixes to the parser reach the viewer
  on the next build.
- **Complexity and waves come for free.** They are computed by the real code,
  so the TypeScript analysis port and its parity tests from the first plan are
  dropped.
- **Cost.**
  - A first load of about 10 to 15 MB, cached by the browser after that. A
    loading screen shows progress.
  - Parsing the 60-dataflow sample should take about a second; I'll measure it.
- **Where Pyodide comes from.** By default from the jsDelivr CDN at a pinned
  version. For offline or air-gapped clients, `npm run build -- --offline`
  copies the Pyodide files into `dist/` so the app needs no internet.
- **Privacy.** Files are read with the browser File API and processed in the
  page. Nothing is uploaded anywhere.

## Supported inputs

Users can drop files or folders, use a file picker (multiple files or a
folder), or pass `?src=<url>` for hosted demos. The viewer detects the format
from content, not just the file extension:

| Input | Detected by | Handling |
|---|---|---|
| `estate.json` (IR 0.1.x, 0.2.x) | JSON with `ir_version` | loaded with `Estate.model_validate_json` |
| BODS XML export | XML root `DataIntegratorExport` | `bods.parse_files`; several files merge into one estate |
| A folder of mixed files | per file | each file routed as above; tools may not be mixed |
| `.zip` of any of the above | zip signature | unpacked with Python `zipfile` in Pyodide (no new JS dependency) |
| SSIS `.dtsx` | XML root `DTS:Executable` | recognised, with the message "SSIS support is planned" (SSIS work is deferred) |
| BODS `.atl` (text export) | `.atl` content | recognised, with the message "ATL is not supported yet; export as XML" |
| anything else | none | error naming the file and listing supported formats |

Parser warnings (`Estate.unparsed`) and per-file errors appear in a "Load
report" panel. A bad file never stops the others from loading.

**Note on `.atl`:** many real BODS repositories are exported as ATL text files
rather than XML. Supporting ATL would be a new parser, and it is worth checking
which format your design partners can provide.

**Demo.** `npm run demo:data` copies `samples/bods/*.xml` into
`viewer/public/demo/`, and a "Load demo" button opens them. The demo goes
through the XML path, so it also exercises the parser.

## Keeping the viewer in sync with the IR

- New CLI command `deltabound schema` writes the IR JSON Schema
  (`Estate.model_json_schema()`) to `viewer/src/ir.schema.json`.
- `npm run gen:types` turns that schema into `src/ir.ts` with
  `json-schema-to-typescript` (a dev dependency).
- A pytest test fails if the committed schema is out of date with `ir.py`.
- **The bridge.** `src/py/bridge.py` runs in Pyodide and returns one JSON
  payload to the UI: the estate, the analysis (call trees, dead code, missing
  references, job stats, waves) and the load report. Column lineage is
  computed on demand per dataflow through the same bridge.

## Screens

```
+----------------------------------------------------------------------+
| DeltaBound  [Load estate.json] [Load demo]   source 14.3 · IR 0.2.0  |
+------------------+---------------------------------------------------+
| Jobs (15)  [find]| JOB_SALES_DAILY                                   |
|  JOB_SALES_DAILY | v WF_SALES_DAILY_MAIN                              |
|  JOB_FINANCE_GL  |   > WF_JOB_START                                   |
|  ...             |   v WF_STAGE_SALES                                 |
| Dead code (6)    |       DF_STG_ORDERS_FULL   [branch] $G_FULL_LOAD='Y'|
|  WF_ARCHIVE_2018 |       DF_STG_ORDERS_DELTA  [branch] NOT (...)      |
| Missing (1)      |   v WF_LOAD_SALES_MART ...                         |
|  WF_PURGE_AUDIT..|     DF_CTL_JOB_ERROR       [on error] ALL          |
| Coverage 97%     |                                                   |
+------------------+---------------------------------------------------+
```

1. **Job tree.** This is a collapsible tree, not a graph, because it reads
   better for nesting 6 levels deep.
   - Badges are coloured by `control`: branch, loop and error handler. The full
     condition shows on hover.
   - A shared dataflow shows a badge such as "used by 4 jobs".
   - Missing references show in red.
   - Expand all and collapse all buttons. Keyboard navigation follows the ARIA
     tree pattern.
2. **Dataflow view** (click any dataflow).
   - A React Flow graph of the transforms from sources to targets.
   - Nodes are coloured by kind. Opaque transforms are hatched and labelled
     "not modeled".
   - The side panel shows the selected transform's columns, expressions, joins,
     GROUP BY, ORDER BY, branches, lookups and options.
3. **Column lineage.** Pick a target column, or click any column. Its
   upstream path is highlighted in the dataflow graph and listed in the panel.
   Lookup tables appear as external source nodes.
4. **Dead code.** Unreachable objects get their own list and a struck-through
   style wherever they appear. The "All objects" view can show only dead code.
   Missing references are listed with their callers.
5. **Unparsed items** from `Estate.unparsed`, with links to the matching
   dataflow.

State lives in the URL hash (for example `#/job/JOB_X` or `#/df/DF_Y?col=...`),
so views can be bookmarked and shared.

## Layout

```
viewer/
  package.json  vite.config.ts  tsconfig.json  index.html
  src/
    main.tsx  App.tsx  ir.ts (generated)  ir.schema.json (generated)
    py/bridge.py      # runs in Pyodide: detect format, parse, analyse, return JSON
    pyodide.ts        # load Pyodide + deltabound wheel, call bridge
    load.ts           # file/folder drop, zip, ?src=, format detection
    components/ JobTree.tsx  DataflowGraph.tsx  LineagePanel.tsx  DeadCode.tsx ...
  test/ load.test.ts  bridge.test.ts
```

## Tests and done criteria

- pytest: `bridge.py` runs under normal Python too, so format detection, mixed
  folders, zip files, bad files, the `.dtsx` and `.atl` messages, and the JSON
  payload are tested in the existing suite.
- Vitest: format sniffing in `load.ts` and the UI state logic.
- A browser smoke test, driven by me, loads the demo XML, a JSON export, a zip,
  and a bad file, and checks every screen.
- `npm run build` gives a static `dist/`. I then drive the app in a browser on
  the demo estate and check each screen.
- README section on viewer quick start.

## Open questions

1. Can I add Pyodide (option A), `@dagrejs/dagre`, and `json-schema-to-typescript`
   as a dev dependency?
2. Explanations from `explain/` will show in the viewer: `explanations.json` is
   one more supported input type.
