const assert = require("node:assert/strict");
const test = require("node:test");

const {
  basketPriceSummary,
  formatQuantityForDisplay,
  formatQuantityMentions,
  groupedByCategory,
  hasSelectedProducts,
  isPositiveDecimal,
  normalizeDecimalText,
  pickupProgress,
} = require("../src/home_kitchen/static/shopping-view.js");

test("does not show a basket total when nothing has been selected", () => {
  assert.equal(hasSelectedProducts({ needs: [{ selections: [] }] }), false);
  assert.equal(basketPriceSummary({ total_cost: "0", currency: "NOK", price_complete: false, needs: [] }), null);
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
    label: "Selected product total: 92.5 NOK",
    accessibleLabel: "Exact selected product total: 92.50 NOK",
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
    "Known subtotal: 25 NOK · some prices are unknown",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: "0",
      currency: "NOK",
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Selected product prices are incomplete.",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: null,
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Selected product prices are incomplete.",
  );
});

test("formats pickup progress and handles empty or inconsistent counts", () => {
  assert.deepEqual(pickupProgress(2, 5), {
    checked: 2,
    total: 5,
    percent: 40,
    label: "2 of 5 picked up",
  });
  assert.deepEqual(pickupProgress(9, 3), {
    checked: 3,
    total: 3,
    percent: 100,
    label: "3 of 3 picked up",
  });
  assert.deepEqual(pickupProgress(0, 0), {
    checked: 0,
    total: 0,
    percent: 0,
    label: "No items to pick up",
  });
});

test("rounds display quantities with decimal-string arithmetic only", () => {
  assert.equal(formatQuantityForDisplay("2.345"), "2.35");
  assert.equal(formatQuantityForDisplay("1.2300"), "1.23");
  assert.equal(formatQuantityForDisplay("0.004"), "<0.01");
  assert.equal(formatQuantityForDisplay("12345678901234567890.005"), "12345678901234567890.01");
  assert.equal(formatQuantityForDisplay("unknown"), "unknown");
  assert.equal(formatQuantityForDisplay("1E+3"), "1000");
  assert.equal(formatQuantityForDisplay("1.25e-3"), "<0.01");
  assert.equal(normalizeDecimalText("1.25e-3"), "0.00125");
  assert.equal(normalizeDecimalText("1,000.5"), null);
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
    "Milk: 233.33 g still needed; 1000 ml overbuy",
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
