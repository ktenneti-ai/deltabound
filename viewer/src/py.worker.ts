/// <reference lib="webworker" />
// Runs Python (Pyodide) off the main thread so the page stays responsive.
// Everything is served by this app: the Pyodide runtime under pyodide/, wheels under py/.
import type { PyodideAPI } from "pyodide";
import type { InputFile } from "./types";

export type Request =
  | { id: number; op: "init"; base: string }
  | { id: number; op: "load"; files: InputFile[] }
  | { id: number; op: "lineage"; dataflow: string };

export type Response =
  | { id: number; ok: true; result: unknown }
  | { id: number; ok: false; error: string }
  | { id: -1; progress: string };

declare const self: DedicatedWorkerGlobalScope;
let py: PyodideAPI | null = null;

const progress = (message: string) => self.postMessage({ id: -1, progress: message } satisfies Response);

async function init(base: string): Promise<string> {
  progress("Starting Python (first visit downloads about 20 MB)");
  const runtime = `${base}pyodide/`;
  const mod = (await import(/* @vite-ignore */ `${runtime}pyodide.mjs`)) as typeof import("pyodide");
  py = await mod.loadPyodide({ indexURL: runtime });
  const manifest = (await (await fetch(`${base}py/manifest.json`)).json()) as { wheels: string[]; packages: string[] };
  progress("Loading Python packages");
  await py.loadPackage(manifest.packages, { messageCallback: () => {} });
  for (const wheel of manifest.wheels) {
    const buf = await (await fetch(`${base}py/${wheel}`)).arrayBuffer();
    py.unpackArchive(buf, "wheel");
  }
  py.runPython("import deltabound.bridge as bridge");
  return py.runPython("import sys, deltabound; f'deltabound {deltabound.__version__} on Python {sys.version.split()[0]}'") as string;
}

function load(files: InputFile[]): unknown {
  if (!py) throw new Error("Python is not ready");
  py.FS.mkdirTree("/tmp/in");
  for (const f of py.FS.readdir("/tmp/in")) if (f !== "." && f !== "..") py.FS.unlink(`/tmp/in/${f}`);
  files.forEach((f, i) => py!.FS.writeFile(`/tmp/in/${i}`, new Uint8Array(f.data)));
  const names = py.toPy(files.map((f) => f.name));
  try {
    py.globals.set("_names", names);
    const json = py.runPython(
      "bridge.load_json([(n, open(f'/tmp/in/{i}', 'rb').read()) for i, n in enumerate(_names)])",
    ) as string;
    return JSON.parse(json);
  } finally {
    names.destroy();
    py.globals.delete("_names");
  }
}

function lineage(dataflow: string): unknown {
  if (!py) throw new Error("Python is not ready");
  py.globals.set("_df", dataflow);
  return JSON.parse(py.runPython("bridge.lineage_json(_df)") as string);
}

self.onmessage = async (event: MessageEvent<Request>) => {
  const req = event.data;
  try {
    const result = req.op === "init" ? await init(req.base) : req.op === "load" ? load(req.files) : lineage(req.dataflow);
    self.postMessage({ id: req.id, ok: true, result } satisfies Response);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // Python tracebacks are long; the last lines carry the actual error.
    self.postMessage({ id: req.id, ok: false, error: msg.split("\n").slice(-6).join("\n") } satisfies Response);
  }
};
