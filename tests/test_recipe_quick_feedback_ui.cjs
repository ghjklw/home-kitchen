"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  formatRatingNumber,
  formatRecipeStatsBadges,
  renderQuickFeedbackBar,
} = require("../src/home_kitchen/static/recipe-feedback-quick.js");
const KitchenRecipeQuickFeedback = {
  formatRatingNumber,
  formatRecipeStatsBadges,
  renderQuickFeedbackBar,
};

class DomElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.hidden = false;
    this.type = "";
    this.value = "";
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
    this._textContent = String(value);
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  replaceChildren(...nodes) {
    this._textContent = "";
    this.children = [...nodes];
  }

  querySelectorAll(selector) {
    const cls = selector.startsWith(".") ? selector.slice(1) : null;
    return walkNodes(this).filter((n) => (cls ? hasClass(n, cls) : n.tagName === selector.toUpperCase()));
  }

  addEventListener(eventName, handler) {
    this.listeners[eventName] = handler;
  }

  async click() {
    if (this.disabled) return;
    if (typeof this.listeners.click === "function") {
      await this.listeners.click();
    }
  }
}

function createFakeDocument() {
  return {
    createElement(tagName) {
      return new DomElement(tagName);
    },
  };
}

test("formatRatingNumber formats integers and Norwegian decimal ratings", () => {
  assert.equal(formatRatingNumber(4), "4");
  assert.equal(formatRatingNumber(4.5), "4,5");
  assert.equal(formatRatingNumber(4.36), "4,4");
  assert.equal(formatRatingNumber("not-a-number"), "");
});

test("formatRecipeStatsBadges builds localized Option B activity and feedback badges", () => {
  assert.deepEqual(formatRecipeStatsBadges(null), []);
  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 0,
      times_cooked: 0,
      times_ordered: 0,
      latest_rating: null,
      average_rating: null,
      would_make_again: null,
      feedback_count: 0,
    }),
    [],
  );

  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 1,
      times_cooked: 2,
      times_ordered: 3,
      average_rating: 4.5,
      latest_rating: 5,
      would_make_again: true,
      feedback_count: 4,
    }),
    [
      "Planlagt 1 gang",
      "Tilberedt 2 ganger",
      "Bestilt 3 ganger",
      "4,5 ★ (4)",
      "👍 Anbefalt",
    ],
  );

  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 2,
      times_cooked: 1,
      times_ordered: 1,
      average_rating: null,
      latest_rating: 3,
      would_make_again: false,
      feedback_count: 1,
    }),
    [
      "Planlagt 2 ganger",
      "Tilberedt 1 gang",
      "Bestilt 1 gang",
      "3 ★ (1)",
      "👎 Ikke anbefalt",
    ],
  );
});

test("renderQuickFeedbackBar validates required recipe ID and document", () => {
  const doc = createFakeDocument();
  assert.throws(() => renderQuickFeedbackBar({}, null, { document: doc }), /Oppskrift med ID/);
  assert.throws(() => renderQuickFeedbackBar({ id: "r1" }, null, {}), /DOM document/);
});

test("1-click star and thumbs buttons submit Option B feedback and update aria-pressed", async () => {
  const doc = createFakeDocument();
  const apiCalls = [];
  const submitted = [];

  const bar = renderQuickFeedbackBar(
    {
      id: "recipe-1",
      activity_stats: { latest_rating: 3, would_make_again: null },
    },
    (result, payload) => submitted.push({ result, payload }),
    {
      document: doc,
      api: async (endpoint, method, payload) => {
        apiCalls.push({ endpoint, method, payload });
        return { id: `entry-${apiCalls.length}`, structured: payload };
      },
    },
  );

  assert.equal(bar.starButtons.length, 5);
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "true");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");

  // 1-click 5-star rating
  await bar.starButtons[4].click();
  assert.deepEqual(apiCalls[0], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { rating: 5 },
  });
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "true");
  assert.equal(bar.statusElement.textContent, "Vurdering lagret: 5 av 5 stjerner");
  assert.equal(bar.errorElement.hidden, true);

  // 1-click thumbs up
  await bar.thumbUpButton.click();
  assert.deepEqual(apiCalls[1], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { would_make_again: true },
  });
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.statusElement.textContent, "Anbefaling lagret: Tommel opp");

  // 1-click thumbs down
  await bar.thumbDownButton.click();
  assert.deepEqual(apiCalls[2], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { would_make_again: false },
  });
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.statusElement.textContent, "Anbefaling lagret: Tommel ned");
  assert.equal(submitted.length, 3);
});

test("quick feedback bar locks controls while busy and recovers cleanly after failed save", async () => {
  const doc = createFakeDocument();
  let resolvePending;
  let shouldFail = false;
  const fetchCalls = [];

  const bar = renderQuickFeedbackBar(
    {
      id: "recipe 2",
      stats: { latest_rating: 2, would_make_again: true },
    },
    null,
    {
      document: doc,
      fetch: async (url, init) => {
        fetchCalls.push({ url, init });
        if (shouldFail) {
          return {
            ok: false,
            status: 500,
            text: async () => "server error",
          };
        }
        return new Promise((resolve) => {
          resolvePending = () =>
            resolve({
              ok: true,
              json: async () => ({ saved: true }),
            });
        });
      },
    },
  );

  // Start async click and verify buttons are disabled while in-flight
  const firstClick = bar.starButtons[3].click();
  assert.equal(bar.starButtons.every((btn) => btn.disabled), true);
  assert.equal(bar.thumbUpButton.disabled, true);
  assert.equal(bar.thumbDownButton.disabled, true);

  // Second click while busy should be ignored
  await bar.thumbDownButton.click();
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/recipes/recipe%202/feedback");

  resolvePending();
  await firstClick;
  assert.equal(bar.starButtons.every((btn) => !btn.disabled), true);
  assert.equal(bar.starButtons[3].getAttribute("aria-pressed"), "true");

  // Failed thumb click preserves prior state and shows alert
  shouldFail = true;
  await bar.thumbDownButton.click();
  assert.equal(bar.errorElement.hidden, false);
  assert.equal(bar.errorElement.textContent, "Kunne ikke lagre anbefaling. Prøv igjen.");
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.disabled, false);

  // Failed star click shows rating error
  await bar.starButtons[0].click();
  assert.equal(bar.errorElement.hidden, false);
  assert.equal(bar.errorElement.textContent, "Kunne ikke lagre vurdering. Prøv igjen.");
  assert.equal(bar.starButtons[0].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[3].getAttribute("aria-pressed"), "true");
});

function walkNodes(node, acc = []) {
  acc.push(node);
  for (const child of node.children || []) {
    walkNodes(child, acc);
  }
  return acc;
}

function hasClass(node, className) {
  return String(node.className || "").split(/\s+/).includes(className);
}

function extractRange(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `Located ${startMarker}`);
  return source.slice(start, end);
}

function buildAppQuickFeedbackHarness({ recipes = [], fetchImpl } = {}) {
  const appSource = fs.readFileSync(
    path.join(__dirname, "../src/home_kitchen/static/app.js"),
    "utf8",
  );
  const helpersSource = [
    extractRange(appSource, "function el(", "\nfunction rememberRecipeCollectionJob("),
    extractRange(appSource, "async function api(", "\nfunction integrationById("),
    extractRange(appSource, "function formatRecipeBadge(", "\nfunction day("),
    extractRange(appSource, "function renderRecipes(", "\nfunction recipeForm("),
    extractRange(appSource, "function viewRecipe(", "\nfunction removeRecipeImage("),
  ].join("\n");

  const doc = createFakeDocument();
  const main = doc.createElement("main");
  const dialogRoot = doc.createElement("div");
  const modal = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  const state = {
    view: "recipes",
    household: { language: "nb" },
    recipes,
    recipeCollectionJob: null,
    recipeCollectionStatusNode: null,
  };
  let renderCalls = 0;

  const context = {
    document: doc,
    window: { KitchenRecipeQuickFeedback },
    URL,
    fetch: fetchImpl,
    state,
    $: (sel) => (sel === "#modal" ? modal : sel === "#main" ? main : dialogRoot),
    openDialog: (headingText) => {
      modal.showModal();
      dialogRoot.replaceChildren(Object.assign(doc.createElement("h2"), { textContent: headingText }));
      return dialogRoot;
    },
    title: (heading) => Object.assign(doc.createElement("h1"), { textContent: heading }),
    empty: (heading) => Object.assign(doc.createElement("div"), { textContent: heading }),
    recipeAssistantConfigured: () => true,
    connectionSettingsButton: () => doc.createElement("button"),
    updateRecipeCollectionStatus: () => {},
    unmappedIngredientsDialog: () => {},
    addRecipeForm: () => {},
    mealForm: () => {},
    recipeForm: () => {},
    recipeImage: () => null,
    recipeImageCredit: () => null,
    recipeImageSearchForm: () => {},
    removeRecipeImage: () => {},
    feedbackForm: () => {},
    viewRecipePhoto: () => {},
    purchaseEstimateText: () => "",
    ingredientQuantityText: (item) => item.name,
    formatUiCount: String,
    formatServings: (v) => `${v} porsjoner`,
    render: () => {
      renderCalls += 1;
      context.renderRecipes(main);
    },
  };

  vm.runInNewContext(helpersSource, context);
  return {
    context,
    state,
    main,
    dialogRoot,
    getRenderCalls: () => renderCalls,
  };
}

test("index.html loads recipe-feedback-quick.js before app.js and style.css defines quick feedback styles", () => {
  const html = fs.readFileSync(
    path.join(__dirname, "../src/home_kitchen/static/index.html"),
    "utf8",
  );
  const css = fs.readFileSync(
    path.join(__dirname, "../src/home_kitchen/static/style.css"),
    "utf8",
  );
  const quickIdx = html.indexOf('<script defer src="/static/recipe-feedback-quick.js"></script>');
  const appIdx = html.indexOf('<script defer src="/static/app.js"></script>');
  assert.ok(quickIdx >= 0, "index.html should load recipe-feedback-quick.js");
  assert.ok(appIdx > quickIdx, "recipe-feedback-quick.js should be loaded before app.js");

  assert.match(css, /\.recipe-stats-badges\s*\{/);
  assert.match(css, /\.quick-feedback-widget\s*\{/);
  assert.match(css, /\.quick-feedback-star\b/);
  assert.match(css, /\.quick-feedback-thumb\b/);
  assert.match(css, /\.quick-feedback-star\[aria-pressed="true"\]/);
  assert.match(css, /\.quick-feedback-thumb-up\[aria-pressed="true"\]/);
  assert.match(css, /\.quick-feedback-star:focus-visible,\s*\.quick-feedback-thumb:focus-visible/);
});

test("app.js loads /recipes?include_stats=true for the main list and renders stats badges in renderRecipes", async () => {
  const appSource = fs.readFileSync(
    path.join(__dirname, "../src/home_kitchen/static/app.js"),
    "utf8",
  );
  assert.match(appSource, /api\("\/recipes\?include_stats=true"\)/);

  const recipes = [
    {
      id: "r1",
      title: "Fiskegrateng",
      servings: 4,
      source: "manual",
      ingredients: [{ name: "torsk" }],
      instructions: ["Stek"],
      activity_stats: {
        times_planned: 2,
        times_cooked: 1,
        times_ordered: 3,
        average_rating: 4.5,
        latest_rating: 5,
        would_make_again: true,
        feedback_count: 4,
      },
    },
    {
      id: "r2",
      title: "Ny oppskrift",
      servings: 2,
      source: "manual",
      ingredients: [{ name: "ris" }],
      instructions: ["Kok"],
    },
  ];
  const harness = buildAppQuickFeedbackHarness({ recipes });
  harness.context.renderRecipes(harness.main);

  const cards = walkNodes(harness.main).filter((n) => hasClass(n, "recipe-card"));
  assert.equal(cards.length, 2);

  const firstStatsWrap = walkNodes(cards[0]).find((n) => hasClass(n, "recipe-stats-badges"));
  assert.ok(firstStatsWrap, "card with stats renders .recipe-stats-badges");
  const badgeTexts = walkNodes(firstStatsWrap)
    .filter((n) => hasClass(n, "recipe-stat-badge"))
    .map((n) => n.textContent);
  assert.deepEqual(badgeTexts, [
    "Planlagt 2 ganger",
    "Tilberedt 1 gang",
    "Bestilt 3 ganger",
    "4,5 ★ (4)",
    "👍 Anbefalt",
  ]);

  const secondStatsWrap = walkNodes(cards[1]).find((n) => hasClass(n, "recipe-stats-badges"));
  assert.equal(secondStatsWrap, undefined);
});

test("app.js viewRecipe renders stats badges and quick feedback widget, submits feedback, and updates stats + state.recipes", async () => {
  const fetchCalls = [];
  let serverStats = {
    times_planned: 1,
    times_cooked: 1,
    times_ordered: 0,
    latest_rating: 3,
    average_rating: 3,
    would_make_again: null,
    feedback_count: 1,
  };

  const recipes = [
    {
      id: "rec 1",
      title: "Tomatsuppe",
      servings: 2,
      source: "manual",
      ingredients: [{ name: "tomat" }],
      instructions: ["Kok"],
      activity_stats: { ...serverStats },
      stats: { ...serverStats },
    },
  ];

  const harness = buildAppQuickFeedbackHarness({
    recipes,
    fetchImpl: async (url, init = {}) => {
      fetchCalls.push({ url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null });
      if (url === "/api/recipes/rec%201/feedback" && init.method === "POST") {
        const body = JSON.parse(init.body);
        serverStats = {
          ...serverStats,
          ...(body.rating != null ? { latest_rating: body.rating, average_rating: 4 } : {}),
          ...(body.would_make_again != null ? { would_make_again: body.would_make_again } : {}),
          feedback_count: serverStats.feedback_count + 1,
        };
        return {
          ok: true,
          status: 201,
          json: async () => ({ id: "hist-1", kind: "recipe_rating", structured: body }),
        };
      }
      if (url === "/api/recipes/rec%201/stats" && (!init.method || init.method === "GET")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...serverStats }),
        };
      }
      throw new Error(`Unexpected fetch: ${init.method || "GET"} ${url}`);
    },
  });

  harness.context.renderRecipes(harness.main);
  harness.context.viewRecipe(recipes[0]);

  const widget = walkNodes(harness.dialogRoot).find((n) => hasClass(n, "quick-feedback-widget"));
  assert.ok(widget, "viewRecipe renders .quick-feedback-widget");

  const stars = walkNodes(widget).filter((n) => hasClass(n, "quick-feedback-star"));
  const thumbUp = walkNodes(widget).find((n) => hasClass(n, "quick-feedback-thumb-up"));
  assert.equal(stars.length, 5);
  assert.equal(stars[2].getAttribute("aria-pressed"), "true");
  assert.ok(thumbUp);

  // Click 5 stars
  await stars[4].click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(fetchCalls[0], {
    url: "/api/recipes/rec%201/feedback",
    method: "POST",
    body: { rating: 5 },
  });
  assert.deepEqual(fetchCalls[1], {
    url: "/api/recipes/rec%201/stats",
    method: "GET",
    body: null,
  });

  // Displayed badges in modal and state.recipes updated
  const modalBadgesAfterStar = walkNodes(harness.dialogRoot)
    .filter((n) => hasClass(n, "recipe-stat-badge"))
    .map((n) => n.textContent);
  assert.deepEqual(modalBadgesAfterStar, [
    "Planlagt 1 gang",
    "Tilberedt 1 gang",
    "4 ★ (2)",
  ]);
  assert.equal(harness.state.recipes[0].activity_stats.latest_rating, 5);
  assert.equal(harness.state.recipes[0].activity_stats.feedback_count, 2);
  assert.ok(harness.getRenderCalls() >= 1);

  // Click thumbs up
  await thumbUp.click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(fetchCalls[2], {
    url: "/api/recipes/rec%201/feedback",
    method: "POST",
    body: { would_make_again: true },
  });
  const modalBadgesAfterThumb = walkNodes(harness.dialogRoot)
    .filter((n) => hasClass(n, "recipe-stat-badge"))
    .map((n) => n.textContent);
  assert.ok(modalBadgesAfterThumb.includes("👍 Anbefalt"));
  assert.equal(harness.state.recipes[0].stats.would_make_again, true);

  // Main list cards also reflect updated stats
  const mainCardBadges = walkNodes(harness.main)
    .filter((n) => hasClass(n, "recipe-stat-badge"))
    .map((n) => n.textContent);
  assert.ok(mainCardBadges.includes("👍 Anbefalt"));
  assert.ok(mainCardBadges.includes("4 ★ (3)"));
});
