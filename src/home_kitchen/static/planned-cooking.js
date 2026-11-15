(function (root) {
  "use strict";

  function buildPlannedCookingView(response) {
    const sourceRecipe = response?.source_recipe;
    if (!response?.meal || !sourceRecipe || !Array.isArray(response.ingredients))
      throw new Error("The planned recipe could not be opened. Your saved recipe is unchanged.");
    const servings = Number(response.servings);
    if (!Number.isInteger(servings) || servings < 1)
      throw new Error("The planned serving count is unavailable. Your saved recipe is unchanged.");
    if (!Array.isArray(sourceRecipe.instructions) || !sourceRecipe.instructions.length)
      throw new Error("This recipe has no cooking instructions yet.");
    return {
      meal: response.meal,
      sourceRecipe,
      servings,
      ingredients: response.ingredients,
    };
  }

  root.KitchenPlannedCooking = { buildPlannedCookingView };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenPlannedCooking;
})(globalThis);
