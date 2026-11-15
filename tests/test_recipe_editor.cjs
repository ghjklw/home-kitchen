const assert = require("node:assert/strict");
const test = require("node:test");

const {
  isPositiveQuantity,
  buildRevisionRequest,
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

test("AI revision keeps an adapted draft editable when quantities need review", () => {
  const payload = buildRevisionRequest({
    title: "Soup",
    servings: "4",
    ingredients: [{ name: "carrots", quantity: "", unit: "", source_indices: [0] }],
    instructions: "Chop\nSimmer",
    source_url: "https://example.test/soup",
    image_url: "",
    source_ingredients: [{ index: 0, text: "carrots to taste" }],
    provenance: { kind: "ai_adaptation", original_source: "imported", source_url: "https://example.test/soup" },
  }, { isEditing: false, source: "ai" }, " make it vegan ");

  assert.deepEqual(payload, {
    draft: {
      title: "Soup",
      servings: 4,
      ingredients: [{ name: "carrots", quantity: null, unit: null, category: "Other", source_indices: [0] }],
      instructions: ["Chop", "Simmer"],
      source_url: "https://example.test/soup",
      image_url: null,
      source_ingredients: [{ index: 0, text: "carrots to taste" }],
      provenance: { kind: "ai_adaptation", original_source: "imported", source_url: "https://example.test/soup" },
      source: "ai",
    },
    prompt: "make it vegan",
  });
});

test("AI revision sends complete saved recipes and requires explicit quantities and units", () => {
  const values = {
    title: "Bread", servings: 2,
    ingredients: [{ name: "flour", quantity: "1,25", unit: "kg", source_indices: [1] }],
    instructions: ["Mix"], source_ingredients: [{ index: 1, text: "1.25 kg flour" }],
    provenance: { kind: "ai_adaptation", source_url: null },
  };
  assert.deepEqual(buildRevisionRequest(values, { isEditing: true, source: "imported" }, " halve it "), {
    recipe: {
      title: "Bread", servings: 2,
      ingredients: [{ name: "flour", quantity: "1.25", unit: "kg", category: "Other", source_indices: [1] }],
      instructions: ["Mix"], source_url: null, image_url: null,
      source_ingredients: [{ index: 1, text: "1.25 kg flour" }],
      provenance: { kind: "ai_adaptation", source_url: null }, source: "imported",
    },
    prompt: "halve it",
  });
  assert.throws(
    () => buildRevisionRequest({ ...values, ingredients: [{ name: "flour", quantity: "", unit: "kg" }] }, { isEditing: true, source: "manual" }, "revise"),
    /Complete each quantity and unit/,
  );
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
