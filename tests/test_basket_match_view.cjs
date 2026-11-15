const assert = require("node:assert/strict");
const test = require("node:test");

const {
  formatDuration,
  mergeNeedPages,
  planKey,
  selectionRecommendation,
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
  assert.equal(formatDuration("PT1H5M"), "1 hr 5 min");
  assert.equal(formatDuration("unknown"), "unknown");
  assert.equal(statusLabel("search_failed"), "Search failed");
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
