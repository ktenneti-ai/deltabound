// Citation ids written by deltabound/explain/context.py:
//   df:<DATAFLOW>/<TRANSFORM>[.<COLUMN>]  call:<OBJECT>#<n>  script:<OBJECT>#<n>
//   var:<OBJECT>/<NAME>  table:<QUALIFIED>  file:<NAME>
import { href, type Route } from "./route";

export type Citation =
  | { kind: "transform"; dataflow: string; transform: string; column?: string }
  | { kind: "call"; object: string; order: number }
  | { kind: "script"; object: string; index: number }
  | { kind: "var"; object: string; name: string }
  | { kind: "table" | "file"; name: string }
  | { kind: "unknown"; raw: string };

export function parseCitation(id: string): Citation {
  const colon = id.indexOf(":");
  const kind = id.slice(0, colon);
  const rest = id.slice(colon + 1);
  if (kind === "df") {
    const slash = rest.indexOf("/");
    if (slash > 0) {
      const tail = rest.slice(slash + 1);
      const dot = tail.indexOf(".");
      return dot > 0
        ? { kind: "transform", dataflow: rest.slice(0, slash), transform: tail.slice(0, dot), column: tail.slice(dot + 1) }
        : { kind: "transform", dataflow: rest.slice(0, slash), transform: tail };
    }
  }
  if (kind === "call" || kind === "script") {
    const [object, n] = rest.split("#");
    if (object && n && /^\d+$/.test(n)) {
      return kind === "call" ? { kind, object, order: Number(n) } : { kind, object, index: Number(n) };
    }
  }
  if (kind === "var") {
    const slash = rest.indexOf("/");
    if (slash > 0) return { kind, object: rest.slice(0, slash), name: rest.slice(slash + 1) };
  }
  if ((kind === "table" || kind === "file") && rest) return { kind, name: rest };
  return { kind: "unknown", raw: id };
}

/** Where a citation chip links to, if anywhere. */
export function citationRoute(c: Citation): Route | null {
  switch (c.kind) {
    case "transform":
      return { view: "df", name: c.dataflow, transform: c.transform, column: c.column ? `${c.transform}.${c.column}` : undefined };
    case "call":
    case "script":
    case "var":
      return { view: "job", name: c.object, tab: "tree" };
    default:
      return null;
  }
}

export function citationHref(id: string): string | null {
  const r = citationRoute(parseCitation(id));
  return r ? href(r) : null;
}

export function citationLabel(c: Citation): string {
  switch (c.kind) {
    case "transform":
      return c.column ? `${c.transform}.${c.column}` : c.transform;
    case "call":
      return `${c.object} step ${c.order}`;
    case "script":
      return `${c.object} script ${c.index}`;
    case "var":
      return c.name;
    case "table":
    case "file":
      return c.name;
    case "unknown":
      return c.raw;
  }
}
