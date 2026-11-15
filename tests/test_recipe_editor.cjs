const assert = require("node:assert/strict");
const test = require("node:test");

const {
  isPositiveQuantity,
  normalizeQuantity,
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
  assert.equal(parseSourceIngredient("0,5 kg gulrøtter").quantity, "0.5");
});

test("leaves ambiguous or unrecognized source text unresolved", () => {
  for (const source of ["1-2 cups flour", "½ cup sugar", "1/2 cup sugar", "1,000.5 kg potatoes", "2 mystery carrots", "salt to taste"]) {
    const row = parseSourceIngredient(source);
    assert.equal(row.sourceText, source);
    assert.equal(row.name, source.trim());
    assert.equal(row.quantity, "");
    assert.equal(row.unit, "");
    assert.equal(row.parsed, false);
  }
});

test("accepts decimal comma or point and normalizes without numeric rounding", () => {
  for (const quantity of ["1", "01.00", ".5", "0.0001", "0,5", "1,25"]) assert.equal(isPositiveQuantity(quantity), true);
  assert.equal(normalizeQuantity("0,5"), "0.5");
  assert.equal(normalizeQuantity("1.25"), "1.25");
  for (const quantity of ["", "0", "0,000", "-1", "+1", "1e2", "1-2", "1/2", "1,000.5", "1.2,5", "1,2,3", "1.", "."]) {
    assert.equal(isPositiveQuantity(quantity), false, quantity);
  }
});

test("maps structured drafts without presenting a synthesized summary as source text", () => {
  assert.deepEqual(rowFromDraft({ name: "flour", quantity: 2, unit: "cups", category: "Pantry" }), {
    sourceText: "",
    source_indices: [],
    name: "flour",
    quantity: "2",
    unit: "cups",
    category: "Pantry",
    parsed: null,
    hasDraftValues: true,
  });
  assert.deepEqual(rowFromDraft({ name: "flour", quantity: "1", unit: "kg", source_indices: [2] }).source_indices, [2]);
});
