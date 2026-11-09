import { href } from "../route";
import type { Payload } from "../types";

export function DeadCode({ payload }: { payload: Payload }) {
  const a = payload.analysis!;
  const estate = payload.estate!;
  const objHref = (name: string) =>
    estate.objects[name]?.kind === "dataflow" ? href({ view: "df", name }) : href({ view: "job", name, tab: "tree" });
  return (
    <div className="page">
      <h1>Dead code</h1>
      <section className="card">
        <h2>Never called by any job ({a.unreachable.length})</h2>
        <p className="muted">Candidates to skip migrating. Confirm with the owners before dropping them.</p>
        {a.unreachable.length ? (
          <table>
            <thead>
              <tr>
                <th>Object</th>
                <th>Kind</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {a.unreachable.map((n) => (
                <tr key={n}>
                  <td>
                    <a className="mono" href={objHref(n)}>{n}</a>
                  </td>
                  <td>{estate.objects[n].kind}</td>
                  <td className="muted">{estate.objects[n].description ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p>None.</p>
        )}
      </section>
      <section className="card">
        <h2>Called but not in the export ({Object.keys(a.missing).length})</h2>
        <p className="muted">Export these objects too, or confirm they are obsolete.</p>
        {Object.keys(a.missing).length ? (
          <table>
            <thead>
              <tr>
                <th>Missing object</th>
                <th>Called by</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(a.missing).map(([n, callers]) => (
                <tr key={n}>
                  <td className="mono">
                    <span className="badge missing">missing</span> {n}
                  </td>
                  <td>
                    {callers.map((c) => (
                      <a key={c} className="mono" href={objHref(c)} style={{ marginRight: 8 }}>
                        {c}
                      </a>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p>None.</p>
        )}
      </section>
    </div>
  );
}
