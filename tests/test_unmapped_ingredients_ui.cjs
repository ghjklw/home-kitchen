"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const unmappedView = require("../src/home_kitchen/static/unmapped-ingredients-view.js");

class MockNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.hidden = false;
    this.open = false;
  }

  append(...children) {
    for (const child of children) {
      if (child) {
        this.children.push(child);
      }
    }
  }

  replaceChildren(...children) {
    this.textContent = "";
    this.children = children.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "type") this.type = String(value);
    if (name === "value") this.value = String(value);
  }

  getAttribute(name) {
    if (name === "type" && this.type !== undefined) return this.type;
    if (name === "value" && this.value !== undefined) return this.value;
    return this.attributes[name] ?? null;
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  async click() {
    if (!this.disabled && this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {} });
    }
  }

  async change() {
    if (this.listeners.change) {
      await this.listeners.change({ target: this });
    }
  }

  async keydown(key) {
    if (this.listeners.keydown) {
      await this.listeners.keydown({ key, preventDefault: () => {} });
    }
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const results = [];

    function matchesSelector(node, sel) {
      if (!node || !node.tagName) return false;
      sel = sel.trim();
      let tag = null;
      let rest = sel;
      const tagMatch = sel.match(/^[a-zA-Z0-9]+/);
      if (tagMatch) {
        tag = tagMatch[0].toUpperCase();
        rest = sel.slice(tag.length);
      }
      if (tag && node.tagName !== tag) return false;

      while (rest.length > 0) {
        if (rest.startsWith(".")) {
          const m = rest.match(/^\.([a-zA-Z0-9_-]+)/);
          if (!m) return false;
          const cls = m[1];
          if (!node.className || !node.className.split(/\s+/).includes(cls)) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith("[")) {
          const m = rest.match(/^\[([a-zA-Z0-9_-]+)(?:([*^$]?=)(["']?)(.*?)\3)?\]/);
          if (!m) return false;
          const attr = m[1];
          const op = m[2];
          const val = m[4];
          const actual = node.getAttribute?.(attr);
          if (actual === null || actual === undefined) return false;
          if (op === "=" && actual !== val) return false;
          if (op === "*=" && !actual.includes(val)) return false;
          rest = rest.slice(m[0].length);
        } else {
          break;
        }
      }
      return true;
    }

    function search(node) {
      if (matchesSelector(node, selector)) {
        results.push(node);
      }
      if (Array.isArray(node?.children)) {
        for (const child of node.children) {
          search(child);
        }
      }
    }

    for (const child of this.children) {
      search(child);
    }
    return results;
  }

  get text() {
    return (
      (this.textContent || "") +
      " " +
      this.children.map((c) => (c.text !== undefined ? c.text : c.textContent || "")).join(" ")
    ).trim();
  }
}

class MockDocument {
  createElement(tag) {
    return new MockNode(tag);
  }

  createTextNode(text) {
    return { textContent: text, text };
  }
}

// Unit tests for helper functions
test("formatIngredientSourceText formats quantity, unit, name preserving original text", () => {
  assert.equal(
    unmappedView.formatIngredientSourceText({
      name: "hvetemel",
      quantity: 200,
      unit: "g",
    }),
    "200 g hvetemel",
  );

  assert.equal(
    unmappedView.formatIngredientSourceText({
      name: "egg",
      quantity: 3,
      unit: "stk",
    }),
    "3 stk egg",
  );

  assert.equal(
    unmappedView.formatIngredientSourceText({
      name: "salt etter smak",
      quantity: null,
      unit: null,
    }),
    "salt etter smak",
  );

  assert.equal(unmappedView.formatIngredientSourceText(null), "");
});

test("ingredientNeeds accurately detects missing product and unit mappings", () => {
  const needsBoth = unmappedView.ingredientNeeds({
    name: "smør",
    unit: "ss",
    canonical_product_id: null,
    canonical_unit_id: null,
  });
  assert.deepEqual(needsBoth, { needsProduct: true, needsUnit: true, isUnmapped: true });
  assert.equal(unmappedView.mappingStatusLabel(needsBoth), "Mangler råvare og måleenhet");

  const needsProductOnly = unmappedView.ingredientNeeds({
    name: "tomat",
    unit: null,
    canonical_product_id: null,
    canonical_unit_id: null,
  });
  assert.deepEqual(needsProductOnly, { needsProduct: true, needsUnit: false, isUnmapped: true });
  assert.equal(unmappedView.mappingStatusLabel(needsProductOnly), "Mangler kanonisk råvare");

  const needsUnitOnly = unmappedView.ingredientNeeds({
    name: "sukker",
    unit: "ts",
    canonical_product_id: "00000000-0000-0000-0000-000000000001",
    canonical_unit_id: null,
  });
  assert.deepEqual(needsUnitOnly, { needsProduct: false, needsUnit: true, isUnmapped: true });
  assert.equal(unmappedView.mappingStatusLabel(needsUnitOnly), "Mangler kanonisk måleenhet");

  const complete = unmappedView.ingredientNeeds({
    name: "melk",
    unit: "dl",
    canonical_product_id: "00000000-0000-0000-0000-000000000001",
    canonical_unit_id: "00000000-0000-0000-0000-000000000002",
  });
  assert.deepEqual(complete, { needsProduct: false, needsUnit: false, isUnmapped: false });
  assert.equal(unmappedView.mappingStatusLabel(complete), "Ferdig koblet");
});

test("normalizeAliasSearchResponse validates alias candidates and status", () => {
  const normalized = unmappedView.normalizeAliasSearchResponse({
    status: "resolved",
    candidates: [
      { id: "cand-1", canonical_name: "Smør", variant: "meierismør" },
      { id: 123 }, // invalid, should be filtered
    ],
  });
  assert.equal(normalized.status, "resolved");
  assert.equal(normalized.candidates.length, 1);
  assert.equal(normalized.candidates[0].canonical_name, "Smør");

  assert.deepEqual(unmappedView.normalizeAliasSearchResponse(null), {
    status: "unmapped",
    candidates: [],
  });
});

test("buildMapIngredientPayload validates required fields and options", () => {
  const payload = unmappedView.buildMapIngredientPayload({
    recipe_id: "rec-1",
    ingredient_index: 0,
    canonical_product_id: "prod-1",
    add_alias: true,
    locale: "nb",
  });
  assert.deepEqual(payload, {
    recipe_id: "rec-1",
    ingredient_index: 0,
    canonical_product_id: "prod-1",
    canonical_unit_id: null,
    add_alias: true,
    locale: "nb",
    context: "recipe",
  });

  assert.throws(
    () => unmappedView.buildMapIngredientPayload({ recipe_id: "" }),
    /Oppskrifts-ID er påkrevd/,
  );
  assert.throws(
    () =>
      unmappedView.buildMapIngredientPayload({
        recipe_id: "rec-1",
        ingredient_index: -1,
      }),
    /Ugyldig ingrediensindeks/,
  );
  assert.throws(
    () =>
      unmappedView.buildMapIngredientPayload({
        recipe_id: "rec-1",
        ingredient_index: 0,
      }),
    /Minst én kanonisk råvare eller måleenhet må velges/,
  );
});

test("buildCreateProductPayload and buildCreateUnitPayload validate non-blank inputs", () => {
  assert.deepEqual(unmappedView.buildCreateProductPayload(" Smør ", " usaltet "), {
    canonical_name: "Smør",
    variant: "usaltet",
  });
  assert.throws(() => unmappedView.buildCreateProductPayload("   "), /Råvarenavn kan ikke være tomt/);

  assert.deepEqual(unmappedView.buildCreateUnitPayload(" gram "), {
    canonical_name: "gram",
  });
  assert.throws(() => unmappedView.buildCreateUnitPayload(""), /Måleenhetsnavn kan ikke være tomt/);
});

// DOM & Integration tests
test("renders list of unmapped ingredients with original text and badges", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const unmappedItems = [
    {
      recipe_id: "recipe-1",
      recipe_title: "Brun lapskaus",
      ingredient_index: 0,
      name: "oksekjøtt i terninger",
      quantity: 500,
      unit: "g",
      source_unit_expression: "g",
      canonical_product_id: null,
      canonical_unit_id: null,
    },
    {
      recipe_id: "recipe-2",
      recipe_title: "Pannekaker",
      ingredient_index: 1,
      name: "hvetemel",
      quantity: 3,
      unit: "dl",
      source_unit_expression: null,
      canonical_product_id: "prod-existing",
      canonical_unit_id: null,
    },
  ];

  let apiCalls = [];
  const api = async (endpoint, method = "GET", body = null) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      return unmappedItems;
    }
    return {};
  };

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  // Initial load happens synchronously in test environment
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0].endpoint, "/catalogue/unmapped-ingredients");

  const cards = root.querySelectorAll(".unmapped-ingredient-card");
  assert.equal(cards.length, 2);

  // Check card 1: Brun lapskaus
  assert.ok(cards[0].text.includes("Brun lapskaus"));
  assert.ok(cards[0].text.includes("500 g oksekjøtt i terninger"));
  assert.ok(cards[0].text.includes("Opprinnelig uttrykk: g"));
  assert.ok(cards[0].text.includes("Mangler råvare og måleenhet"));

  // Check card 2: Pannekaker
  assert.ok(cards[1].text.includes("Pannekaker"));
  assert.ok(cards[1].text.includes("3 dl hvetemel"));
  assert.ok(cards[1].text.includes("Mangler kanonisk måleenhet"));
  assert.ok(cards[1].text.includes("Kanonisk råvare er allerede koblet"));
});

test("renders empty state when there are no unmapped ingredients", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const api = async () => [];

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  const emptyDiv = root.querySelector(".unmapped-empty");
  assert.ok(emptyDiv);
  assert.ok(emptyDiv.text.includes("Ingen ukartlagte ingredienser funnet"));
  assert.ok(emptyDiv.text.includes("Alle oppskriftsingredienser har en koblet råvare og måleenhet"));
});

test("recipe filter updates the query to /catalogue/unmapped-ingredients?recipe_id=...", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const unmappedItems = [
    {
      recipe_id: "rec-1",
      recipe_title: "Suppe",
      ingredient_index: 0,
      name: "løk",
      quantity: 1,
      unit: "stk",
      canonical_product_id: null,
      canonical_unit_id: null,
    },
  ];

  let apiCalls = [];
  const api = async (endpoint) => {
    apiCalls.push(endpoint);
    return unmappedItems;
  };

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;
  assert.equal(apiCalls[0], "/catalogue/unmapped-ingredients");

  const filterSelect = root.querySelector("select");
  assert.ok(filterSelect);
  assert.equal(filterSelect.getAttribute("aria-label"), "Filtrer etter oppskrift");

  filterSelect.value = "rec-1";
  await filterSelect.change();

  assert.equal(apiCalls[1], "/catalogue/unmapped-ingredients?recipe_id=rec-1");
});

test("search and select existing canonical product alias enables mapping button", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const item = {
    recipe_id: "rec-1",
    recipe_title: "Fiskesuppe",
    ingredient_index: 0,
    name: "torskefilet",
    quantity: 400,
    unit: null,
    canonical_product_id: null,
    canonical_unit_id: null,
  };

  const apiCalls = [];
  const api = async (endpoint, method = "GET", body = null) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/catalogue/unmapped-ingredients/map") {
      return { id: "rec-1", title: "Fiskesuppe" };
    }
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      return [item];
    }
    if (endpoint.startsWith("/catalogue/aliases/product")) {
      return {
        status: "resolved",
        candidates: [
          { id: "prod-cod", canonical_name: "Torsk", variant: "fersk filet" },
        ],
      };
    }
    return {};
  };

  let mappedRecipe = null;
  const view = unmappedView.renderReviewDialog(root, {
    document: doc,
    api,
    onMapped: (rec) => {
      mappedRecipe = rec;
    },
  });
  await view.ready;

  const card = root.querySelector(".unmapped-ingredient-card");
  const mapBtn = card.querySelector(".unmapped-map-btn");
  assert.equal(mapBtn.disabled, true, "map button starts disabled");

  // Search product alias
  const searchBtn = card.querySelector('button[aria-label="Søk etter råvare for torskefilet"]');
  assert.ok(searchBtn);
  await searchBtn.click();

  assert.ok(
    apiCalls.some((c) => c.endpoint.startsWith("/catalogue/aliases/product?alias=torskefilet")),
  );

  // Candidate button should appear
  const chooseBtn = card.querySelector(".unmapped-candidate-btn");
  assert.ok(chooseBtn);
  assert.ok(chooseBtn.text.includes("Torsk (fersk filet)"));

  await chooseBtn.click();

  const selectionInfo = card.querySelector(".unmapped-selection-info");
  assert.ok(selectionInfo.text.includes("Valgt råvare: Torsk (fersk filet)"));
  assert.equal(mapBtn.disabled, false, "map button is now enabled");

  // Click map button
  await mapBtn.click();

  const mapCall = apiCalls.find((c) => c.endpoint === "/catalogue/unmapped-ingredients/map");
  assert.ok(mapCall);
  assert.deepEqual(mapCall.body, {
    recipe_id: "rec-1",
    ingredient_index: 0,
    canonical_product_id: "prod-cod",
    canonical_unit_id: null,
    add_alias: true,
    locale: "nb",
    context: "recipe",
  });

  assert.ok(mappedRecipe);
  assert.equal(mappedRecipe.title, "Fiskesuppe");
});

test("create new canonical product and unit and map with alias checkbox", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const item = {
    recipe_id: "rec-2",
    recipe_title: "Kake",
    ingredient_index: 0,
    name: "vaniljestang",
    quantity: 1,
    unit: "stk",
    canonical_product_id: null,
    canonical_unit_id: null,
  };

  const apiCalls = [];
  const api = async (endpoint, method = "GET", body = null) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/catalogue/unmapped-ingredients/map") {
      return { id: "rec-2" };
    }
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      return [item];
    }
    if (endpoint === "/catalogue/identities/products") {
      return { id: "new-prod-id", canonical_name: body.canonical_name, variant: body.variant };
    }
    if (endpoint === "/catalogue/identities/units") {
      return { id: "new-unit-id", canonical_name: body.canonical_name };
    }
    return {};
  };

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  const card = root.querySelector(".unmapped-ingredient-card");

  // Create product
  const createProductBtn = card.querySelector('button[aria-label="Opprett og velg ny kanonisk råvare"]');
  assert.ok(createProductBtn);
  const prodNameInput = card.querySelector('.unmapped-create-name[aria-label="Navn på ny kanonisk råvare"]');
  prodNameInput.value = "Vanilje";
  const prodVariantInput = card.querySelector('.unmapped-create-variant');
  prodVariantInput.value = "stang";

  await createProductBtn.click();

  assert.ok(apiCalls.some((c) => c.endpoint === "/catalogue/identities/products" && c.body.canonical_name === "Vanilje"));

  // Create unit
  const createUnitBtn = card.querySelector('button[aria-label="Opprett og velg ny kanonisk måleenhet"]');
  assert.ok(createUnitBtn);
  const unitNameInput = card.querySelector('.unmapped-create-name[aria-label="Navn på ny kanonisk måleenhet"]');
  unitNameInput.value = "stk";

  await createUnitBtn.click();

  assert.ok(apiCalls.some((c) => c.endpoint === "/catalogue/identities/units" && c.body.canonical_name === "stk"));

  // Uncheck alias
  const aliasCheckbox = card.querySelector('input[type="checkbox"]');
  assert.equal(aliasCheckbox.checked, true);
  aliasCheckbox.checked = false;

  // Submit mapping
  const mapBtn = card.querySelector(".unmapped-map-btn");
  assert.equal(mapBtn.disabled, false);
  await mapBtn.click();

  const mapCall = apiCalls.find((c) => c.endpoint === "/catalogue/unmapped-ingredients/map");
  assert.ok(mapCall);
  assert.equal(mapCall.body.canonical_product_id, "new-prod-id");
  assert.equal(mapCall.body.canonical_unit_id, "new-unit-id");
  assert.equal(mapCall.body.add_alias, false);
});

test("handles load, search, and mapping errors with Bokmål messages and alerts", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  let shouldFailLoad = true;
  let shouldFailMap = false;

  const item = {
    recipe_id: "rec-3",
    recipe_title: "Gryte",
    ingredient_index: 0,
    name: "bønner",
    canonical_product_id: null,
    canonical_unit_id: null,
  };

  const api = async (endpoint, method, body) => {
    if (endpoint.startsWith("/catalogue/unmapped-ingredients/map") && shouldFailMap) {
      throw new Error("Feil ved mapping av råvare i databasen.");
    }
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      if (shouldFailLoad) {
        throw new Error("Nettverksfeil mot server");
      }
      return [item];
    }
    if (endpoint.startsWith("/catalogue/aliases/product")) {
      throw new Error("Synonymsøk feilet");
    }
    if (endpoint === "/catalogue/identities/products") {
      return { id: "p-beans", canonical_name: "Bønner" };
    }
    return {};
  };

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  // Initial load error
  const errorNode = root.querySelector(".unmapped-error");
  assert.equal(errorNode.hidden, false);
  assert.ok(errorNode.text.includes("Kunne ikke laste inn ukartlagte ingredienser: Nettverksfeil mot server"));
  assert.equal(errorNode.getAttribute("role"), "alert");

  // Retry button
  const contentArea = root.querySelector(".unmapped-content");
  const retryBtn = contentArea.querySelector("button");
  assert.equal(retryBtn.text.trim(), "Prøv igjen");
  shouldFailLoad = false;
  await retryBtn.click();

  assert.equal(errorNode.hidden, true);
  const card = root.querySelector(".unmapped-ingredient-card");
  assert.ok(card);

  // Search error
  const searchBtn = card.querySelector('button[aria-label="Søk etter råvare for bønner"]');
  await searchBtn.click();
  const searchResults = card.querySelector(".unmapped-search-results");
  assert.ok(searchResults.text.includes("Søk mislyktes: Synonymsøk feilet"));

  // Select via create
  const createProductBtn = card.querySelector('button[aria-label="Opprett og velg ny kanonisk råvare"]');
  await createProductBtn.click();

  // Mapping failure
  shouldFailMap = true;
  const mapBtn = card.querySelector(".unmapped-map-btn");
  assert.equal(mapBtn.disabled, false);
  await mapBtn.click();

  const cardError = card.querySelector(".unmapped-card-error");
  assert.equal(cardError.hidden, false);
  assert.ok(cardError.text.includes("Kunne ikke koble ingrediens: Feil ved mapping av råvare i databasen."));
  assert.equal(cardError.getAttribute("role"), "alert");
  assert.equal(mapBtn.disabled, false, "map button is re-enabled for retry");
});

test("accessibility: verifies proper roles, aria-live, and accessible labels across the dialog", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const item = {
    recipe_id: "rec-4",
    recipe_title: "Pastarett",
    ingredient_index: 0,
    name: "parmesan",
    quantity: 50,
    unit: "g",
    canonical_product_id: null,
    canonical_unit_id: null,
  };

  const api = async () => [item];

  const view = unmappedView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  // Status element
  const statusNode = root.querySelector(".unmapped-status");
  assert.equal(statusNode.getAttribute("role"), "status");
  assert.equal(statusNode.getAttribute("aria-live"), "polite");

  // Filter select
  const select = root.querySelector("select");
  assert.ok(select.getAttribute("aria-label"));

  // Search input and button
  const card = root.querySelector(".unmapped-ingredient-card");
  const prodSearchInput = card.querySelector('input[type="search"]');
  assert.ok(prodSearchInput.getAttribute("aria-label").includes("parmesan"));

  // Sections have aria-labels
  const sections = card.querySelectorAll(".unmapped-section");
  assert.ok(sections.length >= 2);
  assert.ok(sections[0].getAttribute("aria-label").includes("parmesan"));
  assert.ok(sections[1].getAttribute("aria-label").includes("g"));

  // Map button
  const mapBtn = card.querySelector(".unmapped-map-btn");
  assert.ok(mapBtn.getAttribute("aria-label").includes("parmesan"));

  // Footer close button
  const closeBtn = root.querySelector('button[aria-label="Lukk dialogvindu for ukartlagte ingredienser"]');
  assert.ok(closeBtn);
});
