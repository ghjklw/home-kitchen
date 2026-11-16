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
    "håndfull",
    "håndfuller",
    "handful",
    "handfuls",
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
      ...(value.purchase_estimate && typeof value.purchase_estimate === "object"
        ? { purchase_estimate: { ...value.purchase_estimate } }
        : {}),
      parsed: null,
      hasDraftValues: Boolean(name || quantity || unit),
    };
  }

  function recipeIngredientForSave(values) {
    const name = String(values?.name ?? "").trim();
    const rawQuantity = String(values?.quantity ?? "").trim();
    const quantity = rawQuantity ? normalizeQuantity(rawQuantity) : null;
    const unit = String(values?.unit ?? "").trim() || null;
    const item = {
      name,
      quantity,
      unit,
      category: String(values?.category ?? "Other").trim() || "Other",
      source_indices: Array.isArray(values?.source_indices)
        ? [...values.source_indices]
        : [],
      ...(values?.purchase_estimate && typeof values.purchase_estimate === "object"
        ? { purchase_estimate: { ...values.purchase_estimate } }
        : {}),
    };
    const errors = {
      name: !name,
      quantity: Boolean(rawQuantity && !quantity),
      unit: Boolean(quantity && !unit),
    };
    return { item, errors };
  }

  function buildRevisionRequest(values, mode, promptValue) {
    const prompt = String(promptValue ?? "").trim();
    if (!prompt) throw new Error("Beskriv hva du vil at KI skal endre.");
    const title = String(values.title ?? "").trim();
    if (!title) throw new Error("Legg til et oppskriftsnavn før du ber om en endring.");
    const servings = Number(values.servings);
    if (!Number.isInteger(servings) || servings < 1)
      throw new Error("Oppgi et helt antall porsjoner før du ber om en endring.");
    const instructions = Array.isArray(values.instructions)
      ? values.instructions.map((line) => String(line).trim()).filter(Boolean)
      : String(values.instructions ?? "").split("\n").map((line) => line.trim()).filter(Boolean);
    if (!instructions.length)
      throw new Error("Legg til minst ett steg før du ber om en endring.");
    const ingredients = (Array.isArray(values.ingredients) ? values.ingredients : []).map((item) => {
      const name = String(item.name ?? "").trim();
      if (!name) throw new Error("Legg til navn på alle ingrediensene før du ber om en endring.");
      const rawQuantity = String(item.quantity ?? "").trim();
      const quantity = rawQuantity ? normalizeQuantity(rawQuantity) : null;
      if (rawQuantity && !quantity)
        throw new Error("Rett opp ingrediensmengdene før du ber KI om å endre oppskriften.");
      return {
        name,
        quantity,
        unit: String(item.unit ?? "").trim() || null,
        category: String(item.category ?? "Other").trim() || "Other",
        source_indices: Array.isArray(item.source_indices) ? [...item.source_indices] : [],
        ...(item.purchase_estimate && typeof item.purchase_estimate === "object"
          ? { purchase_estimate: { ...item.purchase_estimate } }
          : {}),
      };
    });
    if (!ingredients.length) throw new Error("Legg til minst én ingrediens før du ber om en endring.");
    const imageUrl = String(values.image_url ?? "").trim();
    if (imageUrl) {
      let parsed;
      try {
        parsed = new URL(imageUrl);
      } catch {}
      if (!parsed || parsed.protocol !== "https:" || parsed.username || parsed.password)
        throw new Error("Lenken til oppskriftsbildet må være en offentlig HTTPS-lenke før du ber om en endring.");
    }
    const current = {
      title,
      servings,
      ingredients,
      instructions,
      source_url: values.source_url || null,
      image_url: imageUrl || null,
      source_ingredients: Array.isArray(values.source_ingredients)
        ? values.source_ingredients.map((item) => ({ ...item }))
        : [],
      provenance: values.provenance || null,
    };
    const isAdaptedDraft = !mode.isEditing && mode.source === "ai";
    if (isAdaptedDraft)
      return { draft: { ...current, source: "ai" }, prompt };
    const completeIngredients = ingredients.map((item) => {
      if (!item.quantity || !item.unit)
        throw new Error("Fyll ut mengde og enhet for alle ingrediensene før du ber om en endring i en lagret oppskrift.");
      return item;
    });
    return {
      recipe: { ...current, ingredients: completeIngredients, source: mode.source },
      prompt,
    };
  }

  root.KitchenRecipeEditor = {
    buildRevisionRequest,
    isPositiveQuantity,
    normalizeQuantity,
    parseSourceIngredient,
    rowFromDraft,
    recipeIngredientForSave,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeEditor;
  }
})(globalThis);
