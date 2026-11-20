const assert = require("node:assert/strict");
const test = require("node:test");

const {
  availabilityStatusLabel,
  buildAvailableCartProposalRequest,
  formatBasketIssue,
  formatDuration,
  formatNeedQuantity,
  mergeNeedPages,
  planKey,
  selectionRecommendation,
  sourceUrl,
  statusLabel,
} = require("../src/home_kitchen/static/basket-match-view.js");

test("fingerprints match scope, quantities, units and dates without depending on row order", () => {
  const first = [
    { id: "b", name: "milk", quantity: "1", unit: "l" },
    { id: "a", name: "rice", quantity: "500", unit: "g" },
  ];
  assert.equal(planKey("2026-11-15", "2026-11-21", first), planKey("2026-11-15", "2026-11-21", [...first].reverse()));
  assert.notEqual(planKey("2026-11-15", "2026-11-21", first), planKey("2026-11-16", "2026-11-22", first));
  assert.notEqual(planKey("2026-11-15", "2026-11-21", first), planKey("2026-11-15", "2026-11-21", [{ ...first[0], quantity: "2" }, first[1]]));
});

test("merges progressive and targeted job snapshots only for current need generations", () => {
  const initial = mergeNeedPages({}, [
    { need_id: "a", page: 1, status: "matched", candidates: [{ id: "old-a" }] },
    { need_id: "b", page: 1, status: "matched", candidates: [{ id: "b" }] },
  ], { a: 1, b: 0 }, { a: 1, b: 0 });
  const staleGlobal = mergeNeedPages(initial, [
    { need_id: "a", page: 1, status: "matched", candidates: [{ id: "stale-a" }] },
  ], { a: 2, b: 0 }, { a: 1, b: 0 });
  const pendingPage = mergeNeedPages(staleGlobal, [
    { need_id: "a", page: 2, status: "pending", candidates: [] },
  ], { a: 2, b: 0 }, { a: 2 });
  const completePage = mergeNeedPages(pendingPage, [
    { need_id: "a", page: 2, status: "matched", candidates: [{ id: "new-a" }] },
  ], { a: 2, b: 0 }, { a: 2 });

  assert.deepEqual(completePage.a[1].candidates, [{ id: "old-a" }]);
  assert.deepEqual(completePage.a[2].candidates, [{ id: "new-a" }]);
  assert.deepEqual(completePage.b[1].candidates, [{ id: "b" }]);
});

test("formats provider durations and labels per-need states", () => {
  assert.equal(formatDuration("P0DT00H20M00S"), "20 min");
  assert.equal(formatDuration("PT1H5M"), "1 t 5 min");
  assert.equal(formatDuration("unknown"), "unknown");
  assert.equal(statusLabel("search_failed"), "Søket mislyktes");
});

test("uses only a complete backend recommendation for compatible explicit selection", () => {
  const candidate = {
    recommended_packages: 2,
    recommended_fulfillment_quantity: "1000",
    pack_quantity: "500",
    pack_unit: "g",
  };
  assert.deepEqual(selectionRecommendation(candidate, "g"), {
    packages: 2,
    fulfillment: "1000",
    packQuantity: "500",
    packUnit: "g",
  });
  assert.equal(selectionRecommendation({ ...candidate, recommended_packages: null }, "g"), null);
  assert.equal(selectionRecommendation(candidate, "ml"), null);
});

test("marks estimated need quantities as approximate and accepts only safe source links", () => {
  assert.equal(formatNeedQuantity({ quantity: "1.25", unit: "g", approximate: true }, (value) => String(value).replace(".", ",")), "ca. 1,25 g");
  assert.equal(formatNeedQuantity({ quantity: "2", unit: "stk", approximate: false }), "2 stk");
  assert.equal(sourceUrl("https://matvaretabellen.no/food"), "https://matvaretabellen.no/food");
  assert.equal(sourceUrl("http://matvaretabellen.no/food"), null);
  assert.equal(sourceUrl("https://user:secret@example.test/food"), null);
});

test("KitchenBasketMatchView delegates basket issue formatting", () => {
  assert.equal(typeof formatBasketIssue, "function");
  const shoppingView = require("../src/home_kitchen/static/shopping-view.js");
  globalThis.KitchenShoppingView = shoppingView;
  assert.equal(
    formatBasketIssue("Tine yoghurt: product evidence is unverified"),
    "Tine yoghurt: produktgrunnlaget er ubekreftet",
  );
  assert.equal(
    formatBasketIssue("Eggs: 1 item still needed"),
    "Eggs: 1 stk. gjenstår",
  );
});

test("mergeNeedPages discards older server revisions within the same match job", () => {
  const newer = mergeNeedPages({}, [
    { need_id: "a", page: 1, match_job_id: "job-1", revision: 2, status: "matched", candidates: [{ id: "new" }] },
  ], { a: 2 }, { a: 2 });
  const stale = mergeNeedPages(newer, [
    { need_id: "a", page: 1, match_job_id: "job-1", revision: 1, status: "matched", candidates: [{ id: "old" }] },
  ], { a: 2 }, { a: 2 });
  assert.deepEqual(stale.a[1].candidates, [{ id: "new" }]);
  assert.equal(stale.a[1].revision, 2);
});

test("buildAvailableCartProposalRequest keeps valid available items and skips unavailable ones", () => {
  const basket = {
    needs: [
      {
        need: { id: "n1", name: "Milk" },
        selections: [{ product: { provider: "oda", product_id: "101", name: "Tine Helmelk" }, packages: 2 }],
      },
      {
        need: { id: "n2", name: "Egg" },
        selections: [{ product: { provider: "oda", product_id: "202", name: "Prior Egg" }, packages: 1 }],
      },
    ],
  };
  const report = {
    items: [
      { need_id: "n1", product_id: "101", availability_status: "available", search_query: "Tine Helmelk" },
      { need_id: "n2", product_id: "202", availability_status: "unavailable", search_query: "Prior Egg" },
    ],
  };
  const filtered = buildAvailableCartProposalRequest(basket, report);
  assert.deepEqual(filtered.additions, [{ product_id: 101, quantity: 2 }]);
  assert.deepEqual(filtered.queries, ["Tine Helmelk"]);
  assert.equal(filtered.skippedCount, 1);
  assert.equal(filtered.availableCount, 1);
  assert.equal(filtered.issue, "");
  assert.equal(availabilityStatusLabel("unavailable"), "Ikke tilgjengelig hos Oda");
  assert.equal(availabilityStatusLabel("available"), "Tilgjengelig hos Oda");
});
