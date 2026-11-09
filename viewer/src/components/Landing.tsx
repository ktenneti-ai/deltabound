import type { ReactNode } from "react";
import type { Boot } from "../App";
import type { LoadEntry } from "../types";
import { LoadReport } from "./LoadReport";

export function Landing(props: { boot: Boot; busy: boolean; error: string | null; report: LoadEntry[]; children: ReactNode }) {
  const { boot } = props;
  return (
    <div className="landing">
      <div className="dropzone">
        <h1>Explore a legacy ETL estate</h1>
        <p className="muted">
          Drop files or folders anywhere on this page, or choose them below. Everything is processed in your browser;
          nothing is uploaded.
        </p>
        <ul>
          <li>SAP Data Services (BODS) XML exports, one or many files</li>
          <li>
            <code>estate.json</code> from <code>deltabound export</code>
          </li>
          <li>
            <code>explanations.json</code> from <code>deltabound explain --bundle</code>
          </li>
          <li>a folder or .zip containing any of these</li>
        </ul>
        {props.children}
        <p className="muted" role="status" aria-live="polite">
          {boot.state === "booting" && (
            <>
              <span className="spinner" /> {boot.message}…
            </>
          )}
          {boot.state === "ready" && (props.busy ? "Reading files…" : `Ready: ${boot.message}`)}
        </p>
        {boot.state === "failed" && (
          <div className="notice" role="alert">
            Python could not start in this browser: {boot.message}
          </div>
        )}
        {props.error && (
          <div className="notice" role="alert">
            {props.error}
          </div>
        )}
      </div>
      {props.report.length > 0 && (
        <div style={{ width: "100%", maxWidth: 900 }}>
          <LoadReport entries={props.report} />
        </div>
      )}
    </div>
  );
}
