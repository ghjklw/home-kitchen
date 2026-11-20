"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const translationsView = require("../src/home_kitchen/static/recipe-translations-view.js");

class MockNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this._textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.disabled = false;
    this.value = "";
    this.hidden = false;
    this.type = "text";
  }

  get textContent() {
    return (this._textContent || "") + this.children.map((c) => c.textContent || "").join("");
  }

  set textContent(val) {
    this._textContent = String(val ?? "");
  }

  append(...children) {
    for (const child of children) {
      if (child) {
        this.children.push(child);
      }
    }
  }

  replaceChildren(...children) {
    this._textContent = "";
    this.children = children.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  async click() {
    if (!this.disabled && this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {} });
    }
  }

  async change(val) {
    if (val !== undefined) this.value = val;
    if (this.listeners.change) {
      await this.listeners.change({ target: this });
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
}

// Global document shim for testing
globalThis.document = {
  createElement: (tag) => new MockNode(tag),
};

test("formatLanguageName correctly localizes language codes to Bokmål", () => {
  assert.equal(translationsView.formatLanguageName("nb"), "Norsk (bokmål)");
  assert.equal(translationsView.formatLanguageName("en"), "Engelsk");
  assert.equal(translationsView.formatLanguageName("da"), "Dansk");
  assert.equal(translationsView.formatLanguageName("sv"), "Svensk");
  assert.equal(translationsView.formatLanguageName("fr"), "Fransk");
  assert.equal(translationsView.formatLanguageName("und"), "Ukjent språk");
  assert.equal(translationsView.formatLanguageName(""), "Ukjent språk");
  assert.equal(translationsView.formatLanguageName(null), "Ukjent språk");
  assert.equal(translationsView.formatLanguageName("es"), "es");
});

test("renderLanguageBadge creates badge with accessibility attributes and localized text", () => {
  const badge = translationsView.renderLanguageBadge("en");
  assert.equal(badge.className, "badge recipe-language-badge");
  assert.equal(badge.dataset.language, "en");
  assert.equal(badge.textContent, "Kildespråk: Engelsk");
  assert.match(badge.getAttribute("aria-label"), /Oppskriftens kildespråk er Engelsk/);

  const unknownBadge = translationsView.renderLanguageBadge(null);
  assert.equal(unknownBadge.dataset.language, "und");
  assert.equal(unknownBadge.textContent, "Kildespråk: Ukjent språk");
});

test("getTranslationErrorMessage maps unconfigured AI and standard errors cleanly to Bokmål", () => {
  const err503 = { status: 503, message: "Configure an AI model in household settings to enable automatic translations." };
  assert.match(
    translationsView.getTranslationErrorMessage(err503),
    /AI-modell er ikke konfigurert\. Konfigurer en AI-modell under Innstillinger/,
  );

  const errText = { message: "Value error: Configure an AI model" };
  assert.match(
    translationsView.getTranslationErrorMessage(errText),
    /AI-modell er ikke konfigurert/,
  );

  const err404 = { status: 404, message: "Not found" };
  assert.equal(
    translationsView.getTranslationErrorMessage(err404),
    "Oppskriften eller oversettelsen ble ikke funnet.",
  );

  const generic = { message: "Tilkobling tidsavbrutt" };
  assert.equal(
    translationsView.getTranslationErrorMessage(generic),
    "Kunne ikke oversette oppskriften: Tilkobling tidsavbrutt",
  );
});

test("attachRecipeTranslation renders source language badge and defaults to target locale", () => {
  const container = new MockNode("div");
  const recipe = {
    id: "rec-1",
    title: "Chicken Tikka Masala",
    source_language: "en",
    ingredients: [{ name: "chicken", quantity: 500, unit: "g" }],
    instructions: ["Cut chicken", "Cook sauce"],
  };

  const controller = translationsView.attachRecipeTranslation(container, recipe, {
    api: async () => [],
    locale: "nb",
  });

  const badge = container.querySelector(".recipe-language-badge");
  assert.ok(badge);
  assert.equal(badge.textContent, "Kildespråk: Engelsk");

  const mode = container.querySelector(".translation-mode-indicator");
  assert.ok(mode);
  assert.equal(mode.textContent, "Viser original");

  const select = container.querySelector(".translation-locale-select");
  assert.ok(select);
  assert.equal(select.value, "nb");

  const translateBtn = container.querySelector(".translate-btn");
  assert.ok(translateBtn);
  assert.equal(translateBtn.textContent, "Oversett til Norsk (bokmål)");
  assert.equal(translateBtn.hidden, false);

  const viewOriginalBtn = container.querySelector(".view-original-btn");
  assert.ok(viewOriginalBtn);
  assert.equal(viewOriginalBtn.hidden, true);
});

test("on-demand translation request updates content callback with versioned translation", async () => {
  const container = new MockNode("div");
  const recipe = {
    id: "rec-123",
    title: "English Stew",
    source_language: "en",
    ingredients: [{ name: "beef", quantity: 400, unit: "g" }],
    instructions: ["Simmer slowly"],
  };

  const translationPayload = {
    id: "trans-abc",
    recipe_id: "rec-123",
    target_locale: "nb",
    recipe_version: "abcdef123456",
    title: "Norsk gryte",
    instructions: ["Småkok sakte"],
    ingredients: [{ name: "storfekjøtt", quantity: 400, unit: "g" }],
    provider: "anthropic",
    model: "claude-3-5-sonnet",
    created_at: "2026-11-16T12:00:00Z",
    is_stale: false,
  };

  let requestedEndpoint = null;
  let requestedPayload = null;
  const mockApi = async (endpoint, method, body) => {
    if (endpoint === "/recipes/rec-123/translations") return [];
    if (endpoint === "/recipes/rec-123/translate") {
      requestedEndpoint = endpoint;
      requestedPayload = body;
      return translationPayload;
    }
    throw new Error(`Unexpected endpoint: ${endpoint}`);
  };

  let contentReceived = null;
  const controller = translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
    onContentChange: (content) => {
      contentReceived = content;
    },
  });

  const translateBtn = container.querySelector(".translate-btn");
  await translateBtn.click();

  assert.equal(requestedEndpoint, "/recipes/rec-123/translate");
  assert.deepEqual(requestedPayload, { target_locale: "nb", force: false });

  // Content callback received the translated title, ingredients, instructions
  assert.ok(contentReceived);
  assert.equal(contentReceived.isTranslated, true);
  assert.equal(contentReceived.title, "Norsk gryte");
  assert.deepEqual(contentReceived.instructions, ["Småkok sakte"]);
  assert.equal(contentReceived.ingredients[0].name, "storfekjøtt");

  // Mode indicator and buttons updated
  const mode = container.querySelector(".translation-mode-indicator");
  assert.equal(mode.textContent, "Viser oversettelse (Norsk (bokmål))");

  const viewOriginalBtn = container.querySelector(".view-original-btn");
  assert.equal(viewOriginalBtn.hidden, false);
  assert.equal(translateBtn.hidden, true);

  // Meta details rendered
  const meta = container.querySelector(".recipe-translation-meta");
  assert.equal(meta.hidden, false);
  const details = meta.querySelector(".translation-details");
  assert.match(details.textContent, /Leverandør: anthropic/);
  assert.match(details.textContent, /Modell: claude-3-5-sonnet/);
  assert.match(details.textContent, /Versjon: abcdef12/);
});

test("one-click toggle switches between translation and immutable original recipe without mutating recipe", async () => {
  const container = new MockNode("div");
  const recipe = Object.freeze({
    id: "rec-imm",
    title: "Original Apple Pie",
    source_language: "en",
    ingredients: Object.freeze([{ name: "apples", quantity: 3, unit: "pcs" }]),
    instructions: Object.freeze(["Bake at 180C"]),
  });

  const translationPayload = {
    id: "trans-pie",
    recipe_id: "rec-imm",
    target_locale: "nb",
    recipe_version: "hash999",
    title: "Eplekake",
    instructions: ["Stek på 180 grader"],
    ingredients: [{ name: "epler", quantity: 3, unit: "stk" }],
    provider: "anthropic",
    model: "claude-3-5-sonnet",
    created_at: "2026-11-16T12:00:00Z",
    is_stale: false,
  };

  const mockApi = async (endpoint) => {
    if (endpoint === "/recipes/rec-imm/translations") return [];
    if (endpoint === "/recipes/rec-imm/translate") return translationPayload;
    throw new Error("unexpected");
  };

  let contentReceived = null;
  translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
    onContentChange: (content) => {
      contentReceived = content;
    },
  });

  // 1. Translate
  await container.querySelector(".translate-btn").click();
  assert.equal(contentReceived.title, "Eplekake");
  assert.equal(contentReceived.isTranslated, true);

  // 2. Toggle back to original in one click
  const viewOriginalBtn = container.querySelector(".view-original-btn");
  await viewOriginalBtn.click();

  assert.equal(contentReceived.title, "Original Apple Pie");
  assert.equal(contentReceived.isTranslated, false);
  assert.equal(contentReceived.translation, null);
  assert.equal(recipe.title, "Original Apple Pie");

  // Mode indicator shows original
  const mode = container.querySelector(".translation-mode-indicator");
  assert.equal(mode.textContent, "Viser original");

  // "Vis oversettelse" button is now shown
  const viewTranslationBtn = container.querySelector(".view-translation-btn");
  assert.equal(viewTranslationBtn.hidden, false);
  assert.equal(viewOriginalBtn.hidden, true);

  // 3. Toggle back to translation in one click
  await viewTranslationBtn.click();
  assert.equal(contentReceived.title, "Eplekake");
  assert.equal(contentReceived.isTranslated, true);
});

test("stale translation renders warning and offers force update button", async () => {
  const container = new MockNode("div");
  const recipe = {
    id: "rec-stale",
    title: "Updated Salad",
    source_language: "en",
    ingredients: [],
    instructions: [],
  };

  const staleTranslation = {
    id: "trans-stale",
    recipe_id: "rec-stale",
    target_locale: "nb",
    recipe_version: "oldhash",
    title: "Gammel salat",
    instructions: ["Bland"],
    ingredients: [],
    provider: "openai",
    model: "gpt-4o",
    created_at: "2026-10-01T10:00:00Z",
    is_stale: true,
  };

  let forceCalled = false;
  const mockApi = async (endpoint, method, body) => {
    if (endpoint === "/recipes/rec-stale/translations") return [];
    if (endpoint === "/recipes/rec-stale/translate") {
      if (body?.force) forceCalled = true;
      return staleTranslation;
    }
  };

  translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
  });

  await container.querySelector(".translate-btn").click();

  // Stale notice is rendered
  const staleNotice = container.querySelector(".translation-stale-notice");
  assert.ok(staleNotice);
  assert.match(staleNotice.textContent, /Oppskriften har blitt endret siden oversettelsen ble laget/);

  // Refresh button is visible
  const refreshBtn = container.querySelector(".refresh-translation-btn");
  assert.equal(refreshBtn.hidden, false);

  await refreshBtn.click();
  assert.equal(forceCalled, true);
});

test("unconfigured AI error displays Bokmål alert and retains original recipe", async () => {
  const container = new MockNode("div");
  const recipe = {
    id: "rec-no-ai",
    title: "French Bread",
    source_language: "fr",
    ingredients: [],
    instructions: [],
  };

  let notifiedError = null;
  const mockApi = async (endpoint) => {
    if (endpoint === "/recipes/rec-no-ai/translations") return [];
    if (endpoint === "/recipes/rec-no-ai/translate") {
      const err = new Error("Configure an AI model in household settings to enable automatic translations.");
      err.status = 503;
      throw err;
    }
  };

  let contentCalled = false;
  translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
    notify: (msg, isErr) => {
      if (isErr) notifiedError = msg;
    },
    onContentChange: () => {
      contentCalled = true;
    },
  });

  const translateBtn = container.querySelector(".translate-btn");
  await translateBtn.click();

  // Content change callback was NOT called
  assert.equal(contentCalled, false);

  // Error message displayed with role="alert"
  const errorBox = container.querySelector(".translation-error-message");
  assert.equal(errorBox.hidden, false);
  assert.equal(errorBox.getAttribute("role"), "alert");
  assert.match(errorBox.textContent, /AI-modell er ikke konfigurert/);
  assert.match(notifiedError, /AI-modell er ikke konfigurert/);

  // Translate button is re-enabled
  assert.equal(translateBtn.disabled, false);
  assert.equal(translateBtn.getAttribute("aria-busy"), null);
});

test("buildManualTranslationPayload validates and normalizes fields in Bokmål", () => {
  const valid = translationsView.buildManualTranslationPayload({
    title: "  Norsk eplekake  ",
    ingredients: [
      { name: "epler", quantity: "3,5", unit: "stk", category: "Produce", source_indices: [0] },
      { name: "kanel", quantity: "", unit: "" },
    ],
    instructions: "  Skrell eplene  \n\nStek i ovn ",
  });
  assert.deepEqual(valid, {
    title: "Norsk eplekake",
    ingredients: [
      { name: "epler", quantity: "3.5", unit: "stk", category: "Produce", source_indices: [0] },
      { name: "kanel", quantity: null, unit: null, category: "Other", source_indices: [] },
    ],
    instructions: ["Skrell eplene", "Stek i ovn"],
  });

  assert.throws(
    () => translationsView.buildManualTranslationPayload({ title: " ", ingredients: [{ name: "salt" }], instructions: "Kok" }),
    /Oppgi en tittel for oversettelsen/,
  );
  assert.throws(
    () => translationsView.buildManualTranslationPayload({ title: "Suppe", ingredients: [{ name: "salt", quantity: "1", unit: "" }], instructions: "Kok" }),
    /Oppgi enhet for alle ingredienser som har mengde/,
  );
  assert.throws(
    () => translationsView.buildManualTranslationPayload({ title: "Suppe", ingredients: [{ name: "salt", quantity: "-2", unit: "g" }], instructions: "Kok" }),
    /Oppgi en gyldig positiv mengde/,
  );
  assert.throws(
    () => translationsView.buildManualTranslationPayload({ title: "Suppe", ingredients: [{ name: "salt" }], instructions: "  " }),
    /Legg til minst ett steg/,
  );
});

test("saveManualTranslation sends PUT /recipes/{id}/translations/{locale}", async () => {
  const calls = [];
  const api = async (endpoint, method, body) => {
    calls.push({ endpoint, method, body });
    return {
      id: "trans-manual",
      recipe_id: "rec-42",
      target_locale: "nb",
      recipe_version: "ver123",
      ...body,
      provider: "manual",
      model: "manual",
      is_stale: false,
    };
  };

  const result = await translationsView.saveManualTranslation(
    "rec-42",
    "NB",
    {
      title: "Hjemmelaget brød",
      ingredients: [{ name: "hvetemel", quantity: "500", unit: "g" }],
      instructions: ["Elt deigen", "Stek"],
    },
    { api },
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].endpoint, "/recipes/rec-42/translations/nb");
  assert.equal(calls[0].method, "PUT");
  assert.deepEqual(calls[0].body, {
    title: "Hjemmelaget brød",
    ingredients: [{ name: "hvetemel", quantity: "500", unit: "g", category: "Other", source_indices: [] }],
    instructions: ["Elt deigen", "Stek"],
  });
  assert.equal(result.provider, "manual");
  assert.equal(result.model, "manual");
  assert.equal(result.is_stale, false);
});

test("Rediger oversettelse allows editing a translation and saving via PUT without altering canonical recipe", async () => {
  const container = new MockNode("div");
  const recipe = Object.freeze({
    id: "rec-manual-edit",
    title: "English Stew",
    source_language: "en",
    ingredients: Object.freeze([
      Object.freeze({ name: "beef", quantity: 400, unit: "g", category: "Meat", source_indices: [0] }),
    ]),
    instructions: Object.freeze(["Simmer slowly"]),
  });

  const staleTranslation = {
    id: "trans-ai",
    recipe_id: "rec-manual-edit",
    target_locale: "nb",
    recipe_version: "oldhash1",
    title: "Engelsk gryte",
    ingredients: [{ name: "oksekjøtt", quantity: "400", unit: "g", category: "Meat", source_indices: [0] }],
    instructions: ["Småkok"],
    provider: "anthropic",
    model: "claude-3-5-sonnet",
    created_at: "2026-11-16T12:00:00Z",
    is_stale: true,
  };

  const apiCalls = [];
  const mockApi = async (endpoint, method = "GET", body) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/recipes/rec-manual-edit/translations" && method === "GET") return [];
    if (endpoint === "/recipes/rec-manual-edit/translate" && method === "POST") return staleTranslation;
    if (endpoint === "/recipes/rec-manual-edit/translations/nb" && method === "PUT") {
      return {
        id: "trans-ai",
        recipe_id: "rec-manual-edit",
        target_locale: "nb",
        recipe_version: "currenthash",
        ...body,
        provider: "manual",
        model: "manual",
        created_at: "2026-11-20T18:00:00Z",
        is_stale: false,
      };
    }
    throw new Error(`Unexpected call: ${method} ${endpoint}`);
  };

  let latestContent = null;
  const notifications = [];
  const controller = translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
    notify: (msg, isErr) => notifications.push({ msg, isErr: Boolean(isErr) }),
    onContentChange: (content) => {
      latestContent = content;
    },
  });

  // Load stale AI translation first
  await container.querySelector(".translate-btn").click();
  assert.ok(container.querySelector(".translation-stale-notice"));

  // Click "Rediger oversettelse"
  const editBtn = container.querySelector(".edit-translation-btn");
  assert.equal(editBtn.textContent, "Rediger oversettelse");
  assert.equal(editBtn.hidden, false);
  await editBtn.click();

  assert.equal(controller.isEditingTranslation(), true);
  const editForm = container.querySelector(".translation-edit-form");
  assert.equal(editForm.hidden, false);
  assert.equal(editBtn.hidden, true);
  assert.match(container.querySelector(".translation-mode-indicator").textContent, /Redigerer oversettelse/);

  // Form pre-populated from current translation
  const titleInput = container.querySelector(".translation-edit-title");
  const instructionsInput = container.querySelector(".translation-edit-instructions");
  assert.equal(titleInput.value, "Engelsk gryte");
  assert.equal(instructionsInput.value, "Småkok");

  const ingNameInput = container.querySelector(".translation-ingredient-name");
  assert.equal(ingNameInput.value, "oksekjøtt");
  assert.equal(ingNameInput.getAttribute("aria-label"), "Ingrediensnavn 1");

  // Edit title, ingredient, add a second ingredient, edit instructions
  titleInput.value = "Langtidskokt storfegryte";
  ingNameInput.value = "høyrygg av storfe";
  await container.querySelector(".add-translation-ingredient-btn").click();
  const rowNames = container.querySelectorAll(".translation-ingredient-name");
  const rowQtys = container.querySelectorAll(".translation-ingredient-quantity");
  const rowUnits = container.querySelectorAll(".translation-ingredient-unit");
  assert.equal(rowNames.length, 2);
  rowNames[1].value = "timian";
  rowQtys[1].value = "1";
  rowUnits[1].value = "klype";
  instructionsInput.value = "Brun kjøttet\nSmåkok i 2 timer";

  await container.querySelector(".save-translation-btn").click();

  const putCall = apiCalls.find((c) => c.method === "PUT");
  assert.ok(putCall);
  assert.equal(putCall.endpoint, "/recipes/rec-manual-edit/translations/nb");
  assert.deepEqual(putCall.body, {
    title: "Langtidskokt storfegryte",
    ingredients: [
      { name: "høyrygg av storfe", quantity: "400", unit: "g", category: "Meat", source_indices: [0] },
      { name: "timian", quantity: "1", unit: "klype", category: "Other", source_indices: [] },
    ],
    instructions: ["Brun kjøttet", "Småkok i 2 timer"],
  });

  // View updated to saved manual translation; stale notice gone; meta shows manual
  assert.equal(controller.isEditingTranslation(), false);
  assert.equal(editForm.hidden, true);
  assert.equal(latestContent.isTranslated, true);
  assert.equal(latestContent.title, "Langtidskokt storfegryte");
  assert.equal(container.querySelector(".translation-stale-notice"), null);
  assert.match(container.querySelector(".translation-details").textContent, /Manuell oversettelse/);

  const statusBox = container.querySelector(".translation-status-message");
  assert.equal(statusBox.hidden, false);
  assert.equal(statusBox.getAttribute("role"), "status");
  assert.match(statusBox.textContent, /Oversettelsen er lagret/);
  assert.equal(notifications.some((n) => !n.isErr && /Oversettelsen er lagret/.test(n.msg)), true);

  // Canonical recipe remained completely untouched and toggleable
  assert.equal(recipe.title, "English Stew");
  assert.equal(recipe.ingredients[0].name, "beef");
  await container.querySelector(".view-original-btn").click();
  assert.equal(latestContent.title, "English Stew");
  assert.equal(latestContent.isTranslated, false);
});

test("Rediger oversettelse can create a manual translation without AI and handles errors cleanly", async () => {
  const container = new MockNode("div");
  const recipe = {
    id: "rec-create-manual",
    title: "Tarte aux pommes",
    source_language: "fr",
    ingredients: [{ name: "pommes", quantity: 4, unit: "stk" }],
    instructions: ["Cuire au four"],
  };

  let shouldFailSave = true;
  const apiCalls = [];
  const mockApi = async (endpoint, method = "GET", body) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/recipes/rec-create-manual/translations" && method === "GET") return [];
    if (endpoint === "/recipes/rec-create-manual/translations/nb" && method === "PUT") {
      if (shouldFailSave) {
        const err = new Error("Nettverksfeil");
        err.status = 500;
        throw err;
      }
      return {
        id: "trans-fr-nb",
        recipe_id: "rec-create-manual",
        target_locale: "nb",
        recipe_version: "v1",
        ...body,
        provider: "manual",
        model: "manual",
        created_at: "2026-11-20T18:05:00Z",
        is_stale: false,
      };
    }
    throw new Error(`Unexpected call: ${method} ${endpoint}`);
  };

  const controller = translationsView.attachRecipeTranslation(container, recipe, {
    api: mockApi,
    locale: "nb",
  });

  // Open manual edit directly from original view (no AI translation exists)
  await container.querySelector(".edit-translation-btn").click();
  const titleInput = container.querySelector(".translation-edit-title");
  assert.equal(titleInput.value, "Tarte aux pommes");

  // Validation error keeps form open and skips API
  titleInput.value = "   ";
  await container.querySelector(".save-translation-btn").click();
  const errorBox = container.querySelector(".translation-error-message");
  assert.equal(errorBox.hidden, false);
  assert.equal(errorBox.getAttribute("role"), "alert");
  assert.match(errorBox.textContent, /Oppgi en tittel for oversettelsen/);
  assert.equal(controller.isEditingTranslation(), true);
  assert.equal(apiCalls.filter((c) => c.method === "PUT").length, 0);

  // Server failure keeps form open with edits preserved and shows Bokmål error
  titleInput.value = "Fransk epleterte";
  container.querySelector(".translation-ingredient-name").value = "epler";
  container.querySelector(".translation-edit-instructions").value = "Stek i ovn";
  await container.querySelector(".save-translation-btn").click();
  assert.equal(errorBox.hidden, false);
  assert.match(errorBox.textContent, /Kunne ikke lagre oversettelsen: Nettverksfeil/);
  assert.equal(controller.isEditingTranslation(), true);
  assert.equal(titleInput.value, "Fransk epleterte");

  // Cancel closes form without mutating recipe
  await container.querySelector(".cancel-translation-edit-btn").click();
  assert.equal(controller.isEditingTranslation(), false);
  assert.equal(recipe.title, "Tarte aux pommes");

  // Retry and succeed
  shouldFailSave = false;
  await container.querySelector(".edit-translation-btn").click();
  container.querySelector(".translation-edit-title").value = "Fransk epleterte";
  container.querySelector(".translation-ingredient-name").value = "epler";
  container.querySelector(".translation-edit-instructions").value = "Stek i ovn";
  await container.querySelector(".save-translation-btn").click();
  assert.equal(controller.isEditingTranslation(), false);
  assert.equal(controller.isViewingTranslation(), true);
  assert.equal(controller.getCurrentTranslation().title, "Fransk epleterte");
});
