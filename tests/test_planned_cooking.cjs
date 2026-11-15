const assert = require("node:assert/strict");
const test = require("node:test");

const { buildPlannedCookingView } = require("../src/home_kitchen/static/planned-cooking.js");

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
  assert.throws(() => buildPlannedCookingView({}), /could not be opened/);
});
