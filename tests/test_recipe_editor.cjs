const assert = require("node:assert/strict");
const test = require("node:test");

const {
  isPositiveQuantity,
  parseSourceIngredient,
  rowFromDraft,
} = require("../src/home_kitchen/static/recipe-editor.js");

test("parses a leading exact decimal and recognized explicit unit", () => {
  assert.deepEqual(parseSourceIngredient(" 0.25 kg carrots "), {
    sourceText: " 0.25 kg carrots ",
    name: "carrots",
    quantity: "0.25",
    unit: "kg",
    category: "Other",
    parsed: true,
  });
  assert.equal(parseSourceIngredient("2 ss olivenolje").unit, "ss");
});

test("leaves ambiguous or unrecognized source text unresolved", () => {
  for (const source of ["1-2 cups flour", "½ cup sugar", "1/2 cup sugar", "1,5 kg potatoes", "2 mystery carrots", "salt to taste"]) {
    const row = parseSourceIngredient(source);
    assert.equal(row.sourceText, source);
    assert.equal(row.name, source.trim());
    assert.equal(row.quantity, "");
    assert.equal(row.unit, "");
    assert.equal(row.parsed, false);
  }
});

test("validates exact positive decimal quantities without numeric rounding", () => {
  for (const quantity of ["1", "01.00", ".5", "0.0001"]) assert.equal(isPositiveQuantity(quantity), true);
  for (const quantity of ["", "0", "0.000", "-1", "+1", "1e2", "1-2", "1/2", "1,5", "1.", "."]) {
    assert.equal(isPositiveQuantity(quantity), false, quantity);
  }
});

test("maps structured drafts without presenting a synthesized summary as source text", () => {
  assert.deepEqual(rowFromDraft({ name: "flour", quantity: 2, unit: "cups", category: "Pantry" }), {
    sourceText: "",
    name: "flour",
    quantity: "2",
    unit: "cups",
    category: "Pantry",
    parsed: null,
    hasDraftValues: true,
  });
});
