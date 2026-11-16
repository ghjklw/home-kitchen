const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync("src/home_kitchen/static/app.js", "utf8");
const renderer = source.match(/function shoppingSections\(\) \{[\s\S]*?\n\}(?=\nfunction renderInStore\()/u);
assert.ok(renderer, "the shopping section navigation renderer exists");

class DomNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
  }
  append(child) { this.children.push(child); }
  setAttribute(name, value) { this.attributes[name] = value; }
}

function renderSections(panel) {
  const state = { shoppingPanel: panel, shoppingRefreshInFlight: false };
  let refreshCount = 0;
  const context = {
    state,
    el: (tag, text, className) => new DomNode(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => {
      const node = new DomNode("button", text, className);
      node.listeners.click = action;
      return node;
    },
    shoppingRefreshLabel: () => "Oppdatert",
    refreshShoppingIfSafe: async () => {},
    refreshView: async () => { refreshCount += 1; },
  };
  vm.runInNewContext(renderer[0] + "\nglobalThis.renderShoppingSections = shoppingSections;", context);
  const nav = context.renderShoppingSections();
  const nodes = (node) => [node, ...(node.children || []).flatMap((child) => child.children ? nodes(child) : [child])];
  return { state, nav, nodes: nodes(nav), refreshCount: () => refreshCount };
}

test("the main shopping list exposes a direct trip-registration action", async () => {
  const ui = renderSections("single");
  const action = ui.nav.children.find((node) => node.tagName === "button" && node.textContent === "Registrer handletur");
  assert.ok(action, "trip registration is visible beside the single-list controls");
  assert.equal(action.className, "primary");
  assert.ok(ui.nav.children.some((node) => node.tagName === "details" && node.children.some((child) => child.textContent === "Flere handlelistevalg")));
  await action.listeners.click();
  assert.equal(ui.state.shoppingPanel, "complete_trip");
  assert.equal(ui.refreshCount(), 1);
});

test("legacy navigation does not add a duplicate trip-registration tab", () => {
  const ui = renderSections("in_store");
  const labels = ui.nav.children.filter((node) => node.tagName === "button").map((node) => node.textContent);
  assert.deepEqual(labels, ["Handleliste", "Behov og forslag", "Oda-handlekurv", "Del og plukk", "Hjemme"]);
  assert.match(source, /add\(main, button\("Registrer handletur"/u, "the in-store view keeps one direct trip-registration action");
});
