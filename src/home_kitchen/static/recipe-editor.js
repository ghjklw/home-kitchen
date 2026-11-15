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

  function normalizeQuantity(value) {
    if (typeof value !== "string") return null;
    const quantity = value.trim();
    if (!/^(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)$/u.test(quantity)) return null;
    const normalized = quantity.replace(",", ".");
    return /[1-9]/u.test(normalized.replace(".", "")) ? normalized : null;
  }

  function isPositiveQuantity(value) {
    return normalizeQuantity(value) !== null;
  }

  function emptyRow(sourceText = "") {
    return {
      sourceText,
      source_indices: [],
      name: sourceText.trim(),
      quantity: "",
      unit: "",
      category: "Other",
      parsed: false,
    };
  }

  function parseSourceIngredient(value) {
    const sourceText = String(value ?? "");
    const match = /^\s*((?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+))\s+(\S+)\s+(.+?)\s*$/u.exec(
      sourceText,
    );
    if (!match || !isPositiveQuantity(match[1])) return emptyRow(sourceText);

    const unitKey = match[2].toLowerCase().replace(/\.$/u, "");
    const name = match[3].trim();
    if (!UNITS.has(unitKey) || !name) return emptyRow(sourceText);

    return {
      sourceText,
      name,
      quantity: normalizeQuantity(match[1]),
      unit: match[2],
      category: "Other",
      parsed: true,
    };
  }

  function rowFromDraft(value, sourceIndex) {
    if (typeof value === "string") {
      const row = parseSourceIngredient(value);
      if (Number.isSafeInteger(sourceIndex) && sourceIndex >= 0)
        row.source_indices = [sourceIndex];
      return row;
    }
    if (!value || typeof value !== "object") return emptyRow();
    const quantity = value.quantity == null ? "" : String(value.quantity);
    const unit = value.unit == null ? "" : String(value.unit);
    const name = value.name == null ? "" : String(value.name);
    return {
      sourceText: "",
      source_indices: Array.isArray(value.source_indices)
        ? [...value.source_indices]
        : [],
      name,
      quantity,
      unit,
      category: value.category == null ? "Other" : String(value.category),
      parsed: null,
      hasDraftValues: Boolean(name || quantity || unit),
    };
  }

  root.KitchenRecipeEditor = {
    isPositiveQuantity,
    normalizeQuantity,
    parseSourceIngredient,
    rowFromDraft,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeEditor;
  }
})(globalThis);
