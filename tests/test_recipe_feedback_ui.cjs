const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
function sourceFunction(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(match, `app.js should define ${name}`);
  return match[0];
}
// Exercise the actual app renderers and submit handlers without a browser or network.
class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.value = "";
    this.disabled = false;
    this.ownText = "";
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Stored history must never be inserted as HTML"); }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.ownText = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  async click() { if (!this.disabled) await this.listeners.click?.(); }
  async submit() { await this.listeners.submit({ preventDefault() {} }); }
}
const walk = (node) => [node, ...node.children.flatMap(walk)];
const flush = () => new Promise((resolve) => setImmediate(resolve));
const recipe = { id: "soup", title: "Tomatsuppe" };
function entry(kind, message, extra = {}) {
  return { id: "saved-1", kind, message, channel: "app", occurred_at: "2026-11-16T12:00:00Z", structured: {}, ...extra };
}
function harness(respond = () => []) {
  const calls = [];
  const notices = [];
  let root;
  const functions = ["el", "add", "button", "field", "formSubmit", "recentFeedbackPanel", "feedbackForm", "discoveryForm", "suggestWeek"];
  const app = new Function("document", "api", "state", "openDialog", "notify", "dateLabel", "window", "recipeAssistantConfigured", "iso", "day", `
    ${functions.map(sourceFunction).join("\n")}
    return { recentFeedbackPanel, feedbackForm, discoveryForm, suggestWeek };
  `)(
    { createElement: (tag) => new Node(tag) },
    async (endpoint, method = "GET", body) => {
      calls.push({ endpoint, method, body });
      return respond(endpoint, method, body);
    },
    { recipes: [recipe] },
    () => { root = new Node("dialog"); return root; },
    (message) => notices.push(message),
    (date) => date.toISOString(),
    { KitchenRecipeDiscovery: { SOURCES: [{ id: "oda", label: "Oda" }] } },
    () => true,
    (value) => value, () => "2026-11-16",
  );
  return { app, calls, notices, root: () => root };
}
function controls(root) {
  const nodes = walk(root);
  return {
    form: nodes.find((node) => node.tagName === "FORM"),
    inputs: nodes.filter((node) => ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName)),
    status: nodes.find((node) => node.attributes.role === "status"),
    error: nodes.find((node) => node.attributes.role === "alert"),
    reload: nodes.find((node) => node.textContent === "Oppdater lagrede tilbakemeldinger" && node.tagName === "BUTTON"),
    rows: nodes.filter((node) => node.tagName === "LI"),
  };
}

test("recent view reads only saved feedback/preferences and excludes chats and activity", async () => {
  const ui = harness(() => [
    entry("chat", "PRIVATE EXTERNAL CHAT"), entry("purchase", "PURCHASE DATA"),
    entry("planning", "PLANNING DATA"), entry("domain_event", "COOKING DATA"),
    entry("preference", "Unngå koriander"), entry("feedback", "Mindre salt neste gang"),
  ]);
  const { node } = ui.app.recentFeedbackPanel();
  await flush();
  assert.deepEqual(ui.calls, [{ endpoint: "/interactions?limit=100", method: "GET", body: undefined }]);
  assert.equal(controls(node).rows.length, 2);
  assert.match(node.textContent, /Unngå koriander/);
  assert.match(node.textContent, /Lagret fra appen · 2026-11-16/);
  assert.doesNotMatch(node.textContent, /PRIVATE|PURCHASE DATA|PLANNING DATA|COOKING DATA/);
  assert.match(node.textContent, /kan gi kontekst til KI-forslag/);
  assert.match(node.textContent, /bekrefter ikke at et bestemt forslag brukte opplysningene/);
  assert.match(node.textContent, /opptil 10.*siste 100/);
});

test("typed ratings retain explicit false, notes and recipe identity with Bokmål labels", async () => {
  const ui = harness(() => [entry("recipe_rating", "Generated English summary", {
    recipe_id: "soup", channel: "mcp", structured: { rating: 2, would_make_again: false, notes: "For salt" },
  })]);
  const { node } = ui.app.recentFeedbackPanel();
  await flush();
  assert.match(node.textContent, /Oppskriftsvurdering · Tomatsuppe/);
  assert.match(node.textContent, /Vurdering: 2 av 5/);
  assert.match(node.textContent, /Vil lage igjen: Nei/);
  assert.match(node.textContent, /For salt/);
  assert.match(node.textContent, /Lagret fra MCP/);
  assert.doesNotMatch(node.textContent, /Generated English summary/);
});

test("freeform ratings and removed recipes remain readable without rendering HTML", async () => {
  const message = '<img src=x onerror="alert(1)">';
  const ui = harness(() => [entry("recipe_rating", message, { recipe_id: "removed", occurred_at: "invalid" })]);
  const { node } = ui.app.recentFeedbackPanel();
  await flush();
  assert.match(node.textContent, /Tidligere oppskrift/);
  assert.ok(node.textContent.includes(message));
  assert.doesNotMatch(node.textContent, /Invalid Date/);
});

test("recent view bounds displayed entries and does not treat unrelated history as feedback", async () => {
  const ui = harness(() => Array.from({ length: 15 }, (_, i) => entry("feedback", `Notat ${i}`)));
  const { node } = ui.app.recentFeedbackPanel();
  await flush();
  assert.equal(controls(node).rows.length, 10);
  assert.match(node.textContent, /Notat 9/);
  assert.doesNotMatch(node.textContent, /Notat 10/);
  const empty = harness(() => [entry("chat", "private")]);
  const panel = empty.app.recentFeedbackPanel();
  await flush();
  assert.match(panel.node.textContent, /Ingen tilbakemeldinger eller matpreferanser blant de siste 100/);
});

test("failed history refresh preserves prior entries and allows retry", async () => {
  let attempts = 0;
  const ui = harness(() => {
    if (++attempts === 2) throw new Error("offline");
    return [entry("preference", attempts === 1 ? "Gammelt notat" : "Nytt notat")];
  });
  const panel = ui.app.recentFeedbackPanel();
  await flush();
  await panel.refresh();
  assert.match(panel.node.textContent, /Gammelt notat/);
  assert.match(controls(panel.node).status.textContent, /kan være utdaterte/);
  await controls(panel.node).reload.click();
  assert.match(panel.node.textContent, /Nytt notat/);
  assert.doesNotMatch(panel.node.textContent, /Gammelt notat/);
});

test("malformed history is an unavailable state rather than an empty success", async () => {
  const ui = harness(() => ({ entries: [] }));
  const { node } = ui.app.recentFeedbackPanel();
  await flush();
  assert.match(controls(node).status.textContent, /Kunne ikke hente/);
  assert.equal(controls(node).reload.disabled, false);
});

test("older history responses cannot overwrite a newer explicit refresh", async () => {
  let finishOld;
  let reads = 0;
  const ui = harness(() => ++reads === 1 ? new Promise((resolve) => { finishOld = resolve; }) : [entry("preference", "Nyeste")]);
  const panel = ui.app.recentFeedbackPanel();
  await panel.refresh();
  finishOld([entry("preference", "Foreldet")]);
  await flush();
  assert.match(panel.node.textContent, /Nyeste/);
  assert.doesNotMatch(panel.node.textContent, /Foreldet/);
});

test("recipe feedback form is localized and refreshes saved history after explicit submit", async () => {
  let saved = false;
  const ui = harness((endpoint, method) => {
    if (method === "POST") { saved = true; return {}; }
    return saved ? [entry("recipe_rating", "Saved", { recipe_id: "soup", structured: { rating: 4, would_make_again: false, notes: "God suppe" } })] : [];
  });
  ui.app.feedbackForm(recipe);
  await flush();
  const root = ui.root();
  const { inputs, form } = controls(root);
  assert.match(root.textContent, /Vurdering \(valgfritt\)/);
  assert.match(root.textContent, /Vil dere lage dette igjen\? \(valgfritt\)/);
  assert.match(root.textContent, /Ikke besvartJaNei/);
  assert.doesNotMatch(root.textContent, /Rating|Would cook|Notes|Your rating|No answer/);
  inputs[0].value = "4";
  inputs[1].value = "false";
  inputs[2].value = "  God suppe  ";
  await form.submit();
  assert.deepEqual(ui.calls.map(({ endpoint, method }) => [endpoint, method]), [
    ["/interactions?limit=100", "GET"], ["/recipes/soup/feedback", "POST"], ["/interactions?limit=100", "GET"],
  ]);
  assert.deepEqual(ui.calls[1].body, { rating: 4, would_make_again: false, notes: "God suppe" });
  assert.match(root.textContent, /Vurdering: 4 av 5/);
  assert.match(root.textContent, /Vil lage igjen: Nei/);
  assert.ok(inputs.every((input) => input.value === ""));
  assert.equal(ui.notices.length, 1);
});

test("explicit preference save refreshes history while failed save preserves input for retry", async () => {
  let fail = true;
  const ui = harness((endpoint, method) => {
    if (method === "POST" && fail) throw new Error("Lagringen feilet");
    return [];
  });
  ui.app.feedbackForm();
  await flush();
  const { form, inputs, error } = controls(ui.root());
  inputs[0].value = "preference";
  inputs[1].value = "  Mer grønnsaker  ";
  await form.submit();
  assert.equal(inputs[1].value, "  Mer grønnsaker  ");
  assert.match(error.textContent, /Lagringen feilet/);
  assert.equal(ui.calls.length, 2);
  fail = false;
  await form.submit();
  assert.deepEqual(ui.calls[2].body, { channel: "app", kind: "preference", message: "Mer grønnsaker", structured: {} });
  assert.equal(ui.calls[3].endpoint, "/interactions?limit=100");
  assert.equal(inputs[1].value, "");
  assert.equal(error.textContent, "");
});

test("a refresh failure after saving does not claim the explicit save failed", async () => {
  let saved = false;
  const ui = harness((endpoint, method) => {
    if (method === "POST") { saved = true; return {}; }
    if (saved) throw new Error("offline");
    return [];
  });
  ui.app.feedbackForm(recipe);
  await flush();
  const { form, inputs, error, status } = controls(ui.root());
  inputs[2].value = "Rask middag";
  await form.submit();
  assert.equal(error.textContent, "");
  assert.match(status.textContent, /Kunne ikke hente/);
  assert.equal(ui.notices.length, 1);
  assert.equal(inputs[2].value, "");
});

test("discovery displays saved context without automatically searching providers", async () => {
  const ui = harness(() => [entry("preference", "Foretrekker bønner")]);
  ui.app.discoveryForm();
  await flush();
  assert.match(ui.root().textContent, /Foretrekker bønner/);
  assert.deepEqual(ui.calls.map((call) => call.endpoint), ["/interactions?limit=100"]);
});

test("meal-plan review displays recent context independently from AI output", async () => {
  const ui = harness((endpoint) => endpoint === "/ai/plan" ? { meals: [], notes: [] } : [entry("feedback", "Mindre sterk mat")]);
  await ui.app.suggestWeek();
  await flush();
  assert.match(ui.root().textContent, /Mindre sterk mat/);
  assert.match(ui.root().textContent, /Fikk ingen måltidsforslag/);
  assert.deepEqual(ui.calls.map((call) => call.endpoint), ["/interactions?limit=100", "/ai/plan"]);
});
