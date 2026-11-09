// Graph traversal over lineage edges computed by Python (bridge.lineage). The lineage
// rules themselves live only in deltabound/graph.py; this file only walks the edges.
import type { Lineage } from "./types";

export interface Related {
  upstream: Set<string>;
  downstream: Set<string>;
}

function walk(start: string, next: Map<string, string[]>): Set<string> {
  const seen = new Set<string>();
  const stack = [...(next.get(start) ?? [])];
  while (stack.length) {
    const n = stack.pop()!;
    if (seen.has(n)) continue;
    seen.add(n);
    stack.push(...(next.get(n) ?? []));
  }
  seen.delete(start);
  return seen;
}

export function related(lineage: Lineage, node: string): Related {
  const parents = new Map<string, string[]>();
  const children = new Map<string, string[]>();
  for (const [a, b] of lineage.edges) {
    (parents.get(b) ?? parents.set(b, []).get(b)!).push(a);
    (children.get(a) ?? children.set(a, []).get(a)!).push(b);
  }
  return { upstream: walk(node, parents), downstream: walk(node, children) };
}

/** Graph box ids ("transform:X" / "lookup:T") that own any of the given column nodes. */
export function owners(lineage: Lineage, nodes: Iterable<string>): Set<string> {
  const out = new Set<string>();
  for (const n of nodes) {
    const o = lineage.owner[n];
    if (o) out.add(o);
  }
  return out;
}

/** Column name part of a node id: "Q_JOIN.ORDER_ID" -> "ORDER_ID", "DS.STG.T.COL" -> "COL". */
export function columnOf(node: string): string {
  return node.slice(node.lastIndexOf(".") + 1);
}

/** Lineage node id for a column shown inside a transform box. */
export function columnNode(transformName: string, column: string, targetTable?: string): string {
  return `${targetTable ?? transformName}.${column}`;
}
