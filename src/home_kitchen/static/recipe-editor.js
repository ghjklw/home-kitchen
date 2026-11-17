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
    const row = {
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
    for (const field of ["canonical_product_id", "canonical_unit_id", "source_unit_expression"])
      if (Object.prototype.hasOwnProperty.call(value, field)) row[field] = value[field];
    return row;
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
    const normalizedIdentityText = (value) => String(value ?? "").trim().toLocaleLowerCase("nb");
    const nameChanged = values?.original_name !== undefined &&
      normalizedIdentityText(name) !== normalizedIdentityText(values.original_name);
    const unitChanged = values?.original_unit !== undefined &&
      normalizedIdentityText(unit) !== normalizedIdentityText(values.original_unit);
    if (Object.prototype.hasOwnProperty.call(values || {}, "canonical_product_id"))
      item.canonical_product_id = nameChanged ? null : values.canonical_product_id;
    if (Object.prototype.hasOwnProperty.call(values || {}, "canonical_unit_id"))
      item.canonical_unit_id = unitChanged ? null : values.canonical_unit_id;
    if (Object.prototype.hasOwnProperty.call(values || {}, "source_unit_expression"))
      item.source_unit_expression = unitChanged ? unit : values.source_unit_expression;
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
      const { item: saved } = recipeIngredientForSave({
        ...item,
        name,
        quantity,
        unit: String(item.unit ?? "").trim() || null,
      });
      return saved;
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
    const rawPrepMinutes = values.prep_time_minutes;
    const prepMinutes =
      rawPrepMinutes != null && String(rawPrepMinutes).trim() !== ""
        ? Number(rawPrepMinutes)
        : null;
    if (prepMinutes !== null && (!Number.isInteger(prepMinutes) || prepMinutes < 1))
      throw new Error("Tilberedningstid må være et positivt heltall i minutter.");
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
      ...(values.recipe_type ? { recipe_type: String(values.recipe_type) } : {}),
      ...(prepMinutes !== null ? { prep_time_minutes: prepMinutes } : {}),
      ...(values.prep_category ? { prep_category: String(values.prep_category) } : {}),
      ...(values.photo_source ? { photo_source: { ...values.photo_source } } : {}),
      ...(values.image_attribution ? { image_attribution: { ...values.image_attribution } } : {}),
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

  function prepareAIRevisionForEditor(originalIngredients, proposedIngredients) {
    const keyFor = (item) => {
      const indices = Array.isArray(item?.source_indices) ? item.source_indices : [];
      if (!indices.length || indices.some((value) => !Number.isSafeInteger(value) || value < 0)) return null;
      return [...new Set(indices)].sort((left, right) => left - right).join(",");
    };
    const originals = new Map();
    const proposals = new Map();
    (Array.isArray(originalIngredients) ? originalIngredients : []).forEach((item) => {
      const key = keyFor(item);
      if (key === null) return;
      originals.set(key, originals.has(key) ? null : item);
    });
    (Array.isArray(proposedIngredients) ? proposedIngredients : []).forEach((item, index) => {
      const key = keyFor(item);
      if (key === null) return;
      const indexes = proposals.get(key) || [];
      indexes.push(index);
      proposals.set(key, indexes);
    });
    const ingredients = (Array.isArray(proposedIngredients) ? proposedIngredients : []).map((item) => {
      const key = keyFor(item);
      const original = key === null ? null : originals.get(key);
      const proposalIndexes = key === null ? null : proposals.get(key);
      const unitChanged = String(item.unit ?? "").trim().toLocaleLowerCase("nb") !==
        String(original?.unit ?? "").trim().toLocaleLowerCase("nb");
      const uniquelyLinked = Boolean(original && proposalIndexes?.length === 1);
      const revised = {
        ...item,
        canonical_product_id: null,
        canonical_unit_id: null,
      };
      if (uniquelyLinked && !unitChanged &&
          Object.prototype.hasOwnProperty.call(original, "source_unit_expression"))
        revised.source_unit_expression = original.source_unit_expression;
      else if (uniquelyLinked && Object.prototype.hasOwnProperty.call(original, "source_unit_expression"))
        revised.source_unit_expression = null;
      return revised;
    });
    return {
      ingredients,
      identityReviewRequired: (Array.isArray(originalIngredients) ? originalIngredients : [])
        .some((item) => item.canonical_product_id != null || item.canonical_unit_id != null),
    };
  }

  async function applyRevisionIfActive(responsePromise, isActive, apply) {
    const response = await responsePromise;
    if (!isActive()) return false;
    await apply(response);
    return true;
  }

  const RECIPE_TYPES = Object.freeze([
    "middag",
    "dessert",
    "frokost",
    "lunsj",
    "tilbehør",
    "bakst",
    "annet",
  ]);

  const PREP_CATEGORIES = Object.freeze([
    "rask",
    "normal",
    "langtid",
    "batch_cooking",
  ]);

  const RECIPE_TYPE_LABELS = Object.freeze({
    middag: "Middag",
    dessert: "Dessert",
    frokost: "Frokost",
    lunsj: "Lunsj",
    tilbehør: "Tilbehør",
    bakst: "Bakst",
    annet: "Annet",
  });

  const PREP_CATEGORY_LABELS = Object.freeze({
    rask: "Rask",
    normal: "Normal",
    langtid: "Langtid",
    batch_cooking: "Batch-cooking",
  });

  const RECIPE_SOURCE_LABELS = Object.freeze({
    cookidoo: "Cookidoo",
    oda: "Oda",
    photo: "Fra bilde",
    imported: "Fra en oppskriftslenke",
    ai: "KI-idé",
    manual: "Fra ditt kjøkken",
  });

  function formatRecipeSourceBadge(recipe) {
    if (!recipe || typeof recipe !== "object") return "";
    if (recipe.source === "cookidoo") return RECIPE_SOURCE_LABELS.cookidoo;
    if (recipe.source === "ai") return RECIPE_SOURCE_LABELS.ai;
    if (recipe.source === "imported") {
      let isOdaUrl = false;
      if (typeof recipe.source_url === "string" && recipe.source_url) {
        try {
          const host = new URL(recipe.source_url).hostname.toLowerCase();
          isOdaUrl = host === "oda.com" || host.endsWith(".oda.com");
        } catch {
          isOdaUrl = false;
        }
      }
      if (recipe.provenance?.provider === "oda" || isOdaUrl) {
        return RECIPE_SOURCE_LABELS.oda;
      }
      if (
        recipe.provenance?.kind === "photo" ||
        recipe.photo_source?.kind === "photo_book" ||
        Boolean(recipe.photo_source?.id)
      ) {
        return RECIPE_SOURCE_LABELS.photo;
      }
      return RECIPE_SOURCE_LABELS.imported;
    }
    return RECIPE_SOURCE_LABELS.manual;
  }

  function formatRecipeBadge(recipe) {
    if (!recipe || typeof recipe !== "object") return "";
    const typeText = RECIPE_TYPE_LABELS[recipe.recipe_type] || "";
    const prepCatText = PREP_CATEGORY_LABELS[recipe.prep_category] || "";
    const rawMinutes = recipe.prep_time_minutes;
    const minutes = rawMinutes != null && rawMinutes !== "" ? Number(rawMinutes) : null;
    const timeText = minutes != null && Number.isFinite(minutes) && minutes > 0 ? `${minutes} min` : "";

    let prepText = "";
    if (prepCatText && timeText) {
      prepText = `${prepCatText} (${timeText})`;
    } else if (prepCatText) {
      prepText = prepCatText;
    } else if (timeText) {
      prepText = timeText;
    }

    return [typeText, prepText].filter(Boolean).join(" · ");
  }

  function recipeRevisionSnapshot(values) {
    return JSON.stringify({
      title: String(values?.title ?? ""),
      servings: String(values?.servings ?? ""),
      prompt: String(values?.prompt ?? ""),
      recipe_type: String(values?.recipe_type ?? ""),
      prep_time_minutes: String(values?.prep_time_minutes ?? ""),
      prep_category: String(values?.prep_category ?? ""),
      image_url: String(values?.image_url ?? ""),
      ingredients: (Array.isArray(values?.ingredients) ? values.ingredients : []).map((item) => ({
        name: String(item?.name ?? ""),
        quantity: String(item?.quantity ?? ""),
        unit: String(item?.unit ?? ""),
        category: String(item?.category ?? ""),
        source_indices: Array.isArray(item?.source_indices) ? [...item.source_indices] : [],
        purchase_estimate: item?.purchase_estimate ?? null,
        canonical_product_id: item?.canonical_product_id,
        canonical_unit_id: item?.canonical_unit_id,
        source_unit_expression: item?.source_unit_expression,
      })),
      instructions: String(values?.instructions ?? ""),
    });
  }

  root.KitchenRecipeEditor = {
    RECIPE_TYPES,
    PREP_CATEGORIES,
    RECIPE_TYPE_LABELS,
    PREP_CATEGORY_LABELS,
    RECIPE_SOURCE_LABELS,
    formatRecipeBadge,
    formatRecipeSourceBadge,
    buildRevisionRequest,
    applyRevisionIfActive,
    isPositiveQuantity,
    normalizeQuantity,
    parseSourceIngredient,
    prepareAIRevisionForEditor,
    recipeRevisionSnapshot,
    rowFromDraft,
    recipeIngredientForSave,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeEditor;
  }
})(globalThis);
