# Demo source: SQL Server Agent jobs and T-SQL (planned)

Status: **planned**. The folder and manifest are ready; the parser is not built yet, so
`deltabound summary demo/sql_server` explains that instead of guessing.

Input this source will read (decided 2026-11-09):

- SQL Server Agent job definitions, scripted from SSMS (*Script Job as > CREATE To*): job,
  steps, step order, on-success/on-failure actions, schedules.
- The T-SQL the steps run: stored procedures and scripts (`.sql`), parsed into dataflows
  (INSERT/MERGE ... SELECT become source -> query -> target), with procedural code kept
  visible as opaque steps.

Put SYNTHETIC files only into `exports/`, never client or employer code.
