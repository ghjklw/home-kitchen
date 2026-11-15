const assert = require("node:assert/strict");
const test = require("node:test");

require("../src/home_kitchen/static/shopping-view.js");
const {
  formatRemainingShoppingText,
  isRemainingShoppingLine,
} = require("../src/home_kitchen/static/shopping-share.js");

test("formats a date-titled remaining list with category headings and exact decimals", () => {
  const text = formatRemainingShoppingText("Kitchen shopping list · Nov 16–22, 2026", [
    {
      name: "Carrots",
      category: "Produce",
      to_buy_quantity: "0.125",
      unit: "kg",
    },
  ]);

  assert.equal(
    text,
      "Kitchen shopping list · Nov 16–22, 2026\n\nProduce:\n  - Carrots — 0.13 kg",
  );
});

test("excludes checked, zero-covered, and invalid quantity lines", () => {
  const lines = [
    { name: "Picked", category: "Other", to_buy_quantity: "2", unit: "item", checked: true },
    { name: "Covered", category: "Other", to_buy_quantity: "0.000", unit: "item" },
    { name: "Invalid", category: "Other", to_buy_quantity: "1,000.5", unit: "item" },
  ];

  assert.ok(lines.every((line) => !isRemainingShoppingLine(line)));
  assert.equal(formatRemainingShoppingText("Week", lines), "Week\n\nNo items remaining.");
});

test("accepts canonical exponential decimals from the backend for remaining lines", () => {
  const text = formatRemainingShoppingText("Week", [
    { name: "Milk", category: "Dairy", to_buy_quantity: "1E+3", unit: "ml" },
  ]);
  assert.equal(text, "Week\n\nDairy:\n  - Milk — 1000 ml");
});

test("uses a configured category order and readable decimal amounts", () => {
  const text = formatRemainingShoppingText("Week", [
    { name: "zucchini", category: "Produce", to_buy_quantity: "1.500", unit: "kg" },
    { name: "Oats", category: "Pantry", to_buy_quantity: "2.00", unit: "bag" },
    { name: "Apples", category: "Produce", to_buy_quantity: "10.25", unit: "item" },
  ], ["Produce", "Pantry"]);

  assert.equal(
    text,
    "Week\n\nProduce:\n  - Apples — 10.25 item\n  - zucchini — 1.5 kg\n\nPantry:\n  - Oats — 2 bag",
  );
});

test("collapses newlines in categories, names, and units before formatting", () => {
  const text = formatRemainingShoppingText("Week", [
    {
      name: "Milk\nProduce: fake",
      category: "Dairy\nFruit:",
      to_buy_quantity: "1",
      unit: "item\n  - forged",
    },
  ]);

  assert.equal(
    text,
    "Week\n\nDairy Fruit::\n  - Milk Produce: fake — 1 item - forged",
  );
  assert.equal(text.split("\n").length, 4);
});
