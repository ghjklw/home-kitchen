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

  function formatPlannedIngredient(item, formatQuantity = (value) => value) {
    const name = String(item?.name || "Ingredient").trim();
    const unit = String(item?.unit || "").trim();
    if (item?.quantity == null || !unit) {
      const text = "Quantity not specified · " + name;
      return {
        text,
        accessibleLabel: "Quantity not specified for " + name,
        copyText: text,
      };
    }
    const exact = String(item.quantity);
    const displayQuantity = String(formatQuantity(exact));
    const text = displayQuantity + " " + unit + " " + name;
    const spokenUnit =
      {
        g: "grams",
        kg: "kilograms",
        mg: "milligrams",
        ml: "milliliters",
        cl: "centiliters",
        dl: "deciliters",
        l: "liters",
        tsp: "teaspoons",
        tbsp: "tablespoons",
        item: "items",
        items: "items",
        pc: "pieces",
        pcs: "pieces",
        stk: "pieces",
        pinch: "pinch",
        pinches: "pinches",
      }[unit.toLowerCase()] || unit;
    return {
      text,
      accessibleLabel: "Amount: " + displayQuantity + " " + spokenUnit + " " + name,
      copyText: text,
    };
  }

  function formatPlannedIngredientList(view, formatQuantity = (value) => value) {
    const title = String(view?.sourceRecipe?.title || "Recipe").trim();
    const ingredients = (view?.ingredients || []).map((item) =>
      formatPlannedIngredient(item, formatQuantity).copyText,
    );
    return [
      title,
      "For " + view.servings + " servings",
      "",
      "Ingredients:",
      ...(ingredients.length ? ingredients.map((item) => "- " + item) : ["- No ingredients listed"]),
    ].join("\n");
  }

  root.KitchenPlannedCooking = {
    buildPlannedCookingView,
    formatPlannedIngredient,
    formatPlannedIngredientList,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenPlannedCooking;
})(globalThis);
