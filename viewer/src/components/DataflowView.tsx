import { Background, Controls, MiniMap, ReactFlow, type Edge } from "@xyflow/react";
import { useEffect, useMemo, useState } from "react";
import { boxHeight, buildGraph, rowStates, type BoxData } from "../dfgraph";
import type { Transform } from "../ir";
import { layout } from "../layout";
import { columnOf, related } from "../lineage";
import type { PythonClient } from "../python";
import { href } from "../route";
import type { Lineage, Payload } from "../types";
import { ClaimList, Rejected } from "./Claims";
import { TransformNode, type TransformNodeType } from "./TransformNode";

const nodeTypes = { transform: TransformNode };
// SVG fill attributes cannot use CSS variables, so minimap colours come from classes (styles.css).
const minimapClass = (n: TransformNodeType) => `mm ${n.data.box.kindClass}`;
const NODE_WIDTH = 230;

export function DataflowView(props: { payload: Payload; py: PythonClient; name: string; transform?: string; column?: string }) {
  const { payload, py, name, transform, column } = props;
  const df = payload.estate!.objects[name];
  const [lineage, setLineage] = useState<{ df: string; data: Lineage } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setError(null);
    py.lineage(name)
      .then((data) => live && setLineage({ df: name, data }))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [py, name, payload]);

  const current = lineage?.df === name ? lineage.data : null;
  const graph = useMemo(() => (df && current ? buildGraph(df, current) : null), [df, current]);
  const positions = useMemo(
    () => (graph ? layout(graph.boxes.map((b) => ({ id: b.id, width: NODE_WIDTH, height: boxHeight(b) })), graph.edges.map((e) => [e.source, e.target])) : null),
    [graph],
  );

  const go = (t?: string, col?: string) => {
    location.hash = href({ view: "df", name, transform: t, column: col });
  };

  const view = useMemo(() => {
    if (!graph || !positions || !current) return null;
    const rows = rowStates(current, column);
    const hot = column ? new Set(rows.keys()) : null; // boxes on the selected column's lineage
    const nodes: TransformNodeType[] = graph.boxes.map((box) => ({
      id: box.id,
      type: "transform",
      position: positions.get(box.id)!,
      // Nodes are controlled without onNodesChange, so React Flow never writes measured sizes back;
      // the minimap skips nodes without a size. Give it the size the layout used.
      initialWidth: NODE_WIDTH,
      initialHeight: boxHeight(box),
      data: {
        box,
        rows: rows.get(box.id),
        selected: box.id === `transform:${transform}`,
        dim: !!hot && !hot.has(box.id),
        onBox: (b: BoxData) => go(b.transform?.name ?? undefined, undefined),
        onCol: (b: BoxData, node: string) => go(b.transform?.name, node),
      },
    }));
    const edges: Edge[] = graph.edges.map((e) => {
      const on = !!hot && hot.has(e.source) && hot.has(e.target);
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.label,
        className: hot ? (on ? "hot" : "dim") : undefined,
        animated: on,
        style: e.dashed ? { strokeDasharray: "5 4" } : undefined,
      };
    });
    return { nodes, edges };
  }, [graph, positions, current, column, transform, name]);

  if (!df || df.kind !== "dataflow") {
    return (
      <div className="page">
        <div className="notice" role="alert">No dataflow named {name}.</div>
      </div>
    );
  }
  const a = payload.analysis!;
  const users = a.used_by_jobs[name] ?? [];
  const selected = df.transforms.find((t) => t.name === transform);
  const notes = payload.explanations?.dataflows[name];
  const unparsed = payload.estate!.unparsed.filter((u) => u.startsWith(`${name}:`) || u.startsWith(`${name}.`));

  return (
    <div className="df-wrap">
      <div className="df-head">
        <span className="kind">dataflow</span>
        <h1 className="mono">{name}</h1>
        {a.unreachable.includes(name) && <span className="badge dead">never called</span>}
        <span className="muted">
          {df.transforms.length} transforms · run by{" "}
          {users.length
            ? users.map((j) => (
                <a key={j} className="mono" href={href({ view: "job", name: j, tab: "tree" })} style={{ marginRight: 6 }}>
                  {j}
                </a>
              ))
            : "no job"}
        </span>
      </div>
      <div className="df-layout">
        <div className="df-graph" aria-label={`Transforms of ${name}`}>
          {error && <div className="notice" role="alert" style={{ margin: 16 }}>Lineage failed: {error}</div>}
          {!view && !error && (
            <p className="muted" style={{ padding: 16 }}>
              <span className="spinner" /> Computing lineage…
            </p>
          )}
          {view && (
            <ReactFlow
              key={name}
              nodes={view.nodes}
              edges={view.edges}
              nodeTypes={nodeTypes}
              fitView
              // Keep text readable on wide dataflows: fit, but never below 65%; pan or use the minimap.
              fitViewOptions={{ padding: 0.1, minZoom: 0.65, maxZoom: 1 }}
              minZoom={0.2}
              nodesConnectable={false}
              onPaneClick={() => (transform || column) && go()}
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={20} />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable nodeClassName={minimapClass} nodeStrokeWidth={0} ariaLabel="Overview of the dataflow" />
            </ReactFlow>
          )}
        </div>
        <aside className="df-side" aria-label="Details">
          <div className="legend">
            <span><i style={{ background: "var(--k-source)" }} />source</span>
            <span><i style={{ background: "var(--k-query)" }} />query</span>
            <span><i style={{ background: "var(--k-lookup)" }} />lookup</span>
            <span><i style={{ background: "var(--k-route)" }} />case/merge</span>
            <span><i style={{ background: "var(--k-scd)" }} />SCD</span>
            <span><i style={{ background: "var(--k-target)" }} />target</span>
            <span><i style={{ background: "var(--k-opaque)" }} />not modeled</span>
          </div>
          {column && current && <ColumnPanel lineage={current} node={column} df={df.transforms} onPick={(t, c) => go(t, c)} />}
          {selected && <TransformPanel t={selected} onPickColumn={(c) => go(selected.name, `${selected.name}.${c}`)} column={column} />}
          {!column && !selected && (
            <p className="muted" style={{ margin: 0 }}>
              Click a transform for its details, or a column to trace where it comes from (blue) and where it goes (green).
            </p>
          )}
          {unparsed.length > 0 && (
            <section>
              <h3>Not fully modeled</h3>
              <ul className="lineage-list">{unparsed.map((u) => <li key={u}>{u.slice(name.length + 1).trim()}</li>)}</ul>
            </section>
          )}
          {notes && (
            <section>
              <h3 style={{ marginBottom: 6 }}>Explanation</h3>
              <ClaimList claims={[...notes.summary, ...notes.rules]} />
              <Rejected items={notes.rejected} />
            </section>
          )}
        </aside>
      </div>
    </div>
  );
}

function ColumnPanel({ lineage, node, df, onPick }: { lineage: Lineage; node: string; df: Transform[]; onPick: (t: string | undefined, col: string) => void }) {
  const { upstream, downstream } = related(lineage, node);
  const prefix = node.slice(0, node.length - columnOf(node).length - 1);
  const mapping = df.find((t) => t.name === prefix)?.columns.find((c) => c.column === columnOf(node));
  const ownerTransform = (n: string) => {
    const o = lineage.owner[n];
    return o?.startsWith("transform:") ? o.slice("transform:".length) : undefined;
  };
  const list = (title: string, items: Set<string>) => (
    <div>
      <h3>{title} ({items.size})</h3>
      {items.size ? (
        <ul className="lineage-list">
          {[...items].sort().map((n) => (
            <li key={n}>
              <a href="#" onClick={(e) => (e.preventDefault(), onPick(ownerTransform(n), n))}>{n}</a>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ margin: 0 }}>{title === "Comes from" ? "No upstream columns in this dataflow." : "Not used further in this dataflow."}</p>
      )}
    </div>
  );
  return (
    <section className="detail" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div>
        <div className="kind">column</div>
        <h2 className="mono" style={{ wordBreak: "break-all" }}>{node}</h2>
      </div>
      {mapping?.expression && <pre className="expr">{mapping.expression}</pre>}
      {list("Comes from", upstream)}
      {list("Feeds", downstream)}
    </section>
  );
}

function TransformPanel({ t, onPickColumn, column }: { t: Transform; onPickColumn: (c: string) => void; column?: string }) {
  const rows: [string, React.ReactNode][] = [];
  if (t.table) rows.push(["Table", [t.table.datastore, t.table.owner, t.table.table].filter(Boolean).join(".")]);
  if (t.inputs.length) rows.push(["Inputs", t.inputs.join(", ")]);
  if (t.where) rows.push(["Where", t.where]);
  t.joins.forEach((j, i) => rows.push([i ? "" : "Joins", `${j.left} ${j.join_type.replace("_", " ")} join ${j.right} on ${j.condition ?? "?"}`]));
  if (t.group_by.length) rows.push(["Group by", t.group_by.join(", ")]);
  if (t.order_by.length) rows.push(["Order by", t.order_by.map((s) => `${s.expression} ${s.direction}`).join(", ")]);
  t.branches.forEach((b, i) => rows.push([i ? "" : "Branches", `${b.name}: ${b.is_default ? "(default)" : b.condition}`]));
  t.lookups.forEach((lk, i) =>
    rows.push([
      i ? "" : "Lookups",
      `${lk.output_column ? `${lk.output_column} = ` : ""}${lk.function} on ${lk.table ? [lk.table.datastore, lk.table.owner, lk.table.table].filter(Boolean).join(".") : "(unresolved table)"} returning ${lk.return_columns.join(", ")}${lk.match_columns.length ? ` where ${lk.match_columns.map(([c, e]) => `${c} = ${e}`).join(" and ")}` : ""}`,
    ]),
  );
  Object.entries(t.options).forEach(([k, v], i) => rows.push([i ? "" : "Options", `${k} = ${v}`]));
  return (
    <section className="detail" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div>
        <div className="kind">{t.kind === "opaque" ? `not modeled (${t.raw_tag})` : t.kind.replace(/_/g, " ")}</div>
        <h2 className="mono">{t.name}</h2>
      </div>
      {t.kind === "opaque" && (
        <p className="notice" style={{ margin: 0 }}>
          DeltaBound does not model <code>{t.raw_tag}</code> yet, so its logic and lineage are unknown. Review it by hand.
        </p>
      )}
      {rows.length > 0 && (
        <dl>
          {rows.map(([k, v], i) => (
            <div key={i} style={{ display: "contents" }}>
              <dt>{k}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {t.columns.length > 0 && (
        <table className="col-table">
          <thead>
            <tr>
              <th>Column</th>
              <th>Mapping</th>
            </tr>
          </thead>
          <tbody>
            {t.columns.map((c) => (
              <tr key={c.column} className={column === `${t.name}.${c.column}` ? "sel" : ""} onClick={() => onPickColumn(c.column)}>
                <td>{c.column}</td>
                <td>{c.expression ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
