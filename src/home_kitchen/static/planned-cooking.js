(function (root) {
  "use strict";

  function buildPlannedCookingView(response) {
    const sourceRecipe = response?.source_recipe;
    if (!response?.meal || !sourceRecipe || !Array.isArray(response.ingredients))
      throw new Error("Den planlagte oppskriften kunne ikke åpnes. Den lagrede oppskriften er uendret.");
    const servings = Number(response.servings);
    if (!Number.isInteger(servings) || servings < 1)
      throw new Error("Antall porsjoner for måltidet er ikke tilgjengelig. Den lagrede oppskriften er uendret.");
    if (!Array.isArray(sourceRecipe.instructions) || !sourceRecipe.instructions.length)
      throw new Error("Denne oppskriften har ingen fremgangsmåte ennå.");
    return {
      meal: response.meal,
      sourceRecipe,
      servings,
      ingredients: response.ingredients,
    };
  }

  function formatPlannedIngredient(item, formatQuantity = (value) => value) {
    const name = String(item?.name || "Ingrediens").trim();
    const unit = String(item?.unit || "").trim();
    if (item?.quantity == null || !unit) {
      const text = "Mengde ikke oppgitt · " + name;
      return {
        text,
        accessibleLabel: "Mengde ikke oppgitt for " + name,
        copyText: text,
      };
    }
    const exact = String(item.quantity);
    const displayQuantity = String(formatQuantity(exact));
    const text = displayQuantity + " " + unit + " " + name;
    const spokenUnit =
      {
        g: "gram",
        kg: "kilogram",
        mg: "milligram",
        ml: "milliliter",
        cl: "centiliter",
        dl: "desiliter",
        l: "liter",
        tsp: "teskjeer",
        tbsp: "spiseskjeer",
        item: "stk.",
        items: "stk.",
        pc: "stk.",
        pcs: "stk.",
        stk: "stk.",
        pinch: "klype",
        pinches: "klyper",
      }[unit.toLowerCase()] || unit;
    return {
      text,
      accessibleLabel: "Mengde: " + displayQuantity + " " + spokenUnit + " " + name,
      copyText: text,
    };
  }

  function formatPlannedIngredientList(view, formatQuantity = (value) => value) {
    const title = String(view?.sourceRecipe?.title || "Oppskrift").trim();
    const ingredients = (view?.ingredients || []).map((item) =>
      formatPlannedIngredient(item, formatQuantity).copyText,
    );
    return [
      title,
      "Til " + view.servings + " porsjoner",
      "",
      "Ingredienser:",
      ...(ingredients.length ? ingredients.map((item) => "- " + item) : ["- Ingen ingredienser oppført"]),
    ].join("\n");
  }

  root.KitchenPlannedCooking = {
    buildPlannedCookingView,
    formatPlannedIngredient,
    formatPlannedIngredientList,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenPlannedCooking;
})(globalThis);
