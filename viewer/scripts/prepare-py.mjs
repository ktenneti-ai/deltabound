// Prepare everything the browser needs to run deltabound under Pyodide, all served
// from the app itself (no CDN at run time, works offline):
//   public/pyodide/  Pyodide runtime (from node_modules) + pydantic packages (downloaded
//                    once from the Pyodide CDN, checked against pyodide-lock.json sha256)
//   public/py/       deltabound wheel (built from ..) + networkx wheel, and manifest.json
//   public/demo/     the synthetic sample estate (samples/bods/*.xml) and manifest.json
// PYTHON env var: interpreter used to build wheels (default ../.venv/bin/python).
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const viewer = resolve(here, "..");
const repo = resolve(viewer, "..");
const pub = join(viewer, "public");
const python = process.env.PYTHON ?? join(repo, ".venv", "bin", "python");

const pyodidePkg = join(viewer, "node_modules", "pyodide");
const { version } = JSON.parse(readFileSync(join(pyodidePkg, "package.json"), "utf8"));
const CDN = `https://cdn.jsdelivr.net/pyodide/v${version}/full/`;
const PACKAGES = ["pydantic"]; // networkx comes as a wheel: Pyodide's build depends on numpy + matplotlib
const NETWORKX = "networkx==3.6.1";

function step(msg) {
  console.log(`[prepare-py] ${msg}`);
}

// 1. Pyodide runtime
const runtimeDir = join(pub, "pyodide");
mkdirSync(runtimeDir, { recursive: true });
for (const f of ["pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json", "pyodide.mjs"]) {
  copyFileSync(join(pyodidePkg, f), join(runtimeDir, f));
}
step(`Pyodide ${version} runtime copied`);

// 2. pydantic and its dependencies, from the lockfile
const lock = JSON.parse(readFileSync(join(pyodidePkg, "pyodide-lock.json"), "utf8"));
const byName = new Map(Object.values(lock.packages).map((p) => [p.name.toLowerCase(), p]));
const wanted = new Set();
const visit = (name) => {
  const p = byName.get(name.toLowerCase());
  if (!p) throw new Error(`package ${name} not in pyodide-lock.json`);
  if (wanted.has(p.name)) return;
  wanted.add(p.name);
  p.depends.forEach(visit);
};
PACKAGES.forEach(visit);
for (const name of wanted) {
  const p = byName.get(name.toLowerCase());
  const dest = join(runtimeDir, p.file_name);
  const sha = (buf) => createHash("sha256").update(buf).digest("hex");
  if (existsSync(dest) && sha(readFileSync(dest)) === p.sha256) continue;
  const res = await fetch(CDN + p.file_name);
  if (!res.ok) throw new Error(`download ${p.file_name}: HTTP ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  if (sha(buf) !== p.sha256) throw new Error(`sha256 mismatch for ${p.file_name}`);
  writeFileSync(dest, buf);
  step(`downloaded ${p.file_name}`);
}

// 3. Python wheels
const wheelDir = join(pub, "py");
rmSync(wheelDir, { recursive: true, force: true });
mkdirSync(wheelDir, { recursive: true });
const pip = (...args) => execFileSync(python, ["-m", "pip", ...args, "--quiet", "--disable-pip-version-check"], { stdio: "inherit" });
pip("wheel", repo, "--no-deps", "-w", wheelDir);
pip("download", NETWORKX, "--no-deps", "--only-binary=:all:", "--python-version", "3.14", "-d", wheelDir);
const wheels = readdirSync(wheelDir).filter((f) => f.endsWith(".whl")).sort();
writeFileSync(join(wheelDir, "manifest.json"), JSON.stringify({ wheels, packages: PACKAGES }, null, 2));
step(`wheels: ${wheels.join(", ")}`);

// 4. Demo estate
const demoDir = join(pub, "demo");
rmSync(demoDir, { recursive: true, force: true });
mkdirSync(demoDir, { recursive: true });
const samples = join(repo, "samples", "bods");
const demo = readdirSync(samples).filter((f) => f.endsWith(".xml")).sort();
demo.forEach((f) => copyFileSync(join(samples, f), join(demoDir, f)));
writeFileSync(join(demoDir, "manifest.json"), JSON.stringify({ files: demo }, null, 2));
step(`demo: ${demo.length} files`);
