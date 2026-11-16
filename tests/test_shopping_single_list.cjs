"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const shoppingView = require("../src/home_kitchen/static/shopping-view.js");
const basketView = require("../src/home_kitchen/static/basket-match-view.js");
const shareView = require("../src/home_kitchen/static/shopping-share.js");

const source = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
const screen = source.match(/function renderSingleShoppingList\(main\) \{[\s\S]*?\n\}(?=\nfunction renderShopping\()/u);
assert.ok(screen, "the unified shopping screen renderer exists");

class Node {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.classList = { add: (value) => { this.className += ` ${value}`; } };
  }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  get text() { return this.textContent + this.children.map((child) => child.text).join(" "); }
}

function renderFixture(overrides = {}) {
  const requests = [];
  let refreshes = 0;
  const line = {
    id: "need-123",
    name: "Melk",
    unit: "ml",
    category: "Meieri",
    required_quantity: "1000",
    available_quantity: "0",
    covered_quantity: "0",
    to_buy_quantity: "1000",
    checked: false,
    approximate: true,
    provenance: "Fra måltidsplan",
    source_url: "https://example.test/recipe",
    origins: ["meal:meal-1", "meal:meal-2"],
  };
  const state = {
    storeTrip: { lines: [line], checked_count: 0, total_count: 1, unquantified: [] },
    household: { shopping_category_order: ["Meieri"] },
    shoppingGrouping: "meal",
    basket: { needs: [] },
    oda: { configured: false, connected: false },
    meals: [{ id: "meal-1", recipe_id: "recipe-1" }, { id: "meal-2", recipe_id: "recipe-2" }],
    recipes: [{ id: "recipe-1", title: "Grøt" }, { id: "recipe-2", title: "Pannekaker" }],
    shoppingWriteInFlight: 0,
  };
  const context = {
    state,
    window: { KitchenShoppingView: shoppingView, KitchenBasketMatchView: basketView, KitchenShoppingShare: shareView },
    navigator: {},
    document: {},
    el: (tag, text, className) => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => { const node = new Node("button", text, className); node.action = action; return node; },
    formatUiCount: String,
    dateLabel: () => "16. nov.",
    day: () => new Date("2026-11-16T00:00:00"),
    notify: () => {},
    openDialog: () => new Node("dialog"),
    renderShoppingConnectionPrompt: () => {},
    renderBasketMatchSummary: () => new Node("section"),
    renderBasketBatchReview: () => {},
    renderBasketMatchNeed: () => new Node("section"),
    renderPurchaseEvidence: (value) => value.provenance ? new Node("details", "Om mengdegrunnlaget") : null,
    categoryLabel: shoppingView.categoryLabel,
    odaSearchForNeed: () => new Node("div"),
    basketProduct: () => new Node("article"),
    odaCartProposalRequest: () => ({ additions: [], issue: null }),
    api: async (...args) => { requests.push(args); return null; },
    range: () => "?start=2026-11-16&end=2026-11-22",
    refreshView: async () => { refreshes += 1; },
    queuePendingShoppingRefresh: () => {},
  };
  Object.assign(state, overrides);
  vm.runInNewContext(screen[0] + "\nglobalThis.renderSingleShoppingList = renderSingleShoppingList;", context);
  const main = new Node("main");
  context.renderSingleShoppingList(main);
  return { context, line, main, requests, refreshes: () => refreshes };
}

test("renders one checked net-quantity row for a need shared by multiple recipes", () => {
  const { main } = renderFixture();
  const groups = main.children.filter((node) => node.className.includes("actionable-shopping-group"));
  assert.equal(groups.length, 1);
  assert.equal(groups[0].children[1].children.length, 1);
  const row = groups[0].children[1].children[0];
  assert.equal(row.children[0].type, "checkbox");
  assert.equal(row.children[0].checked, false);
  assert.match(row.text, /ca\. 1000 ml å kjøpe/u);
  assert.match(row.text, /Til: Grøt · Pannekaker/u);
  assert.match(row.text, /Om mengdegrunnlaget/u);
});

test("renders rounded Bokmål quantities in visible and accessible shopping row text", () => {
  const { main, line } = renderFixture();
  line.to_buy_quantity = "233.3333333333333333333333334";
  line.unit = "item";
  line.category = "Produce";
  const rerendered = renderFixture({ shoppingGrouping: "category", storeTrip: { lines: [line], checked_count: 0, total_count: 1, unquantified: [] } });
  const group = rerendered.main.children.find((node) => node.className.includes("actionable-shopping-group"));
  assert.equal(group.children[0].textContent, "Frukt og grønt");
  const row = group.children[1].children[0];
  const summary = row.children[1].children.find((node) => node.tagName === "small");
  assert.match(summary.text, /233,33 stk\. å kjøpe/u);
  assert.equal(summary.attributes["aria-label"], "Omtrentlig mengde: ca. 233,33 stykk å kjøpe");
  assert.doesNotMatch(summary.attributes["aria-label"], /333333|item/u);
});

test("preserves custom category text in the rendered heading", () => {
  const { main, line } = renderFixture();
  line.category = "Min egen kategori";
  const fixture = renderFixture({ shoppingGrouping: "category", storeTrip: { lines: [line], checked_count: 0, total_count: 1, unquantified: [] } });
  const group = fixture.main.children.find((node) => node.className.includes("actionable-shopping-group"));
  assert.equal(group.children[0].textContent, "Min egen kategori");
});

test("renders a localized provider failure without exposing raw English detail", () => {
  const renderer = source.match(/function renderBasketMatchNeed\(need\) \{[\s\S]*?\n\}(?=\nfunction renderBasketMatchCandidate\()/u);
  assert.ok(renderer, "the inline product match renderer exists");
  const context = {
    state: {
      basketMatchPages: { "need-1": { 1: { status: "search_failed", detail: "Product search failed or timed out.", candidates: [] } } },
      basketMatchCurrentPage: {}, basketMatchBusy: {}, basketMatchErrors: {}, oda: { connected: true },
    },
    window: { KitchenBasketMatchView: basketView },
    el: (tag, text, className) => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text) => new Node("button", text),
    formatUiCount: String,
    startBasketMatchJob: () => {},
    showBasketMatchPage: () => {},
  };
  vm.runInNewContext(renderer[0] + "\nglobalThis.renderBasketMatchNeed = renderBasketMatchNeed;", context);
  const panel = context.renderBasketMatchNeed({ id: "need-1" });
  assert.match(panel.text, /Produktsøket kunne ikke fullføres/u);
  assert.doesNotMatch(panel.text, /Product search failed|timed out/u);
});

test("checking a rendered line writes only its stable store-trip need ID", async () => {
  const fixture = renderFixture();
  const row = fixture.main.children.find((node) => node.className.includes("actionable-shopping-group")).children[1].children[0];
  const checkbox = row.children[0];
  checkbox.checked = true;
  await checkbox.listeners.change();
  assert.equal(fixture.requests.length, 1);
  assert.equal(fixture.requests[0][0], "/store-trip/items/need-123?start=2026-11-16&end=2026-11-22");
  assert.equal(fixture.requests[0][1], "PUT");
  assert.equal(JSON.stringify(fixture.requests[0][2]), JSON.stringify({ checked: true }));
  assert.equal(fixture.refreshes(), 1);
});

test("keeps the checked list usable and shows the existing match inline when Oda is connected", () => {
  const { main } = renderFixture({
    oda: { configured: true, connected: true },
    basket: { needs: [{ need: { id: "need-123", name: "Melk", unit: "ml" }, selections: [] }] },
  });
  const row = main.children.find((node) => node.className.includes("actionable-shopping-group")).children[1].children[0];
  assert.equal(row.children[0].type, "checkbox");
  assert.ok(row.children.some((child) => child.className.includes("inline-basket-match")));
});
