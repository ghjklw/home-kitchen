const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync("src/home_kitchen/static/app.js", "utf8");
const helperStart = source.indexOf("window.KitchenTripCompletion = {");
const helperEnd = source.indexOf("\n};", helperStart) + 3;
const sandbox = { window: {} };
vm.runInNewContext(source.slice(helperStart, helperEnd), sandbox);
const completion = sandbox.window.KitchenTripCompletion;

test("maps reviewed meal, capture, and regular sources to their matching purchase fields", () => {
  assert.equal(completion.purchaseSourceField("meal"), "close_meal_ids");
  assert.equal(completion.purchaseSourceField("capture"), "close_capture_ids");
  assert.equal(completion.purchaseSourceField("regular"), "regular_ids");
  assert.equal(completion.purchaseSourceField("unknown"), null);
});

test("exact product purchase requires a product from this reviewed line and explicit package count", () => {
  const line = { selected_products: [{ product: { provider: "oda", product_id: "p-1" } }] };
  assert.deepEqual({ ...completion.exactProductPurchase(line, 0, "3") }, {
    provider: "oda", product_id: "p-1", packages: 3,
  });
  assert.equal(completion.exactProductPurchase(line, "", "3"), null);
  assert.equal(completion.exactProductPurchase(line, 1, "3"), null);
  assert.equal(completion.exactProductPurchase(line, 0, ""), null);
  assert.equal(completion.exactProductPurchase(line, 0, "1.5"), null);
  assert.equal(completion.exactProductPurchase(line, 0, "100"), null);
});

test("completion UI uses reviewed endpoints and does not default planned need into actual purchase", () => {
  assert.match(source, /\/store-trip\/completion-review/);
  assert.match(source, /\/store-trip\/completions/);
  assert.match(source, /Planlagt behov: \$\{formatUiQuantity\(line\.to_buy_quantity\)\} \$\{line\.unit\} · kontekst, ikke faktisk kjøp/);
  assert.match(source, /quantity\.placeholder = "Ukjent"/);
  assert.match(source, /reviewed: true/);
  assert.match(source, /crypto\.randomUUID\(\)/);
});

const renderer = source.match(/function renderTripCompletion\(main\) \{[\s\S]*?\n\}(?=\nfunction inventoryForm\()/u);
assert.ok(renderer, "the actual trip-completion renderer exists");

class DomNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.classList = { add: (...names) => { this.className += " " + names.join(" "); } };
  }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  querySelectorAll(selector) {
    const nodes = this.children.flatMap((child) => [child, ...(child.querySelectorAll?.(selector) || [])]);
    return nodes.filter((node) => selector === "input, select" && ["input", "select"].includes(node.tagName));
  }
}
function makeLine(id, name, unit, kind) {
  return {
    id, name, unit, to_buy_quantity: "4", selected_products: [], captures: [],
    source_quantities: [{ kind, source_id: `source-${id}`, quantity: "4", unit }],
  };
}
function renderCompletion({ lines, retry = null, oda = null, api, refreshView = async () => {}, notices = [] }) {
  let idempotencyCounter = 0;
  const state = {
    tripCompletionReview: { review_token: "review-1", start: "2026-11-16", end: "2026-11-22", lines },
    tripCompletionRetry: retry,
    oda,
  };
  const main = new DomNode("main");
  const context = {
    state,
    window: { KitchenTripCompletion: completion },
    crypto: { randomUUID: () => `idempotency-${++idempotencyCounter}` },
    Option: class { constructor(label, value) { this.textContent = label; this.value = value; } },
    el: (tag, text, className) => new DomNode(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className) => {
      const node = new DomNode("button", text, className);
      node.listeners.click = action;
      return node;
    },
    empty: (title, description) => new DomNode("section", `${title} ${description}`),
    formatUiQuantity: String,
    iso: () => "2026-11-16",
    api,
    refreshView,
    notify: (message, error) => notices.push({ message, error }),
  };
  vm.runInNewContext(renderer[0] + "\nglobalThis.renderTripCompletion = renderTripCompletion;", context);
  context.renderTripCompletion(main);
  const nodes = (node) => [node, ...(node.children || []).flatMap((child) => child.children ? nodes(child) : [child])];
  return {
    state,
    main,
    nodes: nodes(main),
    submit: nodes(main).find((node) => node.tagName === "button" && /Lagre registrering|Prøv lagring igjen/u.test(node.textContent)),
    notices,
  };
}
function labeled(ui, label) {
  const node = ui.nodes.find((entry) => entry.attributes?.["aria-label"] === label);
  assert.ok(node, `input ${label} exists`);
  return node;
}

test("exact-product controls require evidence but retained choices remain available offline", async () => {
  const line = makeLine("need-yoghurt", "Yoghurt", "g", "meal");
  const disconnected = renderCompletion({
    lines: [line],
    oda: { connected: false },
    api: async () => ({}),
  });
  const productControls = (ui) => ui.nodes.filter((node) =>
    [
      "Kjøp eksakt produkt for Yoghurt",
      "Velg vist produkt for Yoghurt",
      "Antall pakker kjøpt: Yoghurt",
    ].includes(node.attributes?.["aria-label"]),
  );
  assert.equal(productControls(disconnected).length, 0);
  assert.ok(disconnected.nodes.some((node) =>
    node.textContent === "Oda er ikke tilkoblet. Oppgi faktisk mengde, eller la den stå ukjent.",
  ));

  const noProductMatches = renderCompletion({
    lines: [line],
    oda: { connected: true },
    api: async () => ({}),
  });
  assert.equal(productControls(noProductMatches).length, 0);
  assert.ok(noProductMatches.nodes.some((node) =>
    node.textContent === "Ingen valgte Oda-produkter for denne varen. Oppgi faktisk mengde, eller la den stå ukjent.",
  ));

  const calls = [];
  const offlineWithSelection = renderCompletion({
    lines: [{
      ...line,
      selected_products: [{
        packages: 1,
        product: {
          name: "Test yoghurt",
          provider: "oda",
          product_id: "yoghurt-1",
          pack_quantity: "500",
          pack_unit: "g",
        },
      }],
    }],
    oda: { connected: false },
    api: async (endpoint, method, body) => { calls.push({ endpoint, method, body }); return {}; },
  });
  assert.ok(offlineWithSelection.nodes.some((node) => node.attributes?.["aria-label"] === "Kjøp eksakt produkt for Yoghurt"));
  assert.ok(offlineWithSelection.nodes.some((node) => node.attributes?.["aria-label"] === "Velg vist produkt for Yoghurt"));
  assert.ok(offlineWithSelection.nodes.some((node) => node.attributes?.["aria-label"] === "Antall pakker kjøpt: Yoghurt"));
  assert.ok(offlineWithSelection.nodes.some((node) =>
    node.textContent === "Dette er lagrede produktvalg for varen. Tilgjengeligheten hos Oda er ikke kontrollert nå.",
  ));
  labeled(offlineWithSelection, "Registrer faktisk kjøp: Yoghurt").checked = true;
  labeled(offlineWithSelection, "Kjøp eksakt produkt for Yoghurt").checked = true;
  labeled(offlineWithSelection, "Velg vist produkt for Yoghurt").value = "0";
  labeled(offlineWithSelection, "Antall pakker kjøpt: Yoghurt").value = "2";
  const sourceRow = offlineWithSelection.nodes.find((node) =>
    node.tagName === "label" && node.children.some((child) => child.textContent.includes("Lukk måltidsbehov · 4 g")),
  );
  assert.ok(sourceRow);
  sourceRow.children[0].checked = true;
  await offlineWithSelection.submit.listeners.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].endpoint, "/store-trip/completions");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.purchases[0].product)), {
    provider: "oda", product_id: "yoghurt-1", packages: 2,
  });
});

test("unknown actual quantities stay null for g/ml/item purchases and closures stay separate", async () => {
  const lines = [makeLine("need-g", "Mel", "g", "meal"), makeLine("need-ml", "Melk", "ml", "capture"), makeLine("need-item", "Egg", "item", "regular"), makeLine("need-close", "Salt", "g", "meal")];
  const calls = [];
  const ui = renderCompletion({ lines, api: async (endpoint, method, body) => { calls.push({ endpoint, method, body }); return {}; } });
  for (const line of lines.slice(0, 3)) {
    labeled(ui, `Registrer faktisk kjøp: ${line.name}`).checked = true;
    const sourceLabel = line.source_quantities[0].kind === "meal" ? "måltidsbehov" : line.source_quantities[0].kind === "capture" ? "hurtigtillegg" : "fast vare";
    const sourceRow = ui.nodes.find((node) => node.tagName === "label" && node.children.some((child) => child.textContent.includes(`Lukk ${sourceLabel} · 4 ${line.unit}`)));
    assert.ok(sourceRow);
    sourceRow.children[0].checked = true;
  }
  const closeInput = ui.nodes.filter((node) => node.tagName === "label" && node.children.some((child) => child.textContent.includes("Lukk måltidsbehov · 4 g"))).at(-1)?.children[0];
  assert.ok(closeInput);
  closeInput.checked = true;
  const reason = ui.nodes.filter((node) => node.tagName === "select" && node.children.some((child) => child.value === "unavailable")).at(-1);
  reason.value = "unavailable";
  await ui.submit.listeners.click();
  assert.equal(calls.length, 1, JSON.stringify(ui.notices));
  assert.equal(calls[0].endpoint, "/store-trip/completions");
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.purchases)), [
    { need_id: "need-g", close_meal_ids: ["source-need-g"] },
    { need_id: "need-ml", close_capture_ids: ["source-need-ml"] },
    { need_id: "need-item", regular_ids: ["source-need-item"] },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.source_closures)), [
    { need_id: "need-close", source_kind: "meal", source_id: "source-need-close", resolution: "unavailable" },
  ]);
  for (const purchase of calls[0].body.purchases) {
    assert.equal(Object.hasOwn(purchase, "quantity"), false);
    assert.equal(Object.hasOwn(purchase, "unit"), false);
  }
});

test("a definitive 422 clears the invalid retry payload but preserves editable input", async () => {
  const line = makeLine("need-g", "Mel", "g", "meal");
  const calls = [];
  const ui = renderCompletion({ lines: [line], api: async (endpoint, method, body) => {
    calls.push({ endpoint, method, body });
    if (calls.length === 1) { const error = new Error("Ugyldig mengde"); error.status = 422; throw error; }
    return {};
  } });
  labeled(ui, "Registrer faktisk kjøp: Mel").checked = true;
  labeled(ui, "Faktisk mengde kjøpt: Mel").value = "0";
  labeled(ui, "Enhet for faktisk kjøp: Mel").value = "g";
  await ui.submit.listeners.click();
  assert.equal(ui.state.tripCompletionRetry, null);
  assert.equal(labeled(ui, "Faktisk mengde kjøpt: Mel").value, "0");
  labeled(ui, "Faktisk mengde kjøpt: Mel").value = "10";
  await ui.submit.listeners.click();
  assert.equal(calls[0].body.purchases[0].quantity, "0");
  assert.equal(calls[1].body.purchases[0].quantity, "10");
  assert.notEqual(calls[0].body.idempotency_key, calls[1].body.idempotency_key);
});

test("uncertain save restores cached form values and resends the identical request after refresh", async () => {
  const line = makeLine("need-g", "Mel", "g", "meal");
  const calls = [];
  let attempt = 0;
  const api = async (endpoint, method, body) => {
    calls.push({ endpoint, method, body });
    if (++attempt === 1) throw new Error("Tilkoblingen ble brutt");
    return {};
  };
  const first = renderCompletion({ lines: [line], api });
  labeled(first, "Registrer faktisk kjøp: Mel").checked = true;
  labeled(first, "Faktisk mengde kjøpt: Mel").value = "10";
  labeled(first, "Enhet for faktisk kjøp: Mel").value = "g";
  await first.submit.listeners.click();
  const cached = first.state.tripCompletionRetry;
  assert.ok(cached);
  assert.ok(labeled(first, "Faktisk mengde kjøpt: Mel").disabled);
  const afterRefresh = renderCompletion({ lines: [line], retry: cached, api });
  assert.equal(labeled(afterRefresh, "Faktisk mengde kjøpt: Mel").value, "10");
  assert.equal(labeled(afterRefresh, "Enhet for faktisk kjøp: Mel").value, "g");
  assert.ok(labeled(afterRefresh, "Registrer faktisk kjøp: Mel").checked);
  assert.ok(labeled(afterRefresh, "Faktisk mengde kjøpt: Mel").disabled);
  await afterRefresh.submit.listeners.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body)), JSON.parse(JSON.stringify(calls[0].body)));
});
