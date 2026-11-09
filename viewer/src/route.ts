// Hash routes, so every view can be bookmarked and shared:
//   #/                      overview
//   #/job/NAME[?tab=explain] job or workflow call tree (explanation tab for jobs)
//   #/df/NAME[?t=T&col=C]   dataflow graph, selected transform and column node
//   #/dead                  dead code and missing references
//   #/load                  load report
export type Route =
  | { view: "overview" }
  | { view: "job"; name: string; tab: "tree" | "explain" }
  | { view: "df"; name: string; transform?: string; column?: string }
  | { view: "dead" }
  | { view: "load" };

export function parseRoute(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?", 2);
  const params = new URLSearchParams(query);
  const [head, ...rest] = path.split("/");
  const name = decodeURIComponent(rest.join("/"));
  switch (head) {
    case "job":
      return name ? { view: "job", name, tab: params.get("tab") === "explain" ? "explain" : "tree" } : { view: "overview" };
    case "df":
      return name
        ? { view: "df", name, transform: params.get("t") ?? undefined, column: params.get("col") ?? undefined }
        : { view: "overview" };
    case "dead":
      return { view: "dead" };
    case "load":
      return { view: "load" };
    default:
      return { view: "overview" };
  }
}

export function href(route: Route): string {
  switch (route.view) {
    case "overview":
      return "#/";
    case "job":
      return `#/job/${encodeURIComponent(route.name)}${route.tab === "explain" ? "?tab=explain" : ""}`;
    case "df": {
      const params = new URLSearchParams();
      if (route.transform) params.set("t", route.transform);
      if (route.column) params.set("col", route.column);
      const q = params.toString();
      return `#/df/${encodeURIComponent(route.name)}${q ? `?${q}` : ""}`;
    }
    case "dead":
      return "#/dead";
    case "load":
      return "#/load";
  }
}
