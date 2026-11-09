import type { LoadEntry } from "../types";

export function LoadReport({ entries }: { entries: LoadEntry[] }) {
  const problems = entries.filter((e) => e.status !== "ok").length;
  return (
    <div className="page">
      <div className="header-row">
        <h1>Load report</h1>
        <span className="muted">
          {entries.length} file(s), {problems} with problems
        </span>
      </div>
      <div className="card" style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>File</th>
              <th>Format</th>
              <th>Status</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, i) => (
              <tr key={i}>
                <td className="mono">{e.file}</td>
                <td>{e.format.replace("_", " ")}</td>
                <td>
                  <span className={`badge ${e.status}`}>{e.status}</span>
                </td>
                <td>{e.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
