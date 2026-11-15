const assert = require("node:assert/strict");
const test = require("node:test");

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
    "Kitchen shopping list · Nov 16–22, 2026\n\nProduce:\n  - Carrots — 0.125 kg",
  );
});

test("excludes checked, zero-covered, and invalid quantity lines", () => {
  const lines = [
    { name: "Picked", category: "Other", to_buy_quantity: "2", unit: "item", checked: true },
    { name: "Covered", category: "Other", to_buy_quantity: "0.000", unit: "item" },
    { name: "Invalid", category: "Other", to_buy_quantity: "1e2", unit: "item" },
  ];

  assert.ok(lines.every((line) => !isRemainingShoppingLine(line)));
  assert.equal(formatRemainingShoppingText("Week", lines), "Week\n\nNo items remaining.");
});

test("orders categories then names deterministically and keeps decimal strings exact", () => {
  const text = formatRemainingShoppingText("Week", [
    { name: "zucchini", category: "Produce", to_buy_quantity: "1.500", unit: "kg" },
    { name: "Oats", category: "Pantry", to_buy_quantity: "2.00", unit: "bag" },
    { name: "Apples", category: "Produce", to_buy_quantity: "10.25", unit: "item" },
  ]);

  assert.equal(
    text,
    "Week\n\nPantry:\n  - Oats — 2.00 bag\n\nProduce:\n  - Apples — 10.25 item\n  - zucchini — 1.500 kg",
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
