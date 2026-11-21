"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const KitchenRecipeDiscovery = require("../src/home_kitchen/static/recipe-discovery.js");
const KitchenRecipeEditor = require("../src/home_kitchen/static/recipe-editor.js");
const KitchenPlannedCooking = require("../src/home_kitchen/static/planned-cooking.js");
const KitchenShoppingView = require("../src/home_kitchen/static/shopping-view.js");
const KitchenRecipeQuickFeedback = require("../src/home_kitchen/static/recipe-feedback-quick.js");

const appSource = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);

function extractRange(startMarker, endMarker) {
  const start = appSource.indexOf(startMarker);
  const end = appSource.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `Located ${startMarker} -> ${endMarker}`);
  return appSource.slice(start, end);
}

class DomNode {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.className = "";
    this.id = "";
    this.type = "";
    this.name = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.readOnly = false;
    this.isConnected = true;
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this._textContent = "";
    this.classList = {
      contains: (cls) => String(this.className || "").split(/\s+/).includes(cls),
      toggle: (cls, force) => {
        const set = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
        const shouldAdd = force === undefined ? !set.has(cls) : Boolean(force);
        if (shouldAdd) set.add(cls);
        else set.delete(cls);
        this.className = [...set].join(" ");
      },
    };
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  get childNodes() {
    return this.children;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  remove() {}

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this._textContent = "";
    this.children = [...nodes];
  }

  addEventListener(eventName, handler) {
    this.listeners[eventName] = handler;
  }

  focus() {
    this.focused = true;
  }

  select() {
    this.selected = true;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const cls = selector.startsWith(".") ? selector.slice(1) : null;
    const id = selector.startsWith("#") ? selector.slice(1) : null;
    return walk(this).filter((node) => {
      if (cls) return String(node.className || "").split(/\s+/).includes(cls);
      if (id) return node.id === id;
      return node.tagName === selector.toUpperCase();
    });
  }

  async click() {
    if (this.disabled) return;
    if (typeof this.listeners.click === "function") {
      await this.listeners.click();
    }
  }

  async submit() {
    if (typeof this.listeners.submit === "function") {
      await this.listeners.submit({ preventDefault() {} });
    }
  }
}

function walk(node, acc = []) {
  acc.push(node);
  for (const child of node.children || []) {
    walk(child, acc);
  }
  return acc;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function createJourneyHarness() {
  const recipes = [];
  const meals = [];
  const interactions = [];
  const apiCalls = [];
  const notices = [];

  const doc = {
    createElement: (tag) => new DomNode(tag),
  };
  const modal = {
    open: false,
    dataset: {},
    attributes: {},
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
  const dialogRoot = new DomNode("div");
  const main = new DomNode("main");
  const noticeNode = new DomNode("div");
  const state = {
    view: "week",
    selectedDate: new Date("2026-11-25T00:00:00Z"),
    week: new Date("2026-11-23T00:00:00Z"),
    household: { default_servings: 2, language: "nb" },
    recipes,
    meals,
    recipeCollectionJob: null,
    recipeCollectionStatusNode: null,
  };

  const recipeUrl = "https://www.marmiton.org/recettes/recette_pasta-tomat_101.aspx";

  function computeStats(recipeId) {
    const ratingEntries = interactions
      .filter((entry) => entry.kind === "recipe_rating" && entry.recipe_id === recipeId)
      .reverse();
    const ratings = ratingEntries
      .map((entry) => entry.structured?.rating)
      .filter((val) => Number.isInteger(val));
    const latestMakeAgain = ratingEntries.find(
      (entry) => typeof entry.structured?.would_make_again === "boolean",
    )?.structured?.would_make_again ?? null;
    return {
      times_planned: meals.filter((m) => m.recipe_id === recipeId).length,
      times_cooked: 0,
      times_ordered: 0,
      latest_rating: ratings[0] ?? null,
      average_rating: ratings.length
        ? Math.round((ratings.reduce((a, b) => a + b, 0) / ratings.length) * 100) / 100
        : null,
      would_make_again: latestMakeAgain,
      feedback_count: ratingEntries.length,
    };
  }

  async function api(endpoint, method = "GET", body) {
    apiCalls.push({ endpoint, method, body });

    if (endpoint === "/recipe-discovery" && method === "POST") {
      return {
        query: body.query,
        sources: [
          {
            source: "marmiton",
            label: "Marmiton",
            status: "ok",
            result_count: 1,
            detail: "Mocked offline discovery",
          },
        ],
        results: [
          {
            source: "marmiton",
            title: "Kremet tomatpasta",
            url: recipeUrl,
            importable: true,
          },
        ],
      };
    }

    if (endpoint === "/recipe-drafts/ai" && method === "POST") {
      return {
        provider: "ai",
        requires_review: true,
        adapted: {
          title: "Kremet tomatpasta",
          servings: 2,
          recipe_type: "middag",
          prep_time_minutes: 20,
          source_language: "nb",
          ingredients: [
            { name: "Pasta", quantity: "200", unit: "g", source_indices: [0] },
            { name: "Fløte", quantity: "100", unit: "ml", source_indices: [1] },
          ],
          instructions: ["Kok pasta i lettsaltet vann.", "Rør inn fløte."],
          source_url: body.url,
          source_ingredients: [
            { index: 0, text: "200 g pasta" },
            { index: 1, text: "100 ml fløte" },
          ],
          provenance: { original_source: "imported", source_url: body.url },
        },
      };
    }

    if (endpoint === "/recipes" && method === "POST") {
      const created = {
        id: "rec-1",
        ...body,
        source: body.source || "ai",
      };
      recipes.push(created);
      return created;
    }

    if (endpoint === "/recipes?include_stats=true" && method === "GET") {
      return recipes.map((r) => ({ ...r, activity_stats: computeStats(r.id) }));
    }

    if (endpoint.startsWith("/shopping?") && method === "GET") {
      const url = new URL(`http://localhost${endpoint}`);
      const start = url.searchParams.get("start");
      const end = url.searchParams.get("end");
      const activeMeals = meals.filter((m) => m.date >= start && m.date <= end);
      const lines = activeMeals.flatMap((meal) => {
        const recipe = recipes.find((r) => r.id === meal.recipe_id);
        const scale = meal.servings / recipe.servings;
        return recipe.ingredients.map((item) => ({
          name: item.name,
          quantity: String(Number(item.quantity) * scale),
          unit: item.unit,
          origins: [`meal:${meal.id}`],
        }));
      });
      return { start, end, lines };
    }

    if (endpoint === "/meals" && method === "POST") {
      const created = { id: "meal-1", ...body };
      meals.push(created);
      return created;
    }

    if (endpoint === "/meals/meal-1/recipe" && method === "GET") {
      const meal = meals.find((m) => m.id === "meal-1");
      const recipe = recipes.find((r) => r.id === meal.recipe_id);
      const scale = meal.servings / recipe.servings;
      return {
        meal,
        source_recipe: { ...recipe },
        servings: meal.servings,
        ingredients: recipe.ingredients.map((item) => ({
          ...item,
          quantity: String(Number(item.quantity) * scale),
        })),
      };
    }

    if (endpoint.startsWith("/recipes/rec-1/feedback") && method === "POST") {
      const entry = {
        id: `hist-${interactions.length + 1}`,
        kind: "recipe_rating",
        recipe_id: "rec-1",
        channel: "app",
        occurred_at: "2026-11-25T18:00:00Z",
        message: "Feedback recorded",
        structured: body,
      };
      interactions.push(entry);
      return entry;
    }

    if (endpoint === "/recipes/rec-1/stats" && method === "GET") {
      return computeStats("rec-1");
    }

    if (endpoint === "/interactions?limit=100" && method === "GET") {
      return [...interactions].reverse();
    }

    throw new Error(`Unexpected API call: ${method} ${endpoint}`);
  }

  const slices = [
    extractRange("function el(", "\nfunction rememberRecipeCollectionJob("),
    extractRange("function field(", "\nasync function mutate("),
    extractRange("function iso(", "\nfunction day("),
    extractRange("function recipeOriginLabel(", "\nfunction ingredientQuantityText("),
    extractRange("function mealSlotLabel(", "\nfunction httpsUrl("),
    extractRange("function mealForm(", "\nfunction openCookidooPlanSync("),
    extractRange("function recipeForm(", "\nfunction recipeImageCredit("),
    extractRange("function viewRecipe(", "\nfunction removeRecipeImage("),
    extractRange("function discoveryForm(", "\nfunction shoppingOrigins("),
    extractRange("function recentFeedbackPanel(", "\nfunction itemForm("),
  ].join("\n");

  const context = {
    document: doc,
    window: {
      KitchenRecipeDiscovery,
      KitchenRecipeEditor,
      KitchenPlannedCooking,
      KitchenShoppingView,
      KitchenRecipeQuickFeedback,
    },
    URL,
    Date,
    Intl,
    Math,
    Number,
    String,
    Array,
    Map,
    Set,
    setTimeout,
    clearTimeout,
    noticeTimer: null,
    state,
    location: { hash: "" },
    api,
    $: (sel) => {
      if (sel === "#modal") return modal;
      if (sel === "#main") return main;
      if (sel === "#modal-content") return dialogRoot;
      if (sel === "#dialog-title") return dialogRoot.querySelector("#dialog-title");
      if (sel === "#notice") return noticeNode;
      return new DomNode("div");
    },
    openDialog: (heading) => {
      modal.showModal();
      const titleNode = new DomNode("h2");
      titleNode.id = "dialog-title";
      titleNode.textContent = heading;
      dialogRoot.replaceChildren(titleNode);
      return dialogRoot;
    },
    notify: (message) => notices.push(message),
    render: () => {},
    refreshView: async () => {},
    syncPlannerUrl: () => {},
    startOfWeek: () => new Date("2026-11-23T00:00:00Z"),
    parseDate: (v) => (v ? new Date(`${v}T00:00:00Z`) : null),
    localDate: (d) => d,
    dateLabel: (d) => d.toISOString().slice(0, 10),
    formatUiCount: String,
    formatUiTime: String,
    formatServings: (v) => `${v} porsjoner`,
    ingredientQuantityText: (item) => `${item.quantity} ${item.unit} ${item.name}`,
    purchaseEstimateText: () => "",
    recipeImage: () => null,
    recipeImageCredit: () => null,
    recipeAssistantConfigured: () => true,
    connectionSettingsButton: () => new DomNode("button"),
    empty: (heading) => Object.assign(new DomNode("div"), { textContent: heading }),
    unmappedIngredientsDialog: () => {},
    recipeImageSearchForm: () => {},
    removeRecipeImage: () => {},
    viewRecipePhoto: () => {},
    addRecipeForm: () => {},
  };

  vm.runInNewContext(slices, context);

  return {
    context,
    state,
    recipes,
    meals,
    interactions,
    apiCalls,
    notices,
    dialogRoot,
  };
}

test("plannedMealView exposes direct quick feedback and Bokmål 'Gi tilbakemelding' handoff with recipe context", async () => {
  const harness = createJourneyHarness();
  const recipe = {
    id: "rec-1",
    title: "Kremet tomatpasta",
    servings: 2,
    source: "ai",
    ingredients: [{ name: "Pasta", quantity: "200", unit: "g" }],
    instructions: ["Kok pasta."],
    provenance: { original_source: "imported" },
  };
  harness.recipes.push(recipe);

  harness.context.plannedMealView({
    meal: { id: "meal-1", recipe_id: "rec-1", servings: 5 },
    sourceRecipe: { ...recipe },
    servings: 5,
    ingredients: [{ name: "Pasta", quantity: "500", unit: "g" }],
  });

  const widget = harness.dialogRoot.querySelector(".quick-feedback-widget");
  assert.ok(widget, "plannedMealView renders quick-feedback widget");
  const stars = walk(widget).filter((n) => n.classList.contains("quick-feedback-star"));
  assert.equal(stars.length, 5);

  await stars[4].click();
  await flush();
  assert.deepEqual(
    harness.apiCalls.filter((c) => c.method === "POST"),
    [{ endpoint: "/recipes/rec-1/feedback", method: "POST", body: { rating: 5 } }],
  );

  const feedbackButton = walk(harness.dialogRoot).find(
    (n) => n.tagName === "BUTTON" && n.textContent === "Gi tilbakemelding",
  );
  assert.ok(feedbackButton, "plannedMealView exposes Bokmål 'Gi tilbakemelding' button");

  await feedbackButton.click();
  await flush();
  assert.equal(harness.dialogRoot.querySelector("#dialog-title").textContent, "Vurder oppskriften");

  const inputs = walk(harness.dialogRoot).filter((n) =>
    ["INPUT", "SELECT", "TEXTAREA"].includes(n.tagName),
  );
  inputs[0].value = "4";
  inputs[1].value = "true";
  inputs[2].value = "Perfekt til 5 porsjoner";
  await harness.dialogRoot.querySelector("form").submit();
  await flush();

  const postFeedbackCalls = harness.apiCalls.filter(
    (c) => c.method === "POST" && c.endpoint === "/recipes/rec-1/feedback",
  );
  assert.equal(postFeedbackCalls.length, 2);
  assert.deepEqual(
    JSON.parse(JSON.stringify(postFeedbackCalls[1].body)),
    {
      rating: 4,
      would_make_again: true,
      notes: "Perfekt til 5 porsjoner",
    },
  );
  assert.match(harness.dialogRoot.textContent, /Oppskriftsvurdering · Kremet tomatpasta/);
  assert.match(harness.dialogRoot.textContent, /Perfekt til 5 porsjoner/);
});

test("connected frontend journey: find -> adapt -> save -> plan -> cook -> quick & detailed feedback", async () => {
  const harness = createJourneyHarness();

  // 1. FIND: open discovery form and search offline mocked source
  harness.context.discoveryForm();
  await flush();
  const searchInput = walk(harness.dialogRoot).find((n) => n.tagName === "INPUT" && n.type === "search");
  searchInput.value = "tomatpasta";
  await harness.dialogRoot.querySelector("form").submit();
  await flush();
  assert.equal(harness.recipes.length, 0, "discovery must not save recipes");

  // 2. ADAPT: click 'Importer for gjennomgang' -> editable draft without saving yet
  const importBtn = walk(harness.dialogRoot).find(
    (n) => n.tagName === "BUTTON" && n.textContent === "Importer for gjennomgang",
  );
  assert.ok(importBtn);
  await importBtn.click();
  await flush();
  assert.equal(harness.recipes.length, 0, "AI draft requires explicit save confirmation");
  assert.equal(harness.dialogRoot.querySelector("#dialog-title").textContent, "Se over oppskriften");

  // 3. SAVE: confirm adapted recipe
  await harness.dialogRoot.querySelector("form").submit();
  await flush();
  assert.equal(harness.recipes.length, 1);
  assert.equal(harness.recipes[0].source, "ai");
  assert.equal(harness.recipes[0].servings, 2);

  // 4. PLAN: schedule for 5 servings within week 2026-11-23..2026-11-29
  harness.context.mealForm("2026-11-25", harness.recipes[0].id);
  const mealInputs = walk(harness.dialogRoot).filter((n) =>
    ["INPUT", "SELECT"].includes(n.tagName),
  );
  const servingsInput = mealInputs.find((n) => n.type === "number");
  servingsInput.value = "5";
  await harness.dialogRoot.querySelector("form").submit();
  await flush();
  assert.equal(harness.meals.length, 1);
  assert.equal(harness.meals[0].servings, 5);

  // 5. SHOP: verify period-scoped scaled quantities without auto-buying
  const inPeriodShopping = await harness.context.api(
    "/shopping?start=2026-11-23&end=2026-11-29",
  );
  assert.equal(inPeriodShopping.lines.length, 2);
  assert.equal(
    `${KitchenShoppingView.formatNeedQuantity(inPeriodShopping.lines[0].quantity, inPeriodShopping.lines[0].unit, false)} ${inPeriodShopping.lines[0].name}`,
    "500 g Pasta",
  );
  assert.equal(
    `${KitchenShoppingView.formatNeedQuantity(inPeriodShopping.lines[1].quantity, inPeriodShopping.lines[1].unit, false)} ${inPeriodShopping.lines[1].name}`,
    "250 ml Fløte",
  );
  const outPeriodShopping = await harness.context.api(
    "/shopping?start=2026-12-01&end=2026-12-07",
  );
  assert.equal(outPeriodShopping.lines.length, 0);

  // 6. COOK: open planned cooking view and verify scaling + source provenance
  await harness.context.openPlannedMeal(harness.meals[0]);
  await flush();
  assert.match(
    harness.dialogRoot.textContent,
    /Til 5 porsjoner · den lagrede oppskriften er for 2/,
  );
  assert.match(harness.dialogRoot.textContent, /Tilpasset fra en importert oppskrift\./);
  assert.match(harness.dialogRoot.textContent, /500 g Pasta/);
  assert.match(harness.dialogRoot.textContent, /250 ml Fløte/);

  // 6. FEEDBACK: quick thumbs-up + 'Gi tilbakemelding' detailed form preserve recipe context
  const thumbUp = harness.dialogRoot.querySelector(".quick-feedback-thumb-up");
  assert.ok(thumbUp);
  await thumbUp.click();
  await flush();
  assert.equal(harness.state.recipes[0].activity_stats.would_make_again, true);

  const feedbackBtn = walk(harness.dialogRoot).find(
    (n) => n.tagName === "BUTTON" && n.textContent === "Gi tilbakemelding",
  );
  await feedbackBtn.click();
  await flush();
  const feedbackInputs = walk(harness.dialogRoot).filter((n) =>
    ["INPUT", "SELECT", "TEXTAREA"].includes(n.tagName),
  );
  feedbackInputs[0].value = "5";
  feedbackInputs[2].value = "Lagret fra matlagingsvisning";
  await harness.dialogRoot.querySelector("form").submit();
  await flush();

  assert.equal(harness.interactions.length, 2);
  assert.equal(harness.interactions[1].recipe_id, "rec-1");
  assert.match(harness.dialogRoot.textContent, /Lagret fra matlagingsvisning/);
});
