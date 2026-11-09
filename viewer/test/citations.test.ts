import { describe, expect, it } from "vitest";
import { citationHref, citationLabel, parseCitation } from "../src/citations";

describe("citations", () => {
  it("parses every id kind written by explain/context.py", () => {
    expect(parseCitation("df:DF_FACT_SALES/Q_JOIN")).toEqual({ kind: "transform", dataflow: "DF_FACT_SALES", transform: "Q_JOIN" });
    expect(parseCitation("df:DF_FACT_SALES/Q_JOIN.PRODUCT_KEY")).toEqual({
      kind: "transform", dataflow: "DF_FACT_SALES", transform: "Q_JOIN", column: "PRODUCT_KEY",
    });
    expect(parseCitation("call:WF_STAGE_SALES#3")).toEqual({ kind: "call", object: "WF_STAGE_SALES", order: 3 });
    expect(parseCitation("script:WF_POS_INTRADAY#2")).toEqual({ kind: "script", object: "WF_POS_INTRADAY", index: 2 });
    expect(parseCitation("var:JOB_SALES_DAILY/$G_FULL_LOAD")).toEqual({ kind: "var", object: "JOB_SALES_DAILY", name: "$G_FULL_LOAD" });
    expect(parseCitation("table:DS_ERP.SAPSR3.VBAK")).toEqual({ kind: "table", name: "DS_ERP.SAPSR3.VBAK" });
    expect(parseCitation("file:pos_txn_[$L_STORE_IDX].csv")).toEqual({ kind: "file", name: "pos_txn_[$L_STORE_IDX].csv" });
  });

  it("keeps malformed ids instead of guessing", () => {
    for (const bad of ["df:NOSLASH", "call:X", "call:X#y", "weird", "table:"]) {
      expect(parseCitation(bad)).toEqual({ kind: "unknown", raw: bad });
    }
  });

  it("links transforms and columns to the dataflow view", () => {
    expect(citationHref("df:DF_FACT_SALES/Q_JOIN.PRODUCT_KEY")).toBe("#/df/DF_FACT_SALES?t=Q_JOIN&col=Q_JOIN.PRODUCT_KEY");
    expect(citationHref("call:WF_STAGE_SALES#1")).toBe("#/job/WF_STAGE_SALES");
    expect(citationHref("table:DS_ERP.SAPSR3.VBAK")).toBeNull();
    expect(citationLabel(parseCitation("call:WF_STAGE_SALES#1"))).toBe("WF_STAGE_SALES step 1");
  });
});
