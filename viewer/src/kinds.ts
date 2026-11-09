// Display groups for transform kinds (colours in styles.css).
export function kindClass(kind: string): string {
  if (kind === "table_source" || kind === "file_source") return "k-source";
  if (kind === "table_target" || kind === "file_target") return "k-target";
  if (["query", "derived_column", "data_conversion", "aggregate", "sort", "join"].includes(kind)) return "k-query";
  if (kind === "multicast" || kind === "row_count") return "k-route";
  if (kind === "scd") return "k-scd";
  if (kind === "lookup") return "k-lookup";
  if (kind === "case" || kind === "merge" || kind === "validation" || kind === "map_operation") return "k-route";
  if (kind === "table_comparison" || kind === "history_preserving" || kind === "key_generation") return "k-scd";
  if (kind === "opaque") return "k-opaque";
  return "k-other";
}

export function kindLabel(kind: string, rawTag?: string | null): string {
  return kind === "opaque" ? `not modeled: ${rawTag ?? "unknown"}` : kind.replace(/_/g, " ");
}

export const CONTROL_LABEL: Record<string, string> = {
  branch: "if",
  loop: "loop",
  error_handler: "on error",
};
