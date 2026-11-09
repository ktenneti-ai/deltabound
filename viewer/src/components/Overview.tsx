import { href } from "../route";
import type { Payload } from "../types";

export function Overview({ payload }: { payload: Payload }) {
  const a = payload.analysis!;
  const estate = payload.estate!;
  const waves = new Map<number, typeof a.jobs>();
  for (const j of [...a.jobs].sort((x, y) => x.score - y.score)) {
    waves.set(j.wave, [...(waves.get(j.wave) ?? []), j]);
  }
  const stats: [string, number | string][] = [
    ["Jobs", a.counts.job],
    ["Workflows", a.counts.workflow],
    ["Dataflows", a.counts.dataflow],
    ["Transforms", a.counts.transforms],
    ["Coverage", `${a.coverage}%`],
    ["Dead objects", a.unreachable.length],
    ["Missing references", Object.keys(a.missing).length],
  ];
  return (
    <div className="page">
      <div className="header-row">
        <h1>Estate overview</h1>
        <span className="muted">
          {estate.source_tool} {estate.source_version ?? ""}
        </span>
      </div>
      <div className="stats">
        {stats.map(([label, value]) => (
          <div className="stat" key={label}>
            <div className="value">{value}</div>
            <div className="label">{label}</div>
          </div>
        ))}
      </div>

      <section className="card">
        <h2>Most complex jobs</h2>
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th className="num">Score</th>
              <th className="num">Dataflows</th>
              <th className="num">Call depth</th>
              <th className="num">Wave</th>
            </tr>
          </thead>
          <tbody>
            {a.jobs.slice(0, 5).map((j) => (
              <tr key={j.job}>
                <td>
                  <a className="mono" href={href({ view: "job", name: j.job, tab: "tree" })}>{j.job}</a>
                </td>
                <td className="num">{j.score}</td>
                <td className="num">{j.dataflows}</td>
                <td className="num">{j.depth}</td>
                <td className="num">{j.wave}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Migration waves</h2>
        <p className="muted">Jobs easiest first, 10 per wave. Score combines transforms, conditions, scripts and nesting.</p>
        {[...waves.entries()].map(([wave, jobs]) => (
          <div key={wave} style={{ marginTop: 8 }}>
            <h3>Wave {wave}</h3>
            <p style={{ margin: "4px 0", display: "flex", gap: 6, flexWrap: "wrap" }}>
              {jobs.map((j) => (
                <a key={j.job} className="chip" href={href({ view: "job", name: j.job, tab: "tree" })}>
                  {j.job} · {j.score}
                </a>
              ))}
            </p>
          </div>
        ))}
      </section>

      <section className="card">
        <h2>Parser coverage</h2>
        <p>
          <strong>{a.coverage}%</strong> of transforms modeled ({a.counts.transforms - a.counts.opaque} of {a.counts.transforms};{" "}
          {a.counts.opaque} not modeled).
        </p>
        {estate.unparsed.length > 0 ? (
          <ul className="mono" style={{ fontSize: 12.5, paddingLeft: 18 }}>
            {estate.unparsed.map((u, i) => {
              const df = u.split(/[:.]/, 1)[0];
              return (
                <li key={i}>
                  {estate.objects[df]?.kind === "dataflow" ? <a href={href({ view: "df", name: df })}>{u}</a> : u}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">Nothing was left unparsed.</p>
        )}
      </section>
    </div>
  );
}
