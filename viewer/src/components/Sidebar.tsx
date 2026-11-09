import { useMemo, useState } from "react";
import { href, type Route } from "../route";
import type { Payload } from "../types";

export function Sidebar({ payload, route }: { payload: Payload; route: Route }) {
  const [q, setQ] = useState("");
  const a = payload.analysis!;
  const estate = payload.estate!;
  const needle = q.trim().toUpperCase();
  const jobs = useMemo(() => [...a.jobs].sort((x, y) => x.job.localeCompare(y.job)), [a.jobs]);
  const dataflows = useMemo(
    () => Object.values(estate.objects).filter((o) => o.kind === "dataflow").map((o) => o.name).sort(),
    [estate],
  );
  const dead = new Set(a.unreachable);
  const match = (name: string) => !needle || name.toUpperCase().includes(needle);
  const current = (r: Route) => (href(r) === href(route) ? "page" : undefined);
  const isCurrentObject = (name: string) =>
    (route.view === "job" || route.view === "df") && route.name === name ? "page" : undefined;
  const problems = payload.load_report.filter((e) => e.status !== "ok").length;
  const missing = Object.keys(a.missing).length;

  return (
    <nav className="sidebar" aria-label="Estate">
      <ul className="nav-list nav-top">
        <li>
          <a href={href({ view: "overview" })} aria-current={current({ view: "overview" })}>
            Overview
          </a>
        </li>
        <li>
          <a href={href({ view: "dead" })} aria-current={current({ view: "dead" })}>
            Dead code <span className="score">{a.unreachable.length + missing}</span>
          </a>
        </li>
        <li>
          <a href={href({ view: "load" })} aria-current={current({ view: "load" })}>
            Load report <span className="score">{problems ? `${problems} issue(s)` : "ok"}</span>
          </a>
        </li>
      </ul>
      <input type="search" placeholder="Filter jobs and dataflows" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter jobs and dataflows" />
      <section className="nav-section">
        <h3>Jobs ({jobs.length})</h3>
        <ul className="nav-list">
          {jobs.filter((j) => match(j.job)).map((j) => (
            <li key={j.job}>
              <a href={href({ view: "job", name: j.job, tab: "tree" })} aria-current={isCurrentObject(j.job)} title={`Complexity ${j.score}, wave ${j.wave}`}>
                <span>{j.job}</span>
                <span className="score">{j.score}</span>
              </a>
            </li>
          ))}
        </ul>
      </section>
      <section className="nav-section">
        <h3>Dataflows ({dataflows.length})</h3>
        <ul className="nav-list">
          {dataflows.filter(match).map((d) => (
            <li key={d}>
              <a href={href({ view: "df", name: d })} aria-current={isCurrentObject(d)}>
                <span style={dead.has(d) ? { textDecoration: "line-through", color: "var(--dead)" } : undefined}>{d}</span>
              </a>
            </li>
          ))}
        </ul>
      </section>
    </nav>
  );
}
