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
    formatUiNeedQuantity: (need, accessible = false) => shoppingView.formatNeedQuantity(
      need?.quantity, need?.unit, accessible, need?.approximate === true,
    ),
    dateLabel: () => "16. nov.",
    day: () => new Date("2026-11-16T00:00:00"),
    notify: () => {},
    openDialog: () => new Node("dialog"),
    renderShoppingConnectionPrompt: () => {},
    renderBasketMatchSummary: () => new Node("section"),
    renderBasketBatchReview: () => {},
    renderBasketMatchNeed: () => new Node("section"),
    renderPurchaseEvidence: (value) => value.provenance ? new Node("details", "Om mengdegrunnlaget") : null,
    categoryLabel: (value) => value,
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

test("renderInStore displays canonical English categories in Bokmål and uses localized units", () => {
  const inStoreRenderer = source.match(/function renderInStore\(main\) \{[\s\S]*?\n\}(?=\n+function renderTripCompletion\()/u);
  assert.ok(inStoreRenderer, "renderInStore exists");
  const lines = [
    { id: "1", name: "Epler", category: "Produce", to_buy_quantity: "1", unit: "item", checked: false },
    { id: "2", name: "Melk", category: "Dairy", to_buy_quantity: "2", unit: "pack", checked: false },
    { id: "3", name: "Brød", category: "Bakery", to_buy_quantity: "500", unit: "g", checked: false },
  ];
  const state = {
    storeTrip: { lines, checked_count: 0, total_count: 3 },
    household: { shopping_category_order: ["Produce", "Bakery", "Dairy"] },
    shoppingWriteInFlight: 0,
  };
  const context = {
    state,
    window: { KitchenShoppingView: shoppingView, KitchenShoppingShare: shareView },
    navigator: {},
    document: {},
    categoryLabel: (cat) => shoppingView.categoryLabel(cat),
    formatUiAccessibleQuantity: (v) => shoppingView.formatQuantityForAccessibility(v),
    shoppingOrigins: () => "",
    dateLabel: () => "",
    day: () => new Date(),
    el: (tag, text, cls) => new Node(tag, text, cls),
    add: (parent, ...children) => { children.filter(Boolean).forEach((c) => parent.append(c)); return parent; },
    button: (text) => new Node("button", text),
    openDialog: () => new Node("dialog"),
    notify: () => {},
    api: async () => {},
    range: () => "",
    refreshView: async () => {},
    queuePendingShoppingRefresh: () => {},
  };
  vm.runInNewContext(inStoreRenderer[0] + "\nglobalThis.renderInStore = renderInStore;", context);
  const main = new Node("main");
  context.renderInStore(main);
  const sections = main.children.filter((n) => n.tagName === "section");
  const headings = sections.map((s) => s.children[0].textContent);
  assert.deepEqual(headings, ["Frukt og grønt", "Bakeri", "Meieri"]);

  // Produce line (1 item)
  const produceRow = sections[0].children[1].children[0];
  const produceSmall = produceRow.children[1].children[1];
  assert.equal(produceSmall.textContent, "1 stk. å plukke");
  assert.equal(produceSmall.attributes["aria-label"], "Mengde: 1 stykk å plukke");

  // Bakery line (500 g)
  const bakeryRow = sections[1].children[1].children[0];
  const bakerySmall = bakeryRow.children[1].children[1];
  assert.equal(bakerySmall.textContent, "500 g å plukke");
  assert.equal(bakerySmall.attributes["aria-label"], "Mengde: 500 gram å plukke");

  // Dairy line (2 pack)
  const dairyRow = sections[2].children[1].children[0];
  const dairySmall = dairyRow.children[1].children[1];
  assert.equal(dairySmall.textContent, "2 pakker å plukke");
  assert.equal(dairySmall.attributes["aria-label"], "Mengde: 2 pakker å plukke");
});

test("allocation summary formatting uses Norwegian decimal comma and localized units for display and accessibility", () => {
  const allocMatch = source.match(/const allocated = String\(entry\.allocated_quantity[\s\S]*?add\(section, allocation\);/u);
  assert.ok(allocMatch, "allocation snippet exists");

  // Test with decimal pack quantities
  const contextPack = {
    window: { KitchenShoppingView: shoppingView },
    entry: { allocated_quantity: "1.5", remaining_quantity: "0", overbuy_quantity: "0.5" },
    need: { unit: "pack" },
    el: (tag, text) => new Node(tag, text),
    section: new Node("section"),
    add: (p, c) => { p.children.push(c); return p; },
  };
  vm.runInNewContext(allocMatch[0], contextPack);
  const allocPack = contextPack.section.children[0];
  assert.equal(
    allocPack.textContent,
    "Dekket av valgte pakker: 1,5 pakker · Gjenstår: 0 pakker · Til overs fra valgte pakker: 0,5 pakker",
  );
  assert.equal(
    allocPack.attributes["aria-label"],
    "Nøyaktige mengder: dekket av valgte pakker 1,5 pakker; gjenstår 0 pakker; til overs fra valgte pakker 0,5 pakker",
  );

  // Test with item units
  const contextItem = {
    window: { KitchenShoppingView: shoppingView },
    entry: { allocated_quantity: "1", remaining_quantity: "0", overbuy_quantity: "2" },
    need: { unit: "item" },
    el: (tag, text) => new Node(tag, text),
    section: new Node("section"),
    add: (p, c) => { p.children.push(c); return p; },
  };
  vm.runInNewContext(allocMatch[0], contextItem);
  const allocItem = contextItem.section.children[0];
  assert.equal(
    allocItem.textContent,
    "Dekket av valgte pakker: 1 stk. · Gjenstår: 0 stk. · Til overs fra valgte pakker: 2 stk.",
  );
  assert.equal(
    allocItem.attributes["aria-label"],
    "Nøyaktige mengder: dekket av valgte pakker 1 stykk; gjenstår 0 stykk; til overs fra valgte pakker 2 stykk",
  );
});

test("Oda proposal confirmation and package count modal use clear Bokmål copy and fallback", () => {
  assert.match(
    source,
    /Bekreftelse legger bare til pakkeantallene ovenfor\. Ingen bestilling eller betaling blir gjennomført\./,
  );
  assert.match(
    source,
    /el\("h3", product\.name \|\| "Oda-produkt"\)/,
  );
  assert.match(
    source,
    /Oppgi hvor mange [\s\S]*?det er i én pakke/,
  );
});
