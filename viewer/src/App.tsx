import { useCallback, useEffect, useRef, useState } from "react";
import { DataflowView } from "./components/DataflowView";
import { DeadCode } from "./components/DeadCode";
import { JobView } from "./components/JobView";
import { Landing } from "./components/Landing";
import { LoadReport } from "./components/LoadReport";
import { Overview } from "./components/Overview";
import { Sidebar } from "./components/Sidebar";
import { demoFiles, fromDrop, fromFileList, fromUrls } from "./files";
import { PythonClient } from "./python";
import { parseRoute, type Route } from "./route";
import type { InputFile, Payload } from "./types";

function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    const on = () => setRoute(parseRoute(location.hash));
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return route;
}

export type Boot = { state: "booting" | "ready" | "failed"; message: string };

export function App() {
  const py = useRef<PythonClient | null>(null);
  const [boot, setBoot] = useState<Boot>({ state: "booting", message: "Starting" });
  const [payload, setPayload] = useState<Payload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const route = useRoute();
  const estateLoaded = useRef(false);
  estateLoaded.current = !!payload?.ok;

  useEffect(() => setError(null), [route]); // a message belongs to the page it appeared on

  const load = useCallback(async (files: InputFile[] | Promise<InputFile[]>) => {
    if (!py.current) return;
    setBusy(true);
    setError(null);
    try {
      const list = await files;
      if (!list.length) throw new Error("No files selected.");
      const next = await py.current.load(list);
      setPayload((prev) => {
        if (next.ok) return next;
        // Only an explanations bundle (or nothing usable): keep the loaded estate.
        if (prev?.ok) {
          return { ...prev, explanations: next.explanations ?? prev.explanations, load_report: [...prev.load_report, ...next.load_report] };
        }
        return next;
      });
      if (!next.ok && !next.explanations) {
        setError(
          estateLoaded.current
            ? "None of these files could be loaded; the current estate is unchanged. Open the load report for details."
            : "No estate could be loaded from these files. See the load report below.",
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    const client = new PythonClient();
    py.current = client;
    client.onProgress = (message) => setBoot({ state: "booting", message });
    client
      .init()
      .then((version) => {
        if (py.current !== client) return;
        setBoot({ state: "ready", message: version });
        const src = new URLSearchParams(location.search).getAll("src");
        const urls = src.flatMap((s) => s.split(",")).map((s) => s.trim()).filter(Boolean);
        if (urls.length) void load(fromUrls(urls));
      })
      .catch((e: Error) => {
        if (py.current === client) setBoot({ state: "failed", message: e.message });
      });
    return () => {
      // React StrictMode mounts twice in development: stop the first worker.
      if (py.current === client) py.current = null;
      client.terminate();
    };
  }, [load]);

  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => !!e.dataTransfer?.types.includes("Files");
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth++;
      setDragging(true);
    };
    const leave = () => {
      depth = Math.max(0, depth - 1);
      if (!depth) setDragging(false);
    };
    const over = (e: DragEvent) => hasFiles(e) && e.preventDefault();
    const drop = (e: DragEvent) => {
      if (!e.dataTransfer || !hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      void load(fromDrop(e.dataTransfer));
    };
    addEventListener("dragenter", enter);
    addEventListener("dragleave", leave);
    addEventListener("dragover", over);
    addEventListener("drop", drop);
    return () => {
      removeEventListener("dragenter", enter);
      removeEventListener("dragleave", leave);
      removeEventListener("dragover", over);
      removeEventListener("drop", drop);
    };
  }, [load]);

  const ready = boot.state === "ready";
  const actions = {
    onFiles: (files: FileList) => void load(fromFileList(files)),
    onDemo: () => void load(demoFiles()),
  };
  const estate = payload?.ok ? payload : null;

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          Delta<span>Bound</span> viewer
        </a>
        {estate?.estate && (
          <span className="meta">
            {estate.estate.source_tool} {estate.estate.source_version ?? ""} · IR {estate.estate.ir_version}
            {estate.explanations ? ` · ${Object.keys(estate.explanations.jobs).length} explained jobs` : ""}
          </span>
        )}
        <span className="spacer" />
        {busy && <span className="spinner" role="status" aria-label="Loading" />}
        {estate && <LoadButtons ready={ready && !busy} {...actions} compact />}
      </header>
      {estate?.analysis && estate.estate ? (
        <div className="body">
          <Sidebar payload={estate} route={route} />
          <main className="main">
            {error && (
              <div className="page" style={{ paddingBottom: 0 }}>
                <div className="notice" role="alert" style={{ display: "flex", gap: 12, alignItems: "center" }}>
                  <span style={{ flex: 1 }}>
                    {error} <a href="#/load">Load report</a>
                  </span>
                  <button onClick={() => setError(null)} aria-label="Dismiss message">
                    Dismiss
                  </button>
                </div>
              </div>
            )}
            {route.view === "overview" && <Overview payload={estate} />}
            {route.view === "job" && <JobView payload={estate} name={route.name} tab={route.tab} />}
            {route.view === "df" && py.current && (
              <DataflowView payload={estate} py={py.current} name={route.name} transform={route.transform} column={route.column} />
            )}
            {route.view === "dead" && <DeadCode payload={estate} />}
            {route.view === "load" && <LoadReport entries={estate.load_report} />}
          </main>
        </div>
      ) : (
        <Landing boot={boot} busy={busy} error={error} report={payload?.load_report ?? []}>
          <LoadButtons ready={ready && !busy} {...actions} />
        </Landing>
      )}
      {dragging && <div className="drag-overlay">Drop export files, folders, zips, estate.json or explanations.json</div>}
    </div>
  );
}

function LoadButtons(props: { ready: boolean; onFiles: (f: FileList) => void; onDemo: () => void; compact?: boolean }) {
  const files = useRef<HTMLInputElement>(null);
  const folder = useRef<HTMLInputElement>(null);
  const pick = (ref: React.RefObject<HTMLInputElement | null>) => ref.current?.click();
  const change = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) props.onFiles(e.target.files);
    e.target.value = "";
  };
  return (
    <div className="actions" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <button
        className="primary"
        disabled={!props.ready}
        onClick={() => pick(files)}
        title="Import export files (.xml, .dtsx), estate.json, explanations.json or a .zip; runs the analyzer in your browser"
      >
        Import files
      </button>
      <button disabled={!props.ready} onClick={() => pick(folder)} title="Import a whole folder, e.g. a demo source folder">
        Import folder
      </button>
      <button disabled={!props.ready} onClick={props.onDemo}>
        {props.compact ? "Demo" : "Load demo estate"}
      </button>
      <input
        ref={files}
        type="file"
        multiple
        hidden
        accept=".xml,.json,.zip,.dtsx,.atl,.toml"
        aria-label="Import files"
        onChange={change}
      />
      <input ref={folder} type="file" hidden onChange={change} {...{ webkitdirectory: "" }} />
    </div>
  );
}
