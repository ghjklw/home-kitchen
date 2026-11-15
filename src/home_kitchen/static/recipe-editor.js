"use strict";
(function (root) {
  const UNITS = new Set([
    "g",
    "kg",
    "mg",
    "ml",
    "l",
    "dl",
    "cl",
    "tsp",
    "ts",
    "teskje",
    "teskjeer",
    "tbsp",
    "ss",
    "spiseskje",
    "spiseskjeer",
    "cup",
    "cups",
    "teaspoon",
    "teaspoons",
    "tablespoon",
    "tablespoons",
    "oz",
    "lb",
    "pinch",
    "pinches",
    "klype",
    "klyper",
    "stk",
    "piece",
    "pieces",
    "pc",
    "pcs",
    "item",
    "items",
    "can",
    "cans",
    "boks",
    "bokser",
    "tin",
    "tins",
    "clove",
    "cloves",
    "fedd",
    "pack",
    "packs",
    "pk",
    "packet",
    "packets",
    "pose",
    "poser",
    "bunch",
    "bunches",
    "bunt",
    "bunter",
    "slice",
    "slices",
    "skive",
    "skiver",
    "head",
    "heads",
    "hode",
    "hoder",
  ]);

  function isPositiveQuantity(value) {
    if (typeof value !== "string") return false;
    const quantity = value.trim();
    return (
      /^(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$/u.test(quantity) &&
      /[1-9]/u.test(quantity.replace(".", ""))
    );
  }

  function emptyRow(sourceText = "") {
    return {
      sourceText,
      name: sourceText.trim(),
      quantity: "",
      unit: "",
      category: "Other",
      parsed: false,
    };
  }

  function parseSourceIngredient(value) {
    const sourceText = String(value ?? "");
    const match = /^\s*((?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s+(\S+)\s+(.+?)\s*$/u.exec(
      sourceText,
    );
    if (!match || !isPositiveQuantity(match[1])) return emptyRow(sourceText);

    const unitKey = match[2].toLowerCase().replace(/\.$/u, "");
    const name = match[3].trim();
    if (!UNITS.has(unitKey) || !name) return emptyRow(sourceText);

    return {
      sourceText,
      name,
      quantity: match[1],
      unit: match[2],
      category: "Other",
      parsed: true,
    };
  }

  function rowFromDraft(value) {
    if (typeof value === "string") return parseSourceIngredient(value);
    if (!value || typeof value !== "object") return emptyRow();
    const quantity = value.quantity == null ? "" : String(value.quantity);
    const unit = value.unit == null ? "" : String(value.unit);
    const name = value.name == null ? "" : String(value.name);
    const sourceText = [quantity, unit, name].filter(Boolean).join(" ");
    return {
      sourceText,
      name,
      quantity,
      unit,
      category: value.category == null ? "Other" : String(value.category),
      parsed: false,
    };
  }

  root.KitchenRecipeEditor = {
    isPositiveQuantity,
    parseSourceIngredient,
    rowFromDraft,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeEditor;
  }
})(globalThis);
