const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildPlannedCookingView,
  formatPlannedIngredient,
  formatPlannedIngredientList,
} = require("../src/home_kitchen/static/planned-cooking.js");

test("keeps source recipe content while using planned-serving ingredient quantities", () => {
  const sourceRecipe = {
    id: "r1",
    servings: 3,
    instructions: ["Cook for 15 minutes."],
    image_url: "https://example.test/recipe.jpg",
    provenance: { original_source: "imported", source_url: "https://example.test/original" },
  };
  const scaledIngredients = [{ name: "rice", quantity: "233.3333333333333333333333333", unit: "g" }];
  const view = buildPlannedCookingView({
    meal: { id: "m1", servings: 7, recipe_id: "r1" },
    source_recipe: sourceRecipe,
    servings: 7,
    ingredients: scaledIngredients,
  });

  assert.equal(view.servings, 7);
  assert.equal(view.sourceRecipe, sourceRecipe);
  assert.deepEqual(view.ingredients, scaledIngredients);
  assert.equal(view.sourceRecipe.servings, 3);
  assert.deepEqual(view.sourceRecipe.instructions, ["Cook for 15 minutes."]);
  assert.deepEqual(view.sourceRecipe.provenance, { original_source: "imported", source_url: "https://example.test/original" });
  assert.throws(() => buildPlannedCookingView({}), /kunne ikke åpnes/);
});

test("uses readable quantities and a spoken label at the same precision", () => {
  const line = formatPlannedIngredient(
    { name: "pasta", quantity: "233.3333333333333333333333333", unit: "g" },
    () => "233.33",
  );

  assert.deepEqual(line, {
    text: "233.33 g pasta",
    accessibleLabel: "Mengde: 233.33 gram pasta",
    copyText: "233.33 g pasta",
  });
  assert.ok(!line.accessibleLabel.includes("233.3333333333333333333333333"));
});

test("localizes known source units in cooking, accessibility, and copied ingredients", () => {
  assert.deepEqual(formatPlannedIngredient({ name: "egg", quantity: "2", unit: "item" }), {
    text: "2 stk. egg",
    accessibleLabel: "Mengde: 2 stykk egg",
    copyText: "2 stk. egg",
  });
  assert.deepEqual(formatPlannedIngredient({ name: "salt", quantity: "1", unit: "tsp" }), {
    text: "1 ts salt",
    accessibleLabel: "Mengde: 1 teskje salt",
    copyText: "1 ts salt",
  });
  assert.equal(formatPlannedIngredient({ name: "salt", quantity: "2", unit: "pinches" }).text, "2 klyper salt");
  assert.equal(formatPlannedIngredient({ name: "beans", quantity: "2", unit: "custom-unit" }).text, "2 custom-unit beans");
});

test("keeps very small positive and zero cooking quantities truthful", () => {
  const format = (value) => value === "0.004" ? "<0.01" : value;
  assert.deepEqual(
    formatPlannedIngredient({ name: "salt", quantity: "0.004", unit: "g" }, format),
    {
      text: "<0.01 g salt",
      accessibleLabel: "Mengde: <0.01 gram salt",
      copyText: "<0.01 g salt",
    },
  );
  assert.deepEqual(
    formatPlannedIngredient({ name: "water", quantity: "0", unit: "ml" }),
    {
      text: "0 ml water",
      accessibleLabel: "Mengde: 0 milliliter water",
      copyText: "0 ml water",
    },
  );
});

test("keeps unresolved cooking amounts explicit instead of displaying null", () => {
  assert.deepEqual(formatPlannedIngredient({ name: "salt", quantity: null, unit: null }), {
    text: "Mengde ikke oppgitt · salt",
    accessibleLabel: "Mengde ikke oppgitt for salt",
    copyText: "Mengde ikke oppgitt · salt",
  });
});

test("shares the same rounded amounts shown in planned cooking", () => {
  const text = formatPlannedIngredientList(
    {
      servings: 7,
      sourceRecipe: { title: "Tomato pasta" },
      ingredients: [
        { name: "pasta", quantity: "233.3333333333333333333333333", unit: "g" },
        { name: "salt", quantity: null, unit: null },
      ],
    },
    () => "233.33",
  );

  assert.equal(
    text,
    "Tomato pasta\nTil 7 porsjoner\n\nIngredienser:\n- 233.33 g pasta\n- Mengde ikke oppgitt · salt",
  );
});

test("localizes cooking units for en and fr household locales while preserving nb default and unmapped units", () => {
  assert.deepEqual(formatPlannedIngredient({ name: "egg", quantity: "2", unit: "item" }, (v) => v, "en"), {
    text: "2 pcs egg",
    accessibleLabel: "Mengde: 2 pieces egg",
    copyText: "2 pcs egg",
  });
  assert.deepEqual(formatPlannedIngredient({ name: "salt", quantity: "1", unit: "tsp" }, (v) => v, "en"), {
    text: "1 tsp salt",
    accessibleLabel: "Mengde: 1 teaspoon salt",
    copyText: "1 tsp salt",
  });
  assert.deepEqual(formatPlannedIngredient({ name: "farine", quantity: "2", unit: "tbsp" }, (v) => v, "fr"), {
    text: "2 c. à s. farine",
    accessibleLabel: "Mengde: 2 cuillères à soupe farine",
    copyText: "2 c. à s. farine",
  });
  assert.deepEqual(formatPlannedIngredient({ name: "crème", quantity: "4", unit: "dl" }, (v) => v, "fr"), {
    text: "4 dl crème",
    accessibleLabel: "Mengde: 4 décilitres crème",
    copyText: "4 dl crème",
  });
  assert.equal(
    formatPlannedIngredient({ name: "muscade", quantity: "2", unit: "pincée" }, (v) => v, "fr").text,
    "2 pincée muscade",
  );
  assert.equal(
    formatPlannedIngredient({ name: "egg", quantity: "2", unit: "item" }, (v) => v, undefined).text,
    "2 stk. egg",
  );
  const listEn = formatPlannedIngredientList(
    {
      servings: 2,
      sourceRecipe: { title: "Omelette" },
      ingredients: [{ name: "egg", quantity: "2", unit: "item" }],
    },
    (v) => v,
    "en",
  );
  assert.match(listEn, /- 2 pcs egg/);
});
