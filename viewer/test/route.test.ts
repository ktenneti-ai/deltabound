import { describe, expect, it } from "vitest";
import { href, parseRoute, type Route } from "../src/route";

describe("routes", () => {
  const cases: [string, Route][] = [
    ["#/", { view: "overview" }],
    ["", { view: "overview" }],
    ["#/job/JOB_SALES_DAILY", { view: "job", name: "JOB_SALES_DAILY", tab: "tree" }],
    ["#/job/JOB_SALES_DAILY?tab=explain", { view: "job", name: "JOB_SALES_DAILY", tab: "explain" }],
    ["#/df/DF_FACT_SALES", { view: "df", name: "DF_FACT_SALES", transform: undefined, column: undefined }],
    ["#/df/DF_FACT_SALES?t=Q_JOIN&col=Q_JOIN.PRODUCT_KEY", { view: "df", name: "DF_FACT_SALES", transform: "Q_JOIN", column: "Q_JOIN.PRODUCT_KEY" }],
    ["#/dead", { view: "dead" }],
    ["#/load", { view: "load" }],
    ["#/nonsense", { view: "overview" }],
    ["#/job/", { view: "overview" }],
  ];
  it.each(cases)("parses %s", (hash, route) => expect(parseRoute(hash)).toEqual(route));

  it("round-trips through href, including awkward names", () => {
    const routes: Route[] = [
      { view: "job", name: "PKG\\SEQ STAGE/x", tab: "explain" },
      { view: "df", name: "DF_A", transform: "Q&1", column: "DS.STG.T.COL" },
      { view: "dead" },
    ];
    for (const r of routes) {
      const back = parseRoute(href(r));
      expect(back).toMatchObject(r);
    }
  });
});
