const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const KitchenRecipeEditor = require("../src/home_kitchen/static/recipe-editor.js");
const {
  RECIPE_TYPES,
  PREP_CATEGORIES,
  RECIPE_TYPE_LABELS,
  PREP_CATEGORY_LABELS,
  RECIPE_SOURCE_LABELS,
  formatRecipeBadge,
  formatRecipeSourceBadge,
  isPositiveQuantity,
  buildRevisionRequest,
  normalizeQuantity,
  parseSourceIngredient,
  rowFromDraft,
  recipeIngredientForSave,
  prepareAIRevisionForEditor,
  applyRevisionIfActive,
  recipeRevisionSnapshot,
} = KitchenRecipeEditor;

test("saves unknown source quantities without inventing an amount", () => {
  assert.deepEqual(recipeIngredientForSave({
    name: "1 klype salt",
    quantity: "",
    unit: "",
    source_indices: [0],
  }), {
    item: {
      name: "1 klype salt",
      quantity: null,
      unit: null,
      category: "Other",
      source_indices: [0],
    },
    errors: { name: false, quantity: false, unit: false },
  });
});

test("requires a unit only when an explicit quantity is supplied", () => {
  assert.deepEqual(recipeIngredientForSave({ name: "flour", quantity: "2", unit: "" }).errors, {
    name: false,
    quantity: false,
    unit: true,
  });
  assert.deepEqual(recipeIngredientForSave({ name: "flour", quantity: "two", unit: "cup" }).errors, {
    name: false,
    quantity: true,
    unit: false,
  });
});

test("preserves separate purchase estimates through editable recipe saves", () => {
  const purchaseEstimate = {
    quantity: "55",
    unit: "g",
    approximate: true,
    provenance: "Matprat ingredient-specific conversion",
    source_url: "https://example.test/measurements",
  };
  const row = rowFromDraft({
    name: "hvetemel",
    quantity: "1",
    unit: "dl",
    purchase_estimate: purchaseEstimate,
  });
  const saved = recipeIngredientForSave({
    name: row.name,
    quantity: row.quantity,
    unit: row.unit,
    purchase_estimate: row.purchase_estimate,
  });
  assert.deepEqual(saved.item.purchase_estimate, purchaseEstimate);
  assert.equal(saved.item.quantity, "1");
  assert.equal(saved.item.unit, "dl");
});

test("round-trips canonical identity and original unit wording through quantity edits", () => {
  const row = rowFromDraft({
    name: "Old unmatched phrase",
    quantity: "1.25",
    unit: "handfuls",
    canonical_product_id: "product-id",
    canonical_unit_id: "unit-id",
    source_unit_expression: "Handfuls",
  });
  const saved = recipeIngredientForSave({
    ...row,
    quantity: "2.5",
    original_name: row.name,
    original_unit: row.unit,
  });
  assert.deepEqual(saved.item, {
    name: "Old unmatched phrase",
    quantity: "2.5",
    unit: "handfuls",
    category: "Other",
    source_indices: [],
    canonical_product_id: "product-id",
    canonical_unit_id: "unit-id",
    source_unit_expression: "Handfuls",
  });
});

test("semantic edits clear only their related identity and preserve explicit nulls", () => {
  const original = rowFromDraft({
    name: "Salt",
    quantity: "1",
    unit: "tsp",
    canonical_product_id: "product-id",
    canonical_unit_id: "unit-id",
    source_unit_expression: "Teaspoons",
  });
  const renamed = recipeIngredientForSave({
    ...original,
    name: "Pepper",
    original_name: original.name,
    original_unit: original.unit,
  }).item;
  assert.equal(renamed.canonical_product_id, null);
  assert.equal(renamed.canonical_unit_id, "unit-id");
  assert.equal(renamed.source_unit_expression, "Teaspoons");

  const remeasured = recipeIngredientForSave({
    ...original,
    unit: "ml",
    original_name: original.name,
    original_unit: original.unit,
  }).item;
  assert.equal(remeasured.canonical_product_id, "product-id");
  assert.equal(remeasured.canonical_unit_id, null);
  assert.equal(remeasured.source_unit_expression, "ml");

  const explicitlyCleared = rowFromDraft({
    name: "Salt",
    quantity: "1",
    unit: "tsp",
    canonical_product_id: null,
    canonical_unit_id: null,
    source_unit_expression: null,
  });
  const savedNulls = recipeIngredientForSave({
    ...explicitlyCleared,
    original_name: explicitlyCleared.name,
    original_unit: explicitlyCleared.unit,
  }).item;
  assert.equal(Object.hasOwn(savedNulls, "canonical_product_id"), true);
  assert.equal(Object.hasOwn(savedNulls, "canonical_unit_id"), true);
  assert.equal(Object.hasOwn(savedNulls, "source_unit_expression"), true);
  assert.equal(savedNulls.canonical_product_id, null);
  assert.equal(savedNulls.canonical_unit_id, null);
  assert.equal(savedNulls.source_unit_expression, null);
});

test("AI redrafts never inherit canonical IDs from source references and preserve only source wording", () => {
  const originals = [
    {
      name: "flour", quantity: "2", unit: "cup", source_indices: [0],
      canonical_product_id: "flour-id", canonical_unit_id: "cup-id", source_unit_expression: "cups",
    },
    {
      name: "salt", quantity: "1", unit: "tsp", source_indices: [1],
      canonical_product_id: "salt-id", canonical_unit_id: "tsp-id", source_unit_expression: "teaspoon",
    },
  ];
  const proposed = [
    { name: "Fine salt", quantity: "2", unit: "tsp", source_indices: [1] },
    { name: "flour", quantity: "3", unit: "ml", source_indices: [0] },
    { name: "ambiguous", quantity: "1", unit: "g", source_indices: [] },
  ];
  const result = prepareAIRevisionForEditor(originals, proposed);
  assert.equal(result.identityReviewRequired, true);
  assert.deepEqual(result.ingredients[0], {
    name: "Fine salt", quantity: "2", unit: "tsp", source_indices: [1],
    canonical_product_id: null, canonical_unit_id: null, source_unit_expression: "teaspoon",
  });
  assert.deepEqual(result.ingredients[1], {
    name: "flour", quantity: "3", unit: "ml", source_indices: [0],
    canonical_product_id: null, canonical_unit_id: null, source_unit_expression: null,
  });
  assert.deepEqual(result.ingredients[2], {
    ...proposed[2],
    canonical_product_id: null,
    canonical_unit_id: null,
  });
});

test("AI revision keeps source wording for an unmapped unchanged measure without treating it as identity", () => {
  const result = prepareAIRevisionForEditor(
    [{ name: "salt", quantity: "1", unit: "tsp", source_indices: [0], source_unit_expression: "Teaspoons" }],
    [{ name: "salt", quantity: "2", unit: "tsp", source_indices: [0] }],
  );
  assert.equal(result.identityReviewRequired, false);
  assert.deepEqual(result.ingredients[0], {
    name: "salt",
    quantity: "2",
    unit: "tsp",
    source_indices: [0],
    canonical_product_id: null,
    canonical_unit_id: null,
    source_unit_expression: "Teaspoons",
  });
});

test("late AI response cannot replace a different recipe editor opened after the request", async () => {
  let resolveResponse;
  const response = new Promise((resolve) => { resolveResponse = resolve; });
  const modal = { open: true, editor: null };
  const editorA = { reviseButton: { isConnected: true }, draft: { title: "Recipe A" } };
  modal.editor = editorA;
  const applying = applyRevisionIfActive(
    response,
    () => modal.open && editorA.reviseButton.isConnected,
    (result) => { modal.editor.draft = result.adapted; },
  );

  editorA.reviseButton.isConnected = false;
  modal.open = false;
  const editorB = { reviseButton: { isConnected: true }, draft: { title: "Recipe B" } };
  modal.editor = editorB;
  modal.open = true;
  resolveResponse({ adapted: { title: "Late Recipe A" } });

  assert.equal(await applying, false);
  assert.equal(modal.editor, editorB);
  assert.deepEqual(editorB.draft, { title: "Recipe B" });
});

test("AI response still applies to the active recipe editor", async () => {
  const modal = { open: true, editor: { draft: { title: "Recipe A", servings: "2", ingredients: [], instructions: "Cook" } } };
  const submittedSnapshot = recipeRevisionSnapshot(modal.editor.draft);
  const applied = await applyRevisionIfActive(
    Promise.resolve({ adapted: { title: "Revised Recipe A" } }),
    () => modal.open && submittedSnapshot === recipeRevisionSnapshot(modal.editor.draft),
    (result) => { modal.editor.draft = result.adapted; },
  );
  assert.equal(applied, true);
  assert.deepEqual(modal.editor.draft, { title: "Revised Recipe A" });
});

test("AI response is ignored when manual recipe edits change the submitted snapshot", async () => {
  let resolveResponse;
  const response = new Promise((resolve) => { resolveResponse = resolve; });
  const modal = {
    open: true,
    editor: {
      values: {
        title: "Recipe A",
        servings: "2",
        ingredients: [{ name: "Salt", quantity: "1", unit: "tsp" }],
        instructions: "Cook",
      },
    },
  };
  const submittedSnapshot = recipeRevisionSnapshot(modal.editor.values);
  const applying = applyRevisionIfActive(
    response,
    () => modal.open && submittedSnapshot === recipeRevisionSnapshot(modal.editor.values),
    (result) => { modal.editor.values = result.adapted; },
  );

  modal.editor.values = {
    ...modal.editor.values,
    title: "My manual edit while waiting",
    ingredients: [{ name: "Salt", quantity: "3", unit: "tsp" }],
  };
  resolveResponse({ adapted: { title: "AI response based on old values" } });

  assert.equal(await applying, false);
  assert.equal(modal.editor.values.title, "My manual edit while waiting");
  assert.equal(modal.editor.values.ingredients[0].quantity, "3");
});

test("parses a leading exact decimal and recognized explicit unit", () => {
  assert.deepEqual(parseSourceIngredient(" 0.25 kg carrots "), {
    sourceText: " 0.25 kg carrots ",
    name: "carrots",
    quantity: "0.25",
    unit: "kg",
    category: "Other",
    parsed: true,
  });
  assert.equal(parseSourceIngredient("2 ss olivenolje").unit, "ss");
  assert.equal(parseSourceIngredient("0,5 kg gulrøtter").quantity, "0.5");
});

test("AI revision keeps an adapted draft editable when quantities need review", () => {
  const payload = buildRevisionRequest({
    title: "Soup",
    servings: "4",
    ingredients: [{ name: "carrots", quantity: "", unit: "", source_indices: [0] }],
    instructions: "Chop\nSimmer",
    source_url: "https://example.test/soup",
    image_url: "",
    source_ingredients: [{ index: 0, text: "carrots to taste" }],
    provenance: { kind: "ai_adaptation", original_source: "imported", source_url: "https://example.test/soup" },
  }, { isEditing: false, source: "ai" }, " make it vegan ");

  assert.deepEqual(payload, {
    draft: {
      title: "Soup",
      servings: 4,
      ingredients: [{ name: "carrots", quantity: null, unit: null, category: "Other", source_indices: [0] }],
      instructions: ["Chop", "Simmer"],
      source_url: "https://example.test/soup",
      image_url: null,
      source_ingredients: [{ index: 0, text: "carrots to taste" }],
      provenance: { kind: "ai_adaptation", original_source: "imported", source_url: "https://example.test/soup" },
      source: "ai",
    },
    prompt: "make it vegan",
  });
});

test("AI revision preserves the private cookbook photo and selected image credit", () => {
  const photoSource = { id: "photo-id", book_title: "God mat", page: "17" };
  const imageAttribution = {
    provider: "wikimedia_commons",
    provider_id: "image-id",
    query: "suppe",
    title: "Soup",
    image_url: "https://images.example/soup.jpg",
    source_url: "https://commons.wikimedia.org/wiki/File:Soup.jpg",
    creator: "Example",
    license: "CC BY-SA 4.0",
    license_url: "https://creativecommons.org/licenses/by-sa/4.0/",
  };
  const payload = buildRevisionRequest({
    title: "Suppe",
    servings: 2,
    ingredients: [{ name: "gulrot" }],
    instructions: ["Kok"],
    photo_source: photoSource,
    image_attribution: imageAttribution,
  }, { isEditing: false, source: "ai" }, "gjør den litt mildere");
  assert.deepEqual(payload.draft.photo_source, photoSource);
  assert.deepEqual(payload.draft.image_attribution, imageAttribution);
});

test("AI revision sends complete saved recipes including unquantified ingredients, and requires unit when quantity is set", () => {
  const values = {
    title: "Bread", servings: 2,
    ingredients: [{ name: "flour", quantity: "1,25", unit: "kg", source_indices: [1] }],
    instructions: ["Mix"], source_ingredients: [{ index: 1, text: "1.25 kg flour" }],
    provenance: { kind: "ai_adaptation", source_url: null },
  };
  assert.deepEqual(buildRevisionRequest(values, { isEditing: true, source: "imported" }, " halve it "), {
    recipe: {
      title: "Bread", servings: 2,
      ingredients: [{ name: "flour", quantity: "1.25", unit: "kg", category: "Other", source_indices: [1] }],
      instructions: ["Mix"], source_url: null, image_url: null,
      source_ingredients: [{ index: 1, text: "1.25 kg flour" }],
      provenance: { kind: "ai_adaptation", source_url: null }, source: "imported",
    },
    prompt: "halve it",
  });
  const unquantifiedPayload = buildRevisionRequest(
    {
      ...values,
      ingredients: [
        { name: "flour", quantity: "1,25", unit: "kg", source_indices: [1] },
        { name: "salt", quantity: "", unit: "", source_unit_expression: "klype" },
        { name: "persille", quantity: null, unit: "håndfull", source_unit_expression: "håndfull" },
      ],
    },
    { isEditing: true, source: "manual" },
    "juster krydder",
  );
  assert.deepEqual(unquantifiedPayload.recipe.ingredients, [
    { name: "flour", quantity: "1.25", unit: "kg", category: "Other", source_indices: [1] },
    { name: "salt", quantity: null, unit: null, category: "Other", source_indices: [], source_unit_expression: "klype" },
    { name: "persille", quantity: null, unit: "håndfull", category: "Other", source_indices: [], source_unit_expression: "håndfull" },
  ]);
  assert.throws(
    () => buildRevisionRequest({ ...values, ingredients: [{ name: "flour", quantity: "1", unit: "" }] }, { isEditing: true, source: "manual" }, "revise"),
    /Oppgi enhet for alle ingredienser med mengde/,
  );
});

test("AI revision requests include explicit canonical identity fields for mapped ingredients", () => {
  const payload = buildRevisionRequest({
    title: "Soup",
    servings: 1,
    ingredients: [{
      name: "Old unmatched phrase",
      quantity: "1.25",
      unit: "Handfuls",
      original_name: "Old unmatched phrase",
      original_unit: "Handfuls",
      category: "Other",
      source_indices: [0],
      canonical_product_id: "product-id",
      canonical_unit_id: "unit-id",
      source_unit_expression: "Handfuls",
    }],
    instructions: ["Cook"],
  }, { isEditing: true, source: "manual" }, "adjust quantity");
  assert.deepEqual(payload.recipe.ingredients[0], {
    name: "Old unmatched phrase",
    quantity: "1.25",
    unit: "Handfuls",
    category: "Other",
    source_indices: [0],
    canonical_product_id: "product-id",
    canonical_unit_id: "unit-id",
    source_unit_expression: "Handfuls",
  });
});

test("an unmapped ingredient with source wording alone can still change quantity", () => {
  const payload = buildRevisionRequest({
    title: "Soup",
    servings: 1,
    ingredients: [{
      name: "salt",
      quantity: "2",
      unit: "ml",
      original_name: "salt",
      original_unit: "tsp",
      source_unit_expression: "Teaspoons",
    }],
    instructions: ["Cook"],
  }, { isEditing: true, source: "manual" }, "change amount");
  assert.deepEqual(payload.recipe.ingredients[0], {
    name: "salt",
    quantity: "2",
    unit: "ml",
    category: "Other",
    source_indices: [],
    source_unit_expression: "ml",
  });
});

test("leaves ambiguous or unrecognized source text unresolved", () => {
  for (const source of ["1-2 cups flour", "½ cup sugar", "1/2 cup sugar", "1,000.5 kg potatoes", "2 mystery carrots", "salt to taste", "etter smak"])
  {
    const row = parseSourceIngredient(source);
    assert.equal(row.sourceText, source);
    assert.equal(row.name, source.trim());
    assert.equal(row.quantity, "");
    assert.equal(row.unit, "");
    assert.equal(row.parsed, false);
  }
});

test("keeps Norwegian practical measures and their source text without requiring cleanup", () => {
  for (const source of ["1 klype salt", "2 håndfull persille"]) {
    const row = parseSourceIngredient(source);
    assert.equal(row.sourceText, source);
    const saved = recipeIngredientForSave({
      name: row.name,
      quantity: row.quantity,
      unit: row.unit,
      source_indices: [0],
    });
    assert.deepEqual(saved.errors, { name: false, quantity: false, unit: false });
    assert.equal(saved.item.quantity, source.startsWith("1") ? "1" : "2");
    assert.equal(saved.item.unit, source.startsWith("1") ? "klype" : "håndfull");
    assert.deepEqual(saved.item.source_indices, [0]);
  }
});

test("accepts decimal comma or point and normalizes without numeric rounding", () => {
  for (const quantity of ["1", "01.00", ".5", "0.0001", "0,5", "1,25"]) assert.equal(isPositiveQuantity(quantity), true);
  assert.equal(normalizeQuantity("0,5"), "0.5");
  assert.equal(normalizeQuantity("1.25"), "1.25");
  for (const quantity of ["", "0", "0,000", "-1", "+1", "1e2", "1-2", "1/2", "1,000.5", "1.2,5", "1,2,3", "1.", "."]) {
    assert.equal(isPositiveQuantity(quantity), false, quantity);
  }
});

test("maps structured drafts without presenting a synthesized summary as source text", () => {
  assert.deepEqual(rowFromDraft({ name: "flour", quantity: 2, unit: "cups", category: "Pantry" }), {
    sourceText: "",
    source_indices: [],
    name: "flour",
    quantity: "2",
    unit: "cups",
    category: "Pantry",
    parsed: null,
    hasDraftValues: true,
  });
  assert.deepEqual(rowFromDraft({ name: "flour", quantity: "1", unit: "kg", source_indices: [2] }).source_indices, [2]);
});

test("AI revision preserves recipe metadata fields when supplied", () => {
  const payload = buildRevisionRequest({
    title: "Suppe",
    servings: 2,
    ingredients: [{ name: "gulrot", quantity: "2", unit: "stk" }],
    instructions: ["Kok"],
    recipe_type: "middag",
    prep_time_minutes: 25,
    prep_category: "rask",
  }, { isEditing: true, source: "manual" }, "gjør den raskere");
  assert.equal(payload.recipe.recipe_type, "middag");
  assert.equal(payload.recipe.prep_time_minutes, 25);
  assert.equal(payload.recipe.prep_category, "rask");
});

test("formatRecipeBadge formats recipe type, prep category, and prep time minutes", () => {
  assert.deepEqual(RECIPE_TYPES, ["middag", "dessert", "frokost", "lunsj", "tilbehør", "bakst", "annet"]);
  assert.deepEqual(PREP_CATEGORIES, ["rask", "normal", "langtid", "batch_cooking"]);
  assert.equal(RECIPE_TYPE_LABELS.tilbehør, "Tilbehør");
  assert.equal(PREP_CATEGORY_LABELS.batch_cooking, "Batch-cooking");

  assert.equal(
    formatRecipeBadge({ recipe_type: "middag", prep_category: "rask", prep_time_minutes: 25 }),
    "Middag · Rask (25 min)",
  );
  assert.equal(
    formatRecipeBadge({ recipe_type: "bakst", prep_category: "langtid", prep_time_minutes: null }),
    "Bakst · Langtid",
  );
  assert.equal(
    formatRecipeBadge({ recipe_type: null, prep_category: null, prep_time_minutes: 45 }),
    "45 min",
  );
  assert.equal(formatRecipeBadge({ recipe_type: "unknown", prep_time_minutes: 0 }), "");
  assert.equal(formatRecipeBadge(null), "");
});

test("recipeRevisionSnapshot detects metadata changes and buildRevisionRequest preserves metadata when provided", () => {
  const base = {
    title: "Suppe",
    servings: "2",
    prompt: "",
    recipe_type: "middag",
    prep_time_minutes: "20",
    prep_category: "rask",
    ingredients: [{ name: "gulrot", quantity: "2", unit: "stk" }],
    instructions: "Kok",
  };
  assert.notEqual(
    recipeRevisionSnapshot(base),
    recipeRevisionSnapshot({ ...base, recipe_type: "lunsj" }),
  );
  assert.notEqual(
    recipeRevisionSnapshot(base),
    recipeRevisionSnapshot({ ...base, prep_time_minutes: "35" }),
  );
  assert.notEqual(
    recipeRevisionSnapshot(base),
    recipeRevisionSnapshot({ ...base, prep_category: "normal" }),
  );

  const req = buildRevisionRequest(base, { isEditing: true, source: "manual" }, "gjør den raskere");
  assert.equal(req.recipe.recipe_type, "middag");
  assert.equal(req.recipe.prep_time_minutes, 20);
  assert.equal(req.recipe.prep_category, "rask");
});

class DomNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.textContent = text == null ? "" : String(text);
    this.className = className || "";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.type = "";
    this.name = "";
    this.hidden = false;
    this.disabled = false;
    this.parentNode = null;
    this.classList = {
      contains: (cls) => this.className.split(/\s+/).filter(Boolean).includes(cls),
      toggle: (cls, force) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        const shouldAdd = force !== undefined ? Boolean(force) : !set.has(cls);
        if (shouldAdd) set.add(cls);
        else set.delete(cls);
        this.className = [...set].join(" ");
      },
    };
  }
  append(...children) {
    children.forEach((child) => {
      if (!child) return;
      child.parentNode = this;
      this.children.push(child);
    });
  }
  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.append(...children);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this, currentTarget: this })); }
  dispatchEvent(type) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, currentTarget: this })); }
  focus() { this.focused = true; }
  get isConnected() {
    if (this.parentNode) return this.parentNode.isConnected;
    return this.connected === true;
  }
  findAll(predicate, acc = []) {
    if (predicate(this)) acc.push(this);
    this.children.forEach((child) => child.findAll?.(predicate, acc));
    return acc;
  }
  querySelectorAll(selector) {
    const classNames = selector.startsWith(".") ? selector.slice(1).split(".") : null;
    return this.findAll((n) => {
      if (classNames) return classNames.every((cls) => n.classList.contains(cls));
      return n.tagName.toLowerCase() === selector.toLowerCase();
    });
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function buildRecipeUiHarness({ recipes = [] } = {}) {
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
  const extractRange = (startMarker, endMarker) => {
    const start = appSource.indexOf(startMarker);
    const end = appSource.indexOf(endMarker, start);
    assert.ok(start >= 0 && end > start, `Located ${startMarker}`);
    return appSource.slice(start, end);
  };
  const renderRecipesSource = extractRange("function renderRecipes(", "\nfunction recipeForm(");
  const recipeFormSource = extractRange("function recipeForm(", "\nfunction recipeImageCredit(");
  const viewStart = appSource.indexOf("function viewRecipe(");
  const viewEnd = appSource.indexOf("\nfunction removeRecipeImage(", viewStart);
  assert.ok(viewStart >= 0 && viewEnd > viewStart, "Located viewRecipe");
  const viewRecipeSlice = appSource.slice(viewStart, viewEnd);
  const badgeStart = appSource.indexOf("function formatRecipeBadge(");
  const badgeEnd = appSource.indexOf("\nfunction day(", badgeStart);
  const badgeSource = appSource.slice(badgeStart, badgeEnd);
  const importFormSource = extractRange("function importForm(", "\nfunction discoveryForm(");

  const modal = { open: false, dataset: {}, showModal() { this.open = true; }, close() { this.open = false; } };
  const main = new DomNode("main");
  main.connected = true;
  const dialogRoot = new DomNode("div");
  dialogRoot.connected = true;
  const apiCalls = [];
  const state = {
    view: "recipes",
    household: { default_servings: 2, language: "nb" },
    recipes,
    recipeCollectionJob: null,
    recipeCollectionStatusNode: null,
  };
  const fields = [];
  let submitAction = null;
  const context = {
    state,
    window: { KitchenRecipeEditor },
    URL,
    $: (sel) => (sel === "#modal" ? modal : sel === "#main" ? main : dialogRoot),
    openDialog: (headingText) => {
      modal.showModal();
      dialogRoot.replaceChildren(new DomNode("h2", headingText));
      return dialogRoot;
    },
    el: (tag, text = "", className = "") => new DomNode(tag, text, className),
    add: (parent, ...children) => {
      children.filter(Boolean).forEach((child) => parent.append(child));
      return parent;
    },
    button: (text, action, className = "") => {
      const node = new DomNode("button", text, className);
      node.type = "button";
      node.addEventListener("click", action);
      return node;
    },
    field: (label, type = "text", value = "", help = "") => {
      const wrap = new DomNode("label", label, "field");
      const input = new DomNode(type === "textarea" ? "textarea" : type === "select" ? "select" : "input");
      input.value = value == null ? "" : String(value);
      input.fieldLabel = label;
      wrap.append(input);
      if (help) wrap.append(new DomNode("small", help));
      fields.push(input);
      return { wrap, input };
    },
    formSubmit: (form, label, action) => {
      const error = new DomNode("p", "", "form-error");
      form.append(error);
      submitAction = async () => {
        try {
          await action();
        } catch (err) {
          error.textContent = err.message;
          throw err;
        }
      };
      return error;
    },
    title: (heading, eyebrow, subtitle, actions = []) => {
      const header = new DomNode("header", "", "page-title");
      header.append(new DomNode("h1", heading), ...actions);
      return header;
    },
    empty: (heading, desc, action) => {
      const box = new DomNode("div", "", "empty");
      box.append(new DomNode("h2", heading), new DomNode("p", desc), action);
      return box;
    },
    recipeAssistantConfigured: () => false,
    connectionSettingsButton: () => new DomNode("button", "Innstillinger"),
    updateRecipeCollectionStatus: () => {},
    unmappedIngredientsDialog: () => {},
    addRecipeForm: () => {},
    mealForm: () => {},
    recipeImage: () => null,
    recipeImageCredit: () => null,
    purchaseEstimateText: () => "",
    ingredientQuantityText: (item) => item.name,
    formatUiCount: String,
    formatServings: (v) => `${v} porsjoner`,
    httpsUrl: () => true,
    notify: () => {},
    navigate: () => {},
    render: () => {},
    api: async (reqPath, method = "GET", body) => {
      apiCalls.push({ path: reqPath, method, body });
      if (reqPath === "/recipes" && method === "GET") return state.recipes;
      if (reqPath === "/recipe-drafts/ai" && method === "POST" && body?.url) {
        return {
          adapted: {
            title: "Tomatsuppe",
            servings: 4,
            recipe_type: "lunsj",
            prep_time_minutes: 25,
            prep_category: "rask",
            ingredients: [{ name: "tomat", quantity: "400", unit: "g", source_indices: [0] }],
            instructions: ["Kok opp"],
            source_url: body.url,
            source_ingredients: [{ index: 0, text: "400 g tomat" }],
          },
        };
      }
      return { id: "saved-1", ...(body || {}) };
    },
  };
  vm.runInNewContext(
    [badgeSource, renderRecipesSource, recipeFormSource, viewRecipeSlice, importFormSource].join("\n"),
    context,
  );
  return {
    main,
    dialogRoot,
    state,
    context,
    fields,
    apiCalls,
    submitForm: () => submitAction(),
  };
}

test("DOM: recipeForm renders recipe metadata fields, pre-populates edits, and submits metadata in payload", async () => {
  const harness = buildRecipeUiHarness();
  harness.context.recipeForm({
    id: "rec-1",
    title: "Kjøttkaker",
    servings: 4,
    recipe_type: "middag",
    prep_time_minutes: 45,
    prep_category: "normal",
    ingredients: [{ name: "kjøttdeig", quantity: "400", unit: "g" }],
    instructions: ["Stek"],
  }, "manual");

  const metaRow = harness.dialogRoot.querySelector(".recipe-metadata-row");
  assert.ok(metaRow, "recipe-metadata-row should be rendered");
  const typeSelect = metaRow.findAll((n) => n.name === "recipe_type")[0];
  const minutesInput = metaRow.findAll((n) => n.name === "prep_time_minutes")[0];
  const categorySelect = metaRow.findAll((n) => n.name === "prep_category")[0];
  assert.equal(typeSelect.value, "middag");
  assert.equal(minutesInput.value, "45");
  assert.equal(categorySelect.value, "normal");

  // Update metadata values and submit
  typeSelect.value = "lunsj";
  minutesInput.value = "20";
  categorySelect.value = "rask";
  await harness.submitForm();

  const putCall = harness.apiCalls.find((c) => c.path === "/recipes/rec-1" && c.method === "PUT");
  assert.ok(putCall);
  assert.equal(putCall.body.recipe_type, "lunsj");
  assert.equal(putCall.body.prep_time_minutes, 20);
  assert.equal(putCall.body.prep_category, "rask");

  // Invalid negative prep_time_minutes throws validation error
  minutesInput.value = "-5";
  await assert.rejects(() => harness.submitForm(), /Tilberedningstid må være et positivt heltall/);
});

test("DOM: renderRecipes and viewRecipe display recipe metadata badges and filter chips filter cards", () => {
  const recipes = [
    {
      id: "r1",
      title: "Rask suppe",
      servings: 2,
      source: "manual",
      recipe_type: "middag",
      prep_time_minutes: 15,
      prep_category: "rask",
      ingredients: [{ name: "tomat", quantity: "2", unit: "stk" }],
      instructions: ["Kok"],
    },
    {
      id: "r2",
      title: "Langtidssurret gryte",
      servings: 4,
      source: "manual",
      recipe_type: "middag",
      prep_time_minutes: 120,
      prep_category: "langtid",
      ingredients: [{ name: "bønner", quantity: "200", unit: "g" }],
      instructions: ["Kok lenge"],
    },
    {
      id: "r3",
      title: "Eplekake",
      servings: 6,
      source: "manual",
      recipe_type: "dessert",
      prep_time_minutes: 50,
      prep_category: "normal",
      ingredients: [{ name: "eple", quantity: "3", unit: "stk" }],
      instructions: ["Stek"],
    },
    {
      id: "r4",
      title: "Enkel salat",
      servings: 1,
      source: "manual",
      ingredients: [{ name: "salat", quantity: "1", unit: "stk" }],
      instructions: ["Bland"],
    },
  ];
  const harness = buildRecipeUiHarness({ recipes });
  harness.context.renderRecipes(harness.main);

  const cards = harness.main.querySelectorAll(".recipe-card");
  assert.equal(cards.length, 4);
  assert.equal(cards[0].querySelector(".recipe-badge-meta").textContent, "Middag · Rask (15 min)");
  assert.equal(cards[1].querySelector(".recipe-badge-meta").textContent, "Middag · Langtid (120 min)");
  assert.equal(cards[2].querySelector(".recipe-badge-meta").textContent, "Dessert · Normal (50 min)");
  assert.equal(cards[3].querySelector(".recipe-badge-meta"), null);

  // Filter by recipe type "middag" -> 2 cards
  const middagChip = harness.main.findAll((n) => n.dataset?.typeFilter === "middag")[0];
  middagChip.click();
  assert.equal(middagChip.classList.contains("active"), true);
  assert.deepEqual(
    harness.main.querySelectorAll(".recipe-card").map((c) => c.querySelector("h3").textContent),
    ["Rask suppe", "Langtidssurret gryte"],
  );

  // Also filter by prep category "rask" -> 1 card
  const raskChip = harness.main.findAll((n) => n.dataset?.prepFilter === "rask")[0];
  raskChip.click();
  assert.deepEqual(
    harness.main.querySelectorAll(".recipe-card").map((c) => c.querySelector("h3").textContent),
    ["Rask suppe"],
  );

  // viewRecipe shows badge when metadata present and omits when absent
  harness.context.viewRecipe(recipes[0]);
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-meta").textContent, "Middag · Rask (15 min)");
  harness.context.viewRecipe(recipes[3]);
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-meta"), null);
});

test("DOM: importForm pre-populates recipe_type, prep_time_minutes, and prep_category in recipeForm", async () => {
  const harness = buildRecipeUiHarness();
  harness.context.importForm();
  const urlInput = harness.fields.find((f) => f.fieldLabel === "Oppskriftslenke");
  assert.ok(urlInput, "URL input rendered");
  urlInput.value = "https://www.matprat.no/oppskrifter/tomatsuppe/";
  await harness.submitForm();

  const metaRow = harness.dialogRoot.querySelector(".recipe-metadata-row");
  assert.ok(metaRow, "recipeForm opened with recipe-metadata-row");
  assert.equal(metaRow.findAll((n) => n.name === "recipe_type")[0].value, "lunsj");
  assert.equal(metaRow.findAll((n) => n.name === "prep_time_minutes")[0].value, "25");
  assert.equal(metaRow.findAll((n) => n.name === "prep_category")[0].value, "rask");
});

test("formatRecipeSourceBadge formats recipe source labels cleanly", () => {
  assert.equal(RECIPE_SOURCE_LABELS.cookidoo, "Cookidoo");
  assert.equal(RECIPE_SOURCE_LABELS.oda, "Oda");
  assert.equal(RECIPE_SOURCE_LABELS.ai, "KI-idé");
  assert.equal(RECIPE_SOURCE_LABELS.manual, "Fra ditt kjøkken");

  assert.equal(formatRecipeSourceBadge({ source: "cookidoo" }), "Cookidoo");
  assert.equal(formatRecipeSourceBadge({ source: "ai" }), "KI-idé");
  assert.equal(
    formatRecipeSourceBadge({ source: "imported", provenance: { provider: "oda" } }),
    "Oda",
  );
  assert.equal(
    formatRecipeSourceBadge({
      source: "imported",
      source_url: "https://oda.com/no/recipes/731-tomato-soup/",
    }),
    "Oda",
  );
  assert.equal(
    formatRecipeSourceBadge({ source: "imported", provenance: { kind: "photo" } }),
    "Fra bilde",
  );
  assert.equal(
    formatRecipeSourceBadge({ source: "imported", photo_source: { kind: "photo_book" } }),
    "Fra bilde",
  );
  assert.equal(
    formatRecipeSourceBadge({
      source: "imported",
      source_url: "https://www.matprat.no/oppskrifter/tomatsuppe/",
    }),
    "Fra en oppskriftslenke",
  );
  assert.equal(formatRecipeSourceBadge({ source: "manual" }), "Fra ditt kjøkken");
  assert.equal(formatRecipeSourceBadge({}), "Fra ditt kjøkken");
  assert.equal(formatRecipeSourceBadge(null), "");
});

test("DOM: renderRecipes and viewRecipe render source badges and source filter chips filter cards", () => {
  const recipes = [
    {
      id: "s1",
      title: "Cookidoo Risotto",
      servings: 4,
      source: "cookidoo",
      source_url: "https://cookidoo.no/recipes/recipe/nb-NO/r1",
      recipe_type: "middag",
      prep_time_minutes: 30,
      prep_category: "rask",
      ingredients: [{ name: "ris", quantity: "300", unit: "g" }],
      instructions: ["Kok"],
    },
    {
      id: "s2",
      title: "Oda Fiskesuppe",
      servings: 4,
      source: "imported",
      source_url: "https://oda.com/no/recipes/42-fiskesuppe/",
      recipe_type: "middag",
      prep_time_minutes: 25,
      prep_category: "rask",
      ingredients: [{ name: "laks", quantity: "400", unit: "g" }],
      instructions: ["Trekk"],
    },
    {
      id: "s3",
      title: "Matprat Pannekaker",
      servings: 2,
      source: "imported",
      source_url: "https://www.matprat.no/oppskrifter/pannekaker/",
      recipe_type: "frokost",
      prep_time_minutes: 20,
      prep_category: "rask",
      ingredients: [{ name: "mel", quantity: "200", unit: "g" }],
      instructions: ["Stek"],
    },
    {
      id: "s4",
      title: "KI Kyllingbolle",
      servings: 2,
      source: "ai",
      recipe_type: "lunsj",
      ingredients: [{ name: "kylling", quantity: "200", unit: "g" }],
      instructions: ["Bland"],
    },
    {
      id: "s5",
      title: "Mormors Brød",
      servings: 1,
      source: "manual",
      ingredients: [{ name: "mel", quantity: "500", unit: "g" }],
      instructions: ["Bak"],
    },
  ];

  const harness = buildRecipeUiHarness({ recipes });
  harness.context.renderRecipes(harness.main);

  const cards = harness.main.querySelectorAll(".recipe-card");
  assert.equal(cards.length, 5);
  assert.deepEqual(
    cards.map((c) => c.querySelector(".recipe-badge-source").textContent),
    ["Cookidoo", "Oda", "Fra en oppskriftslenke", "KI-idé", "Fra ditt kjøkken"],
  );

  // Source filter chips exist and filter accurately
  const sourceChipButtons = harness.main.querySelector(".source-chips").querySelectorAll(".filter-chip");
  assert.deepEqual(
    sourceChipButtons.map((btn) => btn.textContent),
    ["Alle kilder", "Cookidoo", "Oda", "Lenke", "KI-idé", "Egen"],
  );

  const clickSource = (val) => {
    const chip = sourceChipButtons.find((btn) => btn.dataset.sourceFilter === val);
    assert.ok(chip, `Source chip ${val} exists`);
    chip.click();
    return harness.main.querySelectorAll(".recipe-card").map((c) => c.querySelector("h3").textContent);
  };

  assert.deepEqual(clickSource("cookidoo"), ["Cookidoo Risotto"]);
  assert.deepEqual(clickSource("oda"), ["Oda Fiskesuppe"]);
  assert.deepEqual(clickSource("imported"), ["Matprat Pannekaker"]);
  assert.deepEqual(clickSource("ai"), ["KI Kyllingbolle"]);
  assert.deepEqual(clickSource("manual"), ["Mormors Brød"]);
  assert.deepEqual(clickSource(""), [
    "Cookidoo Risotto",
    "Oda Fiskesuppe",
    "Matprat Pannekaker",
    "KI Kyllingbolle",
    "Mormors Brød",
  ]);

  // Combined filter: type "middag" + source "oda" -> "Oda Fiskesuppe"
  harness.main.findAll((n) => n.dataset?.typeFilter === "middag")[0].click();
  assert.deepEqual(clickSource("oda"), ["Oda Fiskesuppe"]);

  // viewRecipe renders .recipe-badge-source alongside .recipe-badge-meta
  harness.context.viewRecipe(recipes[0]);
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-source").textContent, "Cookidoo");
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-meta").textContent, "Middag · Rask (30 min)");

  harness.context.viewRecipe(recipes[1]);
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-source").textContent, "Oda");
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-meta").textContent, "Middag · Rask (25 min)");

  harness.context.viewRecipe(recipes[4]);
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-source").textContent, "Fra ditt kjøkken");
  assert.equal(harness.dialogRoot.querySelector(".recipe-badge-meta"), null);
});
