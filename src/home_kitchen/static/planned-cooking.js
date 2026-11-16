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
    const sourceUnit = String(item?.unit || "").trim();
    if (item?.quantity == null || !sourceUnit) {
      const text = "Mengde ikke oppgitt · " + name;
      return {
        text,
        accessibleLabel: "Mengde ikke oppgitt for " + name,
        copyText: text,
      };
    }
    const exact = String(item.quantity);
    const displayQuantity = String(formatQuantity(exact));
    const unitKey = sourceUnit.toLowerCase();
    const singular = /^(?:1|1\.0+)$/u.test(exact);
    const displayUnit = ({
      item: "stk.", items: "stk.", pc: "stk.", pcs: "stk.", stk: "stk.",
      tsp: "ts", teaspoon: "ts", teaspoons: "ts",
      tbsp: "ss", tablespoon: "ss", tablespoons: "ss",
      pinch: "klype", pinches: "klyper",
    })[unitKey] || sourceUnit;
    const spokenUnit = ({
      g: "gram", kg: "kilogram", mg: "milligram", ml: "milliliter", cl: "centiliter", dl: "desiliter", l: "liter",
      tsp: singular ? "teskje" : "teskjeer", teaspoon: singular ? "teskje" : "teskjeer", teaspoons: "teskjeer",
      tbsp: singular ? "spiseskje" : "spiseskjeer", tablespoon: singular ? "spiseskje" : "spiseskjeer", tablespoons: "spiseskjeer",
      item: "stykk", items: "stykk", pc: "stykk", pcs: "stykk", stk: "stykk",
      pinch: singular ? "klype" : "klyper", pinches: "klyper",
    })[unitKey] || sourceUnit;
    const text = displayQuantity + " " + displayUnit + " " + name;
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
