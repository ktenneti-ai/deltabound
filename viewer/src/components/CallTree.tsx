import { useMemo, useState } from "react";
import { CONTROL_LABEL } from "../kinds";
import { href } from "../route";
import type { Analysis, TreeNode } from "../types";

type Key = string; // path of names from the root, so a shared object can be expanded in one place only

function paths(node: TreeNode, prefix = ""): Key[] {
  const key = prefix ? `${prefix}/${node.order}:${node.name}` : node.name;
  return node.calls.length ? [key, ...node.calls.flatMap((c) => paths(c, key))] : [];
}

function count(node: TreeNode): number {
  return 1 + node.calls.reduce((n, c) => n + count(c), 0);
}

export function CallTree({ tree, analysis }: { tree: TreeNode; analysis: Analysis }) {
  const all = useMemo(() => paths(tree), [tree]);
  const [open, setOpen] = useState<Set<Key>>(() =>
    count(tree) <= 200 ? new Set(all) : new Set(all.filter((k) => k.split("/").length <= 2)),
  );
  const dead = useMemo(() => new Set(analysis.unreachable), [analysis]);
  const toggle = (k: Key) =>
    setOpen((s) => {
      const next = new Set(s);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const row = (node: TreeNode, key: Key, depth: number) => {
    const isOpen = open.has(key);
    const users = analysis.used_by_jobs[node.name]?.length ?? 0;
    const link =
      node.kind === "dataflow"
        ? href({ view: "df", name: node.name })
        : node.kind === "workflow" && depth > 0
          ? href({ view: "job", name: node.name, tab: "tree" })
          : null;
    const nameClass = `obj-name ${node.kind === "missing" ? "missing" : dead.has(node.name) ? "dead" : ""}`;
    return (
      <div className="tree-row">
        {node.calls.length ? (
          <button className="toggle" aria-expanded={isOpen} aria-label={`${isOpen ? "Collapse" : "Expand"} ${node.name}`} onClick={() => toggle(key)}>
            {isOpen ? "▾" : "▸"}
          </button>
        ) : (
          <span className="toggle-spacer" />
        )}
        <span className="kind">{node.kind}</span>
        {link ? (
          <a className={nameClass} href={link}>
            {node.name}
          </a>
        ) : (
          <span className={nameClass}>{node.name}</span>
        )}
        {node.control && (
          <span className={`badge ${node.control}`} title={node.condition ?? ""}>
            {CONTROL_LABEL[node.control] ?? node.control}
          </span>
        )}
        {node.condition && <span className="cond">{node.condition}</span>}
        {node.kind === "missing" && <span className="badge missing">not in export</span>}
        {node.cycle && <span className="badge error">call cycle</span>}
        {users > 1 && depth > 0 && (
          <span className="badge shared" title={analysis.used_by_jobs[node.name].join(", ")}>
            used by {users} jobs
          </span>
        )}
      </div>
    );
  };

  const render = (node: TreeNode, key: Key, depth: number) => (
    <li key={key} role="treeitem" aria-expanded={node.calls.length ? open.has(key) : undefined}>
      {row(node, key, depth)}
      {node.calls.length > 0 && open.has(key) && (
        <ul role="group">{node.calls.map((c) => render(c, `${key}/${c.order}:${c.name}`, depth + 1))}</ul>
      )}
    </li>
  );

  return (
    <div className="card">
      <div className="tree-tools">
        <button onClick={() => setOpen(new Set(all))}>Expand all</button>
        <button onClick={() => setOpen(new Set())}>Collapse all</button>
        <span className="legend">
          <span className="badge branch">if</span>
          <span className="badge loop">loop</span>
          <span className="badge error_handler">on error</span>
          <span className="badge shared">shared</span>
        </span>
      </div>
      <ul className="tree" role="tree" aria-label={`Call tree of ${tree.name}`} style={{ marginTop: 10 }}>
        {render(tree, tree.name, 0)}
      </ul>
    </div>
  );
}
