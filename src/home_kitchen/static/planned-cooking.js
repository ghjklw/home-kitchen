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

  function resolveLanguage(locale = "nb") {
    const code = String(locale ?? "").trim().toLowerCase().split(/[-_]/u)[0];
    if (code === "en" || code === "fr") return code;
    return "nb";
  }

  function localizedCookingUnits(unitKey, singular, sourceUnit, locale = "nb") {
    const lang = resolveLanguage(locale);
    const tables = {
      nb: {
        item: ["stk.", "stykk"],
        items: ["stk.", "stykk"],
        pc: ["stk.", "stykk"],
        pcs: ["stk.", "stykk"],
        stk: ["stk.", "stykk"],
        "stk.": ["stk.", "stykk"],
        tsp: ["ts", singular ? "teskje" : "teskjeer"],
        teaspoon: ["ts", singular ? "teskje" : "teskjeer"],
        teaspoons: ["ts", "teskjeer"],
        tbsp: ["ss", singular ? "spiseskje" : "spiseskjeer"],
        tablespoon: ["ss", singular ? "spiseskje" : "spiseskjeer"],
        tablespoons: ["ss", "spiseskjeer"],
        pack: [singular ? "pakke" : "pakker", singular ? "pakke" : "pakker"],
        packs: ["pakker", "pakker"],
        pinch: [singular ? "klype" : "klyper", singular ? "klype" : "klyper"],
        pinches: ["klyper", "klyper"],
        g: ["g", "gram"],
        kg: ["kg", "kilogram"],
        mg: ["mg", "milligram"],
        ml: ["ml", "milliliter"],
        cl: ["cl", "centiliter"],
        dl: ["dl", "desiliter"],
        l: ["l", "liter"],
      },
      en: {
        item: [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        items: [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        pc: [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        pcs: [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        stk: [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        "stk.": [singular ? "pc" : "pcs", singular ? "piece" : "pieces"],
        tsp: ["tsp", singular ? "teaspoon" : "teaspoons"],
        teaspoon: ["tsp", singular ? "teaspoon" : "teaspoons"],
        teaspoons: ["tsp", "teaspoons"],
        tbsp: ["tbsp", singular ? "tablespoon" : "tablespoons"],
        tablespoon: ["tbsp", singular ? "tablespoon" : "tablespoons"],
        tablespoons: ["tbsp", "tablespoons"],
        pack: [singular ? "pack" : "packs", singular ? "pack" : "packs"],
        packs: ["packs", "packs"],
        pinch: [singular ? "pinch" : "pinches", singular ? "pinch" : "pinches"],
        pinches: ["pinches", "pinches"],
        g: ["g", singular ? "gram" : "grams"],
        kg: ["kg", singular ? "kilogram" : "kilograms"],
        mg: ["mg", singular ? "milligram" : "milligrams"],
        ml: ["ml", singular ? "milliliter" : "milliliters"],
        cl: ["cl", singular ? "centiliter" : "centiliters"],
        dl: ["dl", singular ? "deciliter" : "deciliters"],
        l: ["l", singular ? "liter" : "liters"],
      },
      fr: {
        item: [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        items: [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        pc: [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        pcs: [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        stk: [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        "stk.": [singular ? "pièce" : "pièces", singular ? "pièce" : "pièces"],
        tsp: ["c. à c.", singular ? "cuillère à café" : "cuillères à café"],
        teaspoon: ["c. à c.", singular ? "cuillère à café" : "cuillères à café"],
        teaspoons: ["c. à c.", "cuillères à café"],
        tbsp: ["c. à s.", singular ? "cuillère à soupe" : "cuillères à soupe"],
        tablespoon: ["c. à s.", singular ? "cuillère à soupe" : "cuillères à soupe"],
        tablespoons: ["c. à s.", "cuillères à soupe"],
        pack: [singular ? "paquet" : "paquets", singular ? "paquet" : "paquets"],
        packs: ["paquets", "paquets"],
        pinch: [singular ? "pincée" : "pincées", singular ? "pincée" : "pincées"],
        pinches: ["pincées", "pincées"],
        g: ["g", singular ? "gramme" : "grammes"],
        kg: ["kg", singular ? "kilogramme" : "kilogrammes"],
        mg: ["mg", singular ? "milligramme" : "milligrammes"],
        ml: ["ml", singular ? "millilitre" : "millilitres"],
        cl: ["cl", singular ? "centilitre" : "centilitres"],
        dl: ["dl", singular ? "décilitre" : "décilitres"],
        l: ["l", singular ? "litre" : "litres"],
      },
    };
    const entry = (tables[lang] || tables.nb)[unitKey];
    return entry ? { displayUnit: entry[0], spokenUnit: entry[1] } : { displayUnit: sourceUnit, spokenUnit: sourceUnit };
  }

  function formatPlannedIngredient(item, formatQuantity = (value) => value, locale = "nb") {
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
    const { displayUnit, spokenUnit } = localizedCookingUnits(unitKey, singular, sourceUnit, locale);
    const text = displayQuantity + " " + displayUnit + " " + name;
    return {
      text,
      accessibleLabel: "Mengde: " + displayQuantity + " " + spokenUnit + " " + name,
      copyText: text,
    };
  }

  function formatPlannedIngredientList(view, formatQuantity = (value) => value, locale = "nb") {
    const title = String(view?.sourceRecipe?.title || "Oppskrift").trim();
    const ingredients = (view?.ingredients || []).map((item) =>
      formatPlannedIngredient(item, formatQuantity, locale).copyText,
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
