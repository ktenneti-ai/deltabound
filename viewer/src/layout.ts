// Left-to-right graph layout with dagre. React Flow positions are top-left corners.
import dagre from "@dagrejs/dagre";

export interface Box {
  id: string;
  width: number;
  height: number;
}

export function layout(boxes: Box[], edges: [string, string][]): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 70, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const b of boxes) g.setNode(b.id, { width: b.width, height: b.height });
  for (const [a, b] of edges) if (g.hasNode(a) && g.hasNode(b)) g.setEdge(a, b);
  dagre.layout(g);
  const out = new Map<string, { x: number; y: number }>();
  for (const b of boxes) {
    const n = g.node(b.id);
    out.set(b.id, { x: n.x - b.width / 2, y: n.y - b.height / 2 });
  }
  return out;
}
