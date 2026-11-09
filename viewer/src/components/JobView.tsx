import { href } from "../route";
import type { Payload } from "../types";
import { CallTree } from "./CallTree";
import { ClaimList, Rejected } from "./Claims";

export function JobView({ payload, name, tab }: { payload: Payload; name: string; tab: "tree" | "explain" }) {
  const a = payload.analysis!;
  const obj = payload.estate!.objects[name];
  const tree = a.trees[name];
  if (!obj || !tree) {
    return (
      <div className="page">
        <div className="notice" role="alert">
          {a.missing[name] ? `${name} is called but is not in the export.` : `No job or workflow named ${name}.`}
        </div>
      </div>
    );
  }
  const isJob = obj.kind === "job";
  const stat = a.jobs.find((j) => j.job === name);
  const users = a.used_by_jobs[name] ?? [];
  const dead = a.unreachable.includes(name);
  const exp = payload.explanations?.jobs[name];

  return (
    <div className="page">
      <div className="header-row">
        <span className="kind">{obj.kind}</span>
        <h1 className="mono">{name}</h1>
        {dead && <span className="badge dead">never called</span>}
      </div>
      {obj.description && <p className="muted" style={{ margin: 0 }}>{obj.description}</p>}
      {stat && (
        <div className="stats">
          {(
            [
              ["Complexity", stat.score],
              ["Dataflows", stat.dataflows],
              ["Call depth", stat.depth],
              ["Wave", stat.wave],
              ["Variables", obj.variables.length],
              ["Scripts", obj.scripts.length],
            ] as const
          ).map(([label, value]) => (
            <div className="stat" key={label}>
              <div className="value">{value}</div>
              <div className="label">{label}</div>
            </div>
          ))}
        </div>
      )}
      {!isJob && users.length > 0 && (
        <p style={{ margin: 0 }}>
          Run by:{" "}
          {users.map((j) => (
            <a key={j} className="chip" href={href({ view: "job", name: j, tab: "tree" })} style={{ marginRight: 4 }}>
              {j}
            </a>
          ))}
        </p>
      )}
      {isJob && (
        <nav className="tabs" aria-label="Job views">
          <a href={href({ view: "job", name, tab: "tree" })} aria-current={tab === "tree" ? "page" : undefined}>
            Call tree
          </a>
          <a href={href({ view: "job", name, tab: "explain" })} aria-current={tab === "explain" ? "page" : undefined}>
            Explanation{exp ? "" : " (not loaded)"}
          </a>
        </nav>
      )}
      {(tab === "tree" || !isJob) && (
        <>
          <CallTree key={name} tree={tree} analysis={a} />
          {(obj.variables.length > 0 || obj.scripts.length > 0) && (
            <section className="card detail">
              <h2>Variables and scripts</h2>
              {obj.variables.length > 0 && (
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr>
                      <th>Variable</th>
                      <th>Scope</th>
                      <th>Type</th>
                      <th>Default</th>
                    </tr>
                  </thead>
                  <tbody>
                    {obj.variables.map((v) => (
                      <tr key={v.name}>
                        <td className="mono">{v.name}</td>
                        <td>{v.scope}</td>
                        <td className="mono">{v.data_type ?? ""}</td>
                        <td className="mono">{v.default ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {obj.scripts.map((s, i) => (
                <div key={i} style={{ marginTop: 8 }}>
                  <div className="muted">Script {i + 1}</div>
                  <pre className="expr">{s}</pre>
                </div>
              ))}
            </section>
          )}
        </>
      )}
      {isJob && tab === "explain" && (
        exp ? (
          <>
            {(
              [
                ["Purpose", exp.purpose],
                ["Inputs", exp.inputs],
                ["Outputs", exp.outputs],
                ["Business rules", exp.business_rules],
              ] as const
            ).map(([title, claims]) => (
              <section className="card" key={title}>
                <h2 style={{ marginBottom: 8 }}>{title}</h2>
                <ClaimList claims={claims} />
              </section>
            ))}
            <Rejected items={exp.rejected} />
            <p className="muted" style={{ margin: 0 }}>
              Written by {exp.run.model} ({exp.run.provider}) on {exp.run.created_at}. Each claim links to the IR element it
              came from.
            </p>
          </>
        ) : (
          <div className="card">
            <p style={{ marginTop: 0 }}>No explanation is loaded for this job.</p>
            <p className="muted" style={{ marginBottom: 0 }}>
              Run <code>deltabound explain &lt;export&gt; --all --bundle explanations.json</code> and drop
              <code> explanations.json</code> on this page.
            </p>
          </div>
        )
      )}
    </div>
  );
}
