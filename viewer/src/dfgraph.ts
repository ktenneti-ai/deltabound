// Turn one dataflow (IR) plus its lineage (from Python) into boxes and edges to draw.
import type { EtlObject, Transform } from "./ir";
import { kindClass, kindLabel } from "./kinds";
import { columnOf, related } from "./lineage";
import type { Lineage } from "./types";

export interface Col {
  label: string;
  node: string; // lineage node selected when this row is clicked
  type?: string | null;
}

export interface BoxData {
  id: string; // "transform:<name>" or "lookup:<table>"
  name: string;
  kind: string;
  kindClass: string;
  kindLabel: string;
  table?: string;
  cols: Col[];
  transform?: Transform;
}

export interface EdgeData {
  id: string;
  source: string;
  target: string;
  label?: string;
  dashed?: boolean;
}

export type RowState = "sel" | "up" | "down";

const tid = (name: string) => `transform:${name}`;

export function buildGraph(df: EtlObject, lineage: Lineage): { boxes: BoxData[]; edges: EdgeData[] } {
  const branchOwner = new Map<string, string>();
  for (const t of df.transforms) for (const b of t.branches) branchOwner.set(b.name, t.name);

  // Columns per box from lineage, for boxes without declared columns (targets, pass-through, sources, lookups).
  const lineageCols = new Map<string, Map<string, string>>();
  for (const [node, owner] of Object.entries(lineage.owner)) {
    const col = columnOf(node);
    const cols = lineageCols.get(owner) ?? lineageCols.set(owner, new Map()).get(owner)!;
    // Prefer the box's own node over a Case branch node for the same column.
    const prefix = node.slice(0, node.length - col.length - 1);
    if (!cols.has(col) || !branchOwner.has(prefix)) cols.set(col, node);
  }
  const fromLineage = (owner: string): Col[] =>
    [...(lineageCols.get(owner) ?? new Map<string, string>())].sort(([a], [b]) => a.localeCompare(b)).map(([label, node]) => ({ label, node }));

  const boxes: BoxData[] = df.transforms.map((t) => ({
    id: tid(t.name),
    name: t.name,
    kind: t.kind,
    kindClass: kindClass(t.kind),
    kindLabel: kindLabel(t.kind, t.raw_tag),
    table: t.table ? [t.table.datastore, t.table.owner, t.table.table].filter(Boolean).join(".") : (t.options["fileName"] ?? undefined),
    cols: t.columns.length
      ? t.columns.map((c) => ({ label: c.column, node: `${t.name}.${c.column}`, type: c.data_type }))
      : fromLineage(tid(t.name)),
    transform: t,
  }));
  const ids = new Set(boxes.map((b) => b.id));

  const edges = new Map<string, EdgeData>();
  for (const t of df.transforms) {
    for (const input of t.inputs) {
      const src = branchOwner.get(input) ?? input;
      if (!ids.has(tid(src))) continue;
      const e: EdgeData = { id: `${src}->${t.name}:${input}`, source: tid(src), target: tid(t.name) };
      if (branchOwner.has(input)) e.label = input;
      edges.set(e.id, e);
    }
    for (const lk of t.lookups) {
      if (!lk.table) continue;
      const table = [lk.table.datastore, lk.table.owner, lk.table.table].filter(Boolean).join(".");
      const id = `lookup:${table}`;
      if (!ids.has(id)) {
        ids.add(id);
        boxes.push({ id, name: table, kind: "lookup table", kindClass: "k-external", kindLabel: "lookup table", cols: fromLineage(id) });
      }
      edges.set(`${id}->${t.name}`, { id: `${id}->${t.name}`, source: id, target: tid(t.name), label: "lookup", dashed: true });
    }
  }
  return { boxes, edges: [...edges.values()] };
}

/** Row highlight per box and column for a selected lineage node. */
export function rowStates(lineage: Lineage, selected: string | undefined): Map<string, Map<string, RowState>> {
  const out = new Map<string, Map<string, RowState>>();
  if (!selected) return out;
  const set = (node: string, state: RowState) => {
    const owner = lineage.owner[node];
    if (!owner) return;
    const m = out.get(owner) ?? out.set(owner, new Map()).get(owner)!;
    if (m.get(columnOf(node)) !== "sel") m.set(columnOf(node), state);
  };
  const { upstream, downstream } = related(lineage, selected);
  upstream.forEach((n) => set(n, "up"));
  downstream.forEach((n) => set(n, "down"));
  set(selected, "sel");
  return out;
}

export function boxHeight(box: BoxData): number {
  const head = box.table ? 62 : 46;
  return head + (box.cols.length ? Math.min(box.cols.length, 11) * 19 + 2 : 0);
}
