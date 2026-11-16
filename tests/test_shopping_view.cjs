const assert = require("node:assert/strict");
const test = require("node:test");

const {
  basketPriceSummary,
  actionableGroups,
  formatQuantityForDisplay,
  formatQuantityForAccessibility,
  formatQuantityMentions,
  groupedByCategory,
  hasSelectedProducts,
  isPositiveDecimal,
  mealOriginNames,
  mergeActionableLines,
  normalizeDecimalText,
  pickupProgress,
} = require("../src/home_kitchen/static/shopping-view.js");

test("does not show a basket total when nothing has been selected", () => {
  assert.equal(hasSelectedProducts({ needs: [{ selections: [] }] }), false);
  assert.equal(basketPriceSummary({ total_cost: "0", currency: "NOK", price_complete: false, needs: [] }), null);
});

test("joins the checked store-trip line to its matching product review by stable ID", () => {
  const line = { id: "need-1", name: "Milk", checked: true, to_buy_quantity: "0.5" };
  const match = { need: { id: "need-1" }, selections: [] };
  assert.deepEqual(mergeActionableLines([line], [match]), [{ line, match }]);
  assert.equal(mergeActionableLines([line], [{ need: { id: "other" } }])[0].match, null);
});

test("recipe grouping keeps shared needs to one actionable row and names every origin", () => {
  const line = { id: "milk", origins: ["meal:1", "meal:2"] };
  const entries = mergeActionableLines([line], []);
  const grouped = actionableGroups(entries, [], [
    { id: "1", recipe_id: "r1" },
    { id: "2", recipe_id: "r2" },
  ], "meal", [
    { id: "r1", title: "Grøt" },
    { id: "r2", title: "Pannekaker" },
  ]);
  assert.equal([...grouped.values()].flat().length, 1);
  assert.equal([...grouped.keys()][0], "Flere måltider");
  assert.deepEqual(mealOriginNames(line.origins, [
    { id: "1", recipe_id: "r1" },
    { id: "2", recipe_id: "r2" },
  ], [
    { id: "r1", title: "Grøt" },
    { id: "r2", title: "Pannekaker" },
  ]), ["Grøt", "Pannekaker"]);
});

test("category grouping sorts and retains actionable row payloads", () => {
  const entries = mergeActionableLines([
    { id: "2", name: "Milk", category: "Dairy" },
    { id: "1", name: "Apples", category: "Fruit" },
  ], []);
  const grouped = actionableGroups(entries, ["Fruit", "Dairy"]);
  assert.deepEqual([...grouped.keys()], ["Fruit", "Dairy"]);
  assert.equal([...grouped.values()].flat()[0].line.name, "Apples");
});

test("shows a complete total only for selected products with complete prices", () => {
  const basket = {
    total_cost: "92.50",
    currency: "NOK",
    price_complete: true,
    needs: [{ selections: [{ product: { product_id: "42" } }] }],
  };
  assert.equal(hasSelectedProducts(basket), true);
  assert.deepEqual(basketPriceSummary(basket), {
    label: "Sum for valgte varer: 92,5 NOK",
    accessibleLabel: "Nøyaktig sum for valgte varer: 92,5 NOK",
    complete: true,
  });
});

test("labels partial price data as a subtotal and suppresses a misleading zero total", () => {
  const selected = { product: { product_id: "42" } };
  assert.equal(
    basketPriceSummary({
      total_cost: "25.00",
      currency: "NOK",
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Kjent delsum: 25 NOK · noen priser er ukjente",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: "0",
      currency: "NOK",
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Prisene for de valgte varene er ufullstendige.",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: null,
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Prisene for de valgte varene er ufullstendige.",
  );
});

test("formats pickup progress and handles empty or inconsistent counts", () => {
  assert.deepEqual(pickupProgress(2, 5), {
    checked: 2,
    total: 5,
    percent: 40,
    label: "2 av 5 varer plukket",
  });
  assert.deepEqual(pickupProgress(9, 3), {
    checked: 3,
    total: 3,
    percent: 100,
    label: "3 av 3 varer plukket",
  });
  assert.deepEqual(pickupProgress(0, 0), {
    checked: 0,
    total: 0,
    percent: 0,
    label: "Ingen varer å plukke",
  });
});

test("rounds display quantities with decimal-string arithmetic only", () => {
  assert.equal(formatQuantityForDisplay("2.345"), "2,35");
  assert.equal(formatQuantityForDisplay("1.2300"), "1,23");
  assert.equal(formatQuantityForDisplay("0.004"), "<0,01");
  assert.equal(formatQuantityForDisplay("12345678901234567890.005"), "12345678901234567890,01");
  assert.equal(formatQuantityForDisplay("unknown"), "unknown");
  assert.equal(formatQuantityForDisplay("1E+3"), "1000");
  assert.equal(formatQuantityForDisplay("1.25e-3"), "<0,01");
  assert.equal(normalizeDecimalText("1.25e-3"), "0.00125");
  assert.equal(normalizeDecimalText("1,000.5"), null);
});

test("formats accessible quantities compactly without hiding small positive amounts", () => {
  assert.equal(formatQuantityForAccessibility("233.3333333333333333333333334"), "233,33");
  assert.equal(formatQuantityForAccessibility("1.2300"), "1,23");
  assert.equal(formatQuantityForAccessibility("1E+3"), "1000");
  assert.equal(formatQuantityForAccessibility("0.0001"), "<0,01");
  assert.equal(formatQuantityForAccessibility("not-a-number"), "not-a-number");
});

test("detects positive decimal text without floating-point coercion", () => {
  assert.equal(isPositiveDecimal("0.0001"), true);
  assert.equal(isPositiveDecimal("0.000"), false);
  assert.equal(isPositiveDecimal("1e2"), true);
  assert.equal(isPositiveDecimal("1,000.5"), false);
});

test("formats decimal quantities inside basket issue text", () => {
  assert.equal(
    formatQuantityMentions("Milk: 233.3333333333333333333333334 g still needed; 1E+3 ml overbuy"),
    "Milk: 233,33 g still needed; 1000 ml overbuy",
  );
});

test("supports a configured category walking order with a stable alphabetical fallback", () => {
  const lines = [
    { name: "Milk", category: "Dairy" },
    { name: "Bread", category: "Bakery" },
    { name: "Apples", category: "Produce" },
    { name: "Oats", category: "Pantry" },
  ];
  assert.deepEqual(
    [...groupedByCategory(lines, ["Produce", "Bakery"]).keys()],
    ["Produce", "Bakery", "Dairy", "Pantry"],
  );
  assert.deepEqual([...groupedByCategory(lines).keys()], ["Bakery", "Dairy", "Pantry", "Produce"]);
});
