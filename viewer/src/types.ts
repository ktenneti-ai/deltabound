// Shapes returned by deltabound/bridge.py. The IR itself (Estate, Transform, ...) is
// generated from the Python models into ir.ts; keep these in sync with bridge.py.
import type { Estate } from "./ir";

export type LoadStatus = "ok" | "error" | "skipped" | "unsupported";

export interface LoadEntry {
  file: string;
  format: string;
  status: LoadStatus;
  message: string;
}

export interface TreeNode {
  name: string;
  kind: "job" | "workflow" | "dataflow" | "task" | "missing";
  condition: string | null;
  control: "branch" | "loop" | "error_handler" | null;
  order: number;
  calls: TreeNode[];
  cycle?: boolean;
}

export interface JobStat {
  job: string;
  score: number;
  dataflows: number;
  depth: number;
  wave: number;
}

export interface Analysis {
  counts: { job: number; workflow: number; dataflow: number; task: number; transforms: number; opaque: number };
  coverage: number;
  jobs: JobStat[]; // hardest first
  trees: Record<string, TreeNode>; // every job and workflow
  unreachable: string[];
  missing: Record<string, string[]>; // missing object -> callers
  used_by_jobs: Record<string, string[]>;
}

export interface Claim {
  text: string;
  citations: string[];
  origin: "llm" | "ir";
}

export interface RejectedClaim {
  section: string;
  text: string;
  citations: string[];
  reason: string;
}

export interface RunInfo {
  provider: string;
  model: string;
  prompt_version: string;
  context_hash: string;
  created_at: string;
}

export interface JobExplanation {
  job: string;
  purpose: Claim[];
  inputs: Claim[];
  outputs: Claim[];
  business_rules: Claim[];
  rejected: RejectedClaim[];
  dataflows: string[];
  run: RunInfo;
}

export interface DataflowNotes {
  dataflow: string;
  summary: Claim[];
  rules: Claim[];
  rejected: RejectedClaim[];
  run: RunInfo;
}

export interface ExplanationBundle {
  format: "deltabound-explanations";
  version: number;
  ir_version: string | null;
  jobs: Record<string, JobExplanation>;
  dataflows: Record<string, DataflowNotes>;
}

export interface Payload {
  ok: boolean;
  load_report: LoadEntry[];
  estate: Estate | null;
  analysis: Analysis | null;
  explanations: ExplanationBundle | null;
}

export interface Lineage {
  edges: [string, string][];
  owner: Record<string, string>; // column node -> "transform:<name>" | "lookup:<table>"
}

export interface InputFile {
  name: string;
  data: ArrayBuffer;
}
