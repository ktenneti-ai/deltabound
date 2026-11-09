# Design: SSIS parser (`deltabound/parsers/ssis.py`)

Status: IMPLEMENTED 2026-11-09 as IR 0.3.0 (all eight changes, additive). Departures: named
outputs are modelled as Branch names "Component.Output" (unique, so inputs reference them
directly; no dotted-input parsing needed); `convert` refuses SSIS for now (its expression
translator is BODS-specific). Unverified against real packages; constants in parsers/ssis.py. Author: Claude for Kishore Tenneti, 2026-11-09.

## Goal

Parse SQL Server Integration Services `.dtsx` packages into the same `Estate`
IR, to show that the IR is tool-neutral. It ships with a synthetic SSIS sample
and tests.

## How SSIS maps to the IR

| SSIS | IR today | Fit |
|---|---|---|
| Package (`.dtsx`) | `EtlObject(kind=job)` | good |
| Sequence / For Loop / Foreach Loop container | `kind=workflow`; loop shown on the call (`control="loop"`) | good |
| Data Flow Task (`Microsoft.Pipeline`) | `kind=dataflow` | good |
| Execute Package Task | `CallStep` job → job | good (enum already allows it) |
| Execute SQL / Script / File System / Send Mail task | none (only `EtlObject.scripts`, which has no position) | **gap 1** |
| Precedence constraints (success / failure / completion, with expressions) | sequential `order` only | **gap 2** |
| OnError event handler | `control="error_handler"` | good |
| Variables `User::X`, `$Package::P`, `$Project::P`, expression-bound | `Variable` (scope is free text) | **gap 3** (expressions) |
| Connection managers | `TableRef.datastore` = name only | **gap 4** |
| OLE DB Source / Destination, Flat File Source / Destination | table/file source and target | good, but SQL-command sources are **gap 5** |
| Derived Column | columns with expressions | **gap 6** (pass-through) |
| Conditional Split | `kind="case"` with `branches` | good |
| Lookup (match / no-match outputs) | `kind="lookup"`, `lookups`, `branches` | good, if named outputs are referenced (**gap 7**) |
| Merge Join | `joins` exists, but on a query | new kind `join` (no schema change) |
| Union All / Merge | `kind="merge"` | good |
| Aggregate | `group_by` plus aggregate expressions | new kind `aggregate` (no schema change) |
| Sort | `order_by` | new kind `sort` (no schema change) |
| Data Conversion, Multicast, Row Count | new kinds | no schema change |
| Slowly Changing Dimension wizard | similar to TC + HP | kind `scd`; detailed modelling later |
| Script Component, OLE DB Command, Fuzzy Lookup... | `opaque` (or `sql_command`) | good |
| Column identity by lineageID | names "Component.Column" | parser resolves; no change |

## Proposed IR changes (IR 0.3.0, all additive)

Every new field has a default, so 0.1.0 and 0.2.0 JSON still load. I'll add
`samples/ir/estate_v0.2.0.json` as a compatibility fixture.

1. **`ObjectKind.TASK` with `EtlObject.task_type: Optional[str]`.**
   - Covers executable steps that are not dataflows (`"sql"`, `"script"`,
     `"file"`, `"mail"`, `"process"`, ...). Their body text goes in `scripts`.
   - Execute SQL tasks are everywhere in SSIS estates. Hiding them inside
     another object would lose their position in the flow.
   - BODS scripts could move to this model later, but not as part of this
     change.
2. **`CallStep.depends_on: Optional[list[int]] = None` and
   `CallStep.trigger: Optional[str] = None`.**
   - `None` keeps today's meaning: the step follows the previous step, as in
     BODS.
   - A list gives the `order` numbers of the steps it waits for, which models
     SSIS precedence graphs including parallel branches.
   - `trigger` is `"success"`, `"failure"` or `"completion"`. Failure
     constraints also set `control="error_handler"`.
   - `tree_lines` keeps working; a later change could show parallel branches.
3. **`Variable.expression: Optional[str] = None`** for variables computed from
   an expression (SSIS `EvaluateAsExpression`). Scope values are documented:
   `global | local | parameter | package | project | system`.
4. **`Estate.datastores: dict[str, Datastore] = {}`**, where
   `Datastore(name, type, properties: dict[str, str])`.
   - Holds connection managers (and BODS datastores later): provider, server,
     database, file path.
   - The parser never stores passwords or connection-string secrets; it strips
     `Password=` and similar.
5. **`Transform.sql: Optional[str] = None`** for sources and commands that run
   free SQL. This covers the SSIS "SQL command" access mode and also suits the
   BODS SQL transform. Lineage does not parse SQL (it is reported as a gap).
6. **`Transform.passes_through: bool = False`.**
   - SSIS components pass every upstream column on unless they drop it, whereas
     BODS queries output only their mapped columns.
   - When true, lineage carries over upstream columns the transform does not
     redefine.
   - `graph.py` uses this flag. For IR 0.2.0 data it falls back to today's
     `PASS_THROUGH_KINDS` set, so BODS behaviour stays the same.
7. **Named outputs in `inputs`.**
   - An input may be `"transform"` or `"transform.output"`. SSIS output names
     like "Lookup No Match Output" repeat on every component, so the component
     name is needed.
   - `Branch` is documented as "named output, optional condition", so lookup
     no-match outputs and error outputs are branches too.
   - `graph.column_lineage` learns to resolve the dotted form.
8. **Documented transform-kind vocabulary.**
   - `ir.py` gets a `TRANSFORM_KINDS` docstring/list: `join`, `aggregate`,
     `sort`, `derived_column`, `data_conversion`, `multicast`, `row_count`,
     `scd` and `sql_command`, next to the BODS ones.
   - Kinds stay free strings.
   - `COMPLEXITY_WEIGHTS` gets entries for the new kinds.

**Not changing:** `ObjectKind` values for job, workflow and dataflow, column
mapping, `TableRef`, or lookups.

## Parser

- Uses the standard library XML parser with the DTS namespaces
  (`www.microsoft.com/SqlServer/Dts`). Supports the package format of SSIS 2012
  and later (PackageFormatVersion 6 and up).
- `TAG_*`, `COMPONENT_KINDS` (componentClassID to kind) and `TASK_TYPES`
  (ExecutableType to task type) are constants, as in BODS. Unknown components
  become `opaque` and unknown executables are listed in `Estate.unparsed`.
- **Object names:**
  - Packages use the package name.
  - Containers, tasks and data flows use `<Package>\<path>`, for example
    `PKG_SALES\SEQ_STAGE\DFT_ORDERS`, because SSIS names are only unique within
    their parent.
  - A short display name goes in `description`.
- Expressions in the SSIS expression language (`[Col] * 2`, `(DT_STR,10,1252)`,
  `ISNULL(...)`) are stored as they are. Source columns are extracted for
  lineage from `[Column]` and `#{lineageId}` references.
- `parse_files` accepts `.dtsx` files and folders.
- **CLI:** `_load` picks the parser by file extension (`.xml` for BODS, `.dtsx`
  for SSIS) and refuses to mix tools in one estate. A `--tool` override is
  available.

## Synthetic sample (`samples/ssis/`, fictional Fernhill Retail)

- `MASTER_NIGHTLY.dtsx`: runs the child packages through Execute Package
  tasks, with an OnError handler and a failure constraint that sends mail.
- `PKG_SALES.dtsx`:
  - a Sequence container
  - a Foreach Loop over POS files
  - a data flow that runs: Flat File Source, Derived Column, Lookup (with a
    no-match output), Conditional Split, Union All, OLE DB Destination
  - Execute SQL truncate tasks, and a parallel branch
- `PKG_FINANCE.dtsx`:
  - Merge Join, Aggregate and Sort
  - an OLE DB source in SQL-command mode
  - a Script Component (opaque)
  - an expression-bound variable
- `PKG_OLD_EXPORT.dtsx`: a package no master calls (dead code), and a call to
  a missing child package.

## Tests (`tests/test_ssis_parser.py`)

- Counts and object kinds, including tasks.
- Precedence: `depends_on` and `trigger`, a parallel branch, and a failure
  path.
- Foreach loop control, and the OnError handler.
- Variable expressions; connection managers in `datastores` with no secrets.
- Derived Column pass-through lineage from the destination back to the flat
  file column.
- Conditional Split branches, the Lookup no-match output, Merge Join, Aggregate
  and Sort.
- The SQL-command source sets `Transform.sql`.
- Opaque Script Component; dead code and missing package.
- **`report` works on the SSIS estate unchanged.** This is the tool-neutrality
  check.
- The IR 0.2.0 fixture still loads, and BODS results are unchanged.

## Open questions

1. Do you approve IR changes 1 to 8? Changes 1, 2 and 6 matter most.
2. Should the name separator be `\` (native SSIS) or `/`? I propose `\` for
   fidelity.
