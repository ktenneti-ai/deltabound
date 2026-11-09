import { describe, expect, it } from "vitest";
import { buildGraph, rowStates } from "../src/dfgraph";
import type { EtlObject } from "../src/ir";
import { layout } from "../src/layout";
import { related } from "../src/lineage";
import type { Lineage } from "../src/types";
import pos from "./fixtures/DF_FACT_POS_SALES.json";
import loyalty from "./fixtures/DF_FACT_LOYALTY_POINTS.json";

const POS = { df: pos.dataflow as unknown as EtlObject, lineage: pos.lineage as unknown as Lineage };
const LOY = { df: loyalty.dataflow as unknown as EtlObject, lineage: loyalty.lineage as unknown as Lineage };

describe("lineage traversal", () => {
  it("finds every upstream column of a target column, across Case branches and Merge", () => {
    const { upstream, downstream } = related(POS.lineage, "DS_DWH.MART.FACT_POS_SALES.AMOUNT");
    for (const n of ["MRG_CHANNELS.AMOUNT", "Q_KIOSK.AMOUNT", "CH_KIOSK.AMOUNT", "CASE_CHANNEL.AMOUNT", "Q_POS_IN.AMOUNT", "STG_POS_TXN.AMOUNT"]) {
      expect(upstream.has(n)).toBe(true);
    }
    expect(downstream.size).toBe(0);
  });

  it("finds downstream columns and ignores cycles", () => {
    const { downstream } = related(POS.lineage, "STG_POS_TXN.AMOUNT");
    expect(downstream.has("DS_DWH.MART.FACT_POS_SALES.AMOUNT")).toBe(true);
    const cyc: Lineage = { edges: [["a", "b"], ["b", "a"]], owner: {} };
    expect([...related(cyc, "a").upstream]).toEqual(["b"]);
  });
});

describe("dataflow graph", () => {
  const g = buildGraph(POS.df, POS.lineage);
  const box = (id: string) => g.boxes.find((b) => b.id === id)!;

  it("has one box per transform plus external lookup tables", () => {
    expect(g.boxes.filter((b) => b.transform)).toHaveLength(POS.df.transforms.length);
    expect(box("lookup:DS_DWH.MART.DIM_STORE").kindClass).toBe("k-external");
  });

  it("connects Case branch outputs to their consumers, labelled with the branch", () => {
    const e = g.edges.find((x) => x.target === "transform:Q_KIOSK")!;
    expect(e.source).toBe("transform:CASE_CHANNEL");
    expect(e.label).toBe("CH_KIOSK");
    expect(g.edges.filter((x) => x.target === "transform:MRG_CHANNELS").map((x) => x.source).sort()).toEqual([
      "transform:Q_KIOSK", "transform:Q_OTHER", "transform:Q_STORE",
    ]);
  });

  it("draws lookups as dashed edges from the lookup table", () => {
    const e = g.edges.filter((x) => x.source === "lookup:DS_DWH.MART.DIM_STORE").map((x) => x.target).sort();
    expect(e).toEqual(["transform:Q_KIOSK", "transform:Q_STORE"]);
    expect(g.edges.find((x) => x.source.startsWith("lookup:"))!.dashed).toBe(true);
  });

  it("gives column-less boxes (targets, Case, Merge) their columns from lineage", () => {
    expect(box("transform:FACT_POS_SALES").cols.map((c) => c.label)).toContain("AMOUNT");
    const merge = box("transform:MRG_CHANNELS").cols;
    expect(merge.map((c) => c.label)).toContain("STORE_KEY");
    // The Case box uses its own node, not a branch node, for each column.
    expect(box("transform:CASE_CHANNEL").cols.find((c) => c.label === "AMOUNT")!.node).toBe("CASE_CHANNEL.AMOUNT");
  });

  it("highlights the selected column, its upstream and downstream rows", () => {
    const rows = rowStates(POS.lineage, "Q_KIOSK.AMOUNT");
    expect(rows.get("transform:Q_KIOSK")!.get("AMOUNT")).toBe("sel");
    expect(rows.get("transform:CASE_CHANNEL")!.get("AMOUNT")).toBe("up");
    expect(rows.get("transform:FACT_POS_SALES")!.get("AMOUNT")).toBe("down");
    expect(rows.has("transform:Q_STORE")).toBe(false);
    expect(rowStates(POS.lineage, undefined).size).toBe(0);
  });

  it("handles the Lookup transform: return column traced to the reference table", () => {
    const lg = buildGraph(LOY.df, LOY.lineage);
    expect(lg.boxes.some((b) => b.id === "lookup:DS_DWH.MART.DIM_LOYALTY_MEMBER")).toBe(true);
    const rows = rowStates(LOY.lineage, "DS_DWH.MART.FACT_LOYALTY_POINTS.MEMBER_KEY");
    expect(rows.get("lookup:DS_DWH.MART.DIM_LOYALTY_MEMBER")!.get("MEMBER_KEY")).toBe("up");
  });

  it("lays out left to right without overlaps", () => {
    const pos = layout(g.boxes.map((b) => ({ id: b.id, width: 230, height: 100 })), g.edges.map((e) => [e.source, e.target]));
    expect(pos.get("transform:STG_POS_TXN")!.x).toBeLessThan(pos.get("transform:FACT_POS_SALES")!.x);
    const all = [...pos.values()];
    for (let i = 0; i < all.length; i++)
      for (let j = i + 1; j < all.length; j++) {
        const overlap = Math.abs(all[i].x - all[j].x) < 230 && Math.abs(all[i].y - all[j].y) < 100;
        expect(overlap).toBe(false);
      }
  });
});
