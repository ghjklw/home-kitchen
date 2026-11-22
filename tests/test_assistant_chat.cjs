"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const assistant = require("../src/home_kitchen/static/assistant-chat.js");

function text(value) {
  return { nodeType: 3, nodeValue: value };
}

function element(tagName, children = [], attributes = {}) {
  return {
    nodeType: 1,
    tagName: tagName.toUpperCase(),
    childNodes: children,
    hidden: false,
    inert: false,
    isContentEditable: false,
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(attributes, name) ? attributes[name] : null;
    },
  };
}

class MockElement {
  constructor(tagName, textContent = "") {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.childNodes = [];
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.hidden = false;
    this.value = "";
    this.className = "";
    this.textContent = textContent;
  }
  get textContent() { return this.childNodes.map((child) => child.textContent || child.nodeValue || "").join(""); }
  set textContent(value) {
    this.childNodes = [];
    const text = String(value ?? "");
    if (text) this.childNodes.push({ nodeType: 3, nodeValue: text, textContent: text, parentNode: this });
  }
  append(...nodes) { nodes.forEach((node) => { node.parentNode = this; this.childNodes.push(node); }); }
  replaceChildren(...nodes) { this.childNodes = []; this.append(...nodes); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  contains(node) { for (let current = node; current; current = current.parentNode) if (current === this) return true; return false; }
}

function assistantHarness(fetch) {
  const heading = new MockElement("h1", "Ukeplan");
  const main = new MockElement("main");
  main.append(heading);
  main.querySelector = () => heading;
  const elements = {
    "#assistant-toggle": new MockElement("button"),
    "#assistant-panel": new MockElement("section"),
    "#assistant-close": new MockElement("button"),
    "#assistant-messages": new MockElement("div"),
    "#assistant-form": new MockElement("form"),
    "#assistant-input": new MockElement("textarea"),
    "#assistant-send": new MockElement("button"),
    "#assistant-status": new MockElement("p"),
    "#main": main,
  };
  const documentRef = {
    title: "Kjøkken",
    querySelector: (selector) => elements[selector] || null,
    createElement: (tag) => new MockElement(tag),
    addEventListener() {},
    defaultView: { getComputedStyle: () => ({}) },
  };
  const windowRef = {
    document: documentRef,
    location: { hash: "#week", pathname: "/" },
    fetch,
  };
  return { elements, windowRef };
}

function descendants(node) {
  return [node, ...(node.childNodes || []).flatMap(descendants)];
}

function pageFixture() {
  const heading = element("h1", [text("Ukeplan")]);
  const hidden = element("section", [text("Hemmelig intern tekst")], { hidden: "" });
  const ariaHidden = element("div", [text("Skjult assistentinnhold")], { "aria-hidden": "true" });
  const form = element("form", [
    element("label", [text("Passord")]),
    element("input"),
    element("textarea", [text("Ikke send standardverdier")]),
    element("select", [element("option", [text("Privat valg")])]),
  ]);
  const main = element("main", [
    heading,
    element("p", [text("Synlig måltid: suppe")]),
    hidden,
    ariaHidden,
    form,
    element("p", [text("Bearer abcdefghijklmnopqrstuvwxyz0123456789")]),
  ]);
  main.querySelector = (selector) => selector === "h1" ? heading : null;
  return main;
}

test("client context is labeled, bounded, and excludes hidden and form-control text", () => {
  const context = assistant.buildClientContext({
    rootNode: pageFixture(),
    documentRef: { title: "Kjøkken", defaultView: { getComputedStyle: () => ({}) } },
    locationRef: { hash: "#week", pathname: "/", search: "?access_token=never-send" },
    selection: "valgt suppe",
    activity: "Valgte: suppe",
  });

  assert.deepEqual(context, {
    route: "#week",
    page: "Ukeplan",
    title: "Kjøkken",
    visible_text: "Synlig innhold på siden: Ukeplan Synlig måltid: suppe Passord Bearer [skjult]",
    activity: "Siste aktivitet: Valgte: suppe. Markert tekst: valgt suppe",
  });
  assert.doesNotMatch(JSON.stringify(context), /Hemmelig intern tekst|Skjult assistentinnhold|standardverdier|Privat valg|access_token|abcdefghijklmnopqrstuvwxyz/u);
});

test("client context bounds visible text and rejects unrecognized route fragments", () => {
  const root = element("main", [element("h1", [text("Katalog")]), element("p", [text("x".repeat(6000))])]);
  root.querySelector = () => root.childNodes[0];
  const context = assistant.buildClientContext({
    rootNode: root,
    documentRef: { title: "Kjøkken", defaultView: { getComputedStyle: () => ({}) } },
    locationRef: { hash: "#access_token=secret", pathname: "/" },
  });
  assert.equal(context.route, "/");
  assert.ok(context.visible_text.length <= assistant.MAX_VISIBLE_TEXT + "Synlig innhold på siden: ".length);
  assert.doesNotMatch(context.visible_text, /secret/u);
});

test("history keeps only bounded user and assistant turns", () => {
  const history = [];
  for (let index = 0; index < 22; index += 1) {
    history.push({ role: "user", content: `Spørsmål ${index}` });
    history.push({ role: "assistant", content: `Svar ${index}` });
  }
  history.push({ role: "system", content: "ikke send" });
  const bounded = assistant.boundedHistory(history);
  assert.equal(bounded.length, 40);
  assert.equal(bounded[0].content, "Spørsmål 2");
  assert.ok(bounded.every((turn) => ["user", "assistant"].includes(turn.role)));

  const largeHistory = Array.from({ length: 20 }, (_, index) => [
    { role: "user", content: `U${index}` + "u".repeat(3998) },
    { role: "assistant", content: `A${index}` + "a".repeat(3998) },
  ]).flat();
  const sizeBounded = assistant.boundedHistory(largeHistory);
  assert.ok(sizeBounded.reduce((total, turn) => total + turn.content.length, 0) <= 24000);
  assert.equal(sizeBounded[0].role, "user");
  assert.equal(sizeBounded.at(-1).role, "assistant");
});

test("action responses make the review and confirmation boundary explicit", () => {
  const notices = assistant.responseNotices({
    reply: "Jeg foreslår å flytte middagen.",
    actions: [{
      type: "replace_meal",
      proposal_id: "proposal-1",
      summary: "Bytt tirsdagens middag til tomatsuppe.",
      confirm_endpoint: "/api/ai/assistant/proposals/proposal-1/confirm",
    }],
    confirmation_required: false,
  });
  assert.equal(notices[0], "Dette er forslag. Ingen endringer er lagret før du har sett dem over og bekreftet dem.");
  assert.equal(notices[1], "Forslag: Bytt tirsdagens middag til tomatsuppe.");
  assert.ok(!notices.some((notice) => notice.includes("proposal-1") || notice.includes("confirm")));

  const confirmation = assistant.responseNotices({ confirmation_required: true });
  assert.equal(confirmation[0], "Krever bekreftelse. Endringen er ikke lagret.");
  assert.equal(assistant.responseNotices({ actions: [{ type: "recipe_saved", summary: "Oppskrift lagret: Tomatsuppe" }] })[0], "Endringene er lagret.");
});

test("assistant sends current context on each turn and retains the conversation across routes", async () => {
  const heading = new MockElement("h1", "Ukeplan");
  const visible = new MockElement("p", "Middag: suppe");
  const privateInput = new MockElement("input");
  privateInput.value = "do-not-send";
  const main = new MockElement("main");
  main.append(heading, visible, privateInput);
  main.querySelector = (selector) => selector === "h1" ? heading : null;

  const elements = {
    "#assistant-toggle": new MockElement("button"),
    "#assistant-panel": new MockElement("section"),
    "#assistant-close": new MockElement("button"),
    "#assistant-messages": new MockElement("div"),
    "#assistant-form": new MockElement("form"),
    "#assistant-input": new MockElement("textarea"),
    "#assistant-send": new MockElement("button"),
    "#assistant-status": new MockElement("p"),
    "#main": main,
  };
  const documentRef = {
    title: "Kjøkken",
    querySelector: (selector) => elements[selector] || null,
    createElement: (tag) => new MockElement(tag),
    addEventListener() {},
    defaultView: { getComputedStyle: () => ({}) },
  };
  const payloads = [];
  const windowRef = {
    document: documentRef,
    location: { hash: "#week", pathname: "/" },
    fetch: async (url, options) => {
      payloads.push({ url, options, body: JSON.parse(options.body) });
      return {
        ok: true,
        json: async () => payloads.length === 1
          ? {
              reply: "Jeg har laget et forslag.",
              actions: [{ type: "meal_plan_proposal", proposal_id: "pending-1", summary: "Bytt tirsdagens middag.", confirm_endpoint: "/api/meal-plan-proposals/pending-1/confirm" }],
            }
          : { reply: `Svar ${payloads.length}` },
      };
    },
  };
  const assistantUI = assistant.initialize(windowRef);
  const submit = elements["#assistant-form"].listeners.submit[0];
  elements["#assistant-input"].value = "Hva står på planen?";
  await submit({ preventDefault() {} });

  assert.equal(payloads[0].url, "/api/ai/assistant/chat");
  assert.deepEqual(payloads[0].body.history, []);
  assert.equal(payloads[0].body.client_context.route, "#week");
  assert.match(payloads[0].body.client_context.visible_text, /Middag: suppe/u);
  assert.doesNotMatch(JSON.stringify(payloads[0].body.client_context), /do-not-send/u);
  assert.equal(payloads.length, 1, "a returned proposal must not be confirmed automatically");
  assert.equal(elements["#assistant-messages"].childNodes.at(-1).className, "assistant-review");

  elements["#assistant-input"].value = "Og neste uke?";
  windowRef.location.hash = "#shopping";
  heading.textContent = "Handleliste";
  visible.textContent = "Gulrøtter";
  await submit({ preventDefault() {} });

  assert.equal(payloads[1].body.client_context.route, "#shopping");
  assert.equal(payloads[1].body.client_context.page, "Handleliste");
  assert.match(payloads[1].body.client_context.visible_text, /Gulrøtter/u);
  assert.deepEqual(payloads[1].body.history, [
    { role: "user", content: "Hva står på planen?" },
    { role: "assistant", content: "Jeg har laget et forslag." },
  ]);
  assert.deepEqual(assistantUI.history, [
    { role: "user", content: "Hva står på planen?" },
    { role: "assistant", content: "Jeg har laget et forslag." },
    { role: "user", content: "Og neste uke?" },
    { role: "assistant", content: "Svar 2" },
  ]);
});

test("week-plan proposal shows details and only saves after explicit confirmation", async () => {
  const calls = [];
  let refreshCount = 0;
  const harness = assistantHarness(async (url, options) => {
    calls.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
    if (url === "/api/ai/assistant/chat")
      return { ok: true, json: async () => ({
        reply: "Jeg foreslår tomatsuppe.",
        actions: [{ type: "meal_plan_proposal", proposal_id: "plan-1", summary: "Legg til tomatsuppe.", confirm_endpoint: "/api/meal-plan-proposals/plan-1/confirm" }],
      }) };
    if (url === "/api/meal-plan-proposals/plan-1")
      return { ok: true, json: async () => ({
        id: "plan-1",
        status: "pending",
        meal_entries: [{ date: "2026-11-23", recipe_id: "recipe-1", servings: 4, label: "Middag", scheduled_time: "18:00" }],
        existing_meals: {},
        created_at: "2026-11-22T10:00:00Z",
        expires_at: "2099-11-23T10:00:00Z",
      }) };
    if (url === "/api/recipes?include_stats=true")
      return { ok: true, json: async () => [{ id: "recipe-1", title: "Tomatsuppe" }] };
    if (url.endsWith("/confirm"))
      return { ok: true, json: async () => ({ id: "plan-1", status: "applied", meal_entries: [] }) };
    throw new Error(`Unexpected request: ${url}`);
  });
  harness.windowRef.refreshView = async () => { refreshCount += 1; };
  assistant.initialize(harness.windowRef);
  harness.elements["#assistant-input"].value = "Legg til tomatsuppe";
  await harness.elements["#assistant-form"].listeners.submit[0]({ preventDefault() {} });

  const messageSection = harness.elements["#assistant-messages"].childNodes.at(-1);
  const details = messageSection.childNodes.find((child) => child.tagName === "DETAILS");
  const content = details.childNodes.find((child) => child.tagName === "DIV");
  const loadButton = descendants(content).find((node) => node.tagName === "BUTTON");
  await loadButton.listeners.click[0]();

  assert.match(content.textContent, /Ukeplanforslag – ikke lagret/u);
  assert.match(content.textContent, /Tomatsuppe/u);
  assert.match(content.textContent, /23\.11\.2026/u);
  assert.match(content.textContent, /4 porsjoner/u);
  assert.equal(calls.some((call) => call.url.endsWith("/confirm")), false, "reading a proposal must not save it");

  const confirmButton = descendants(content).find((node) => node.tagName === "BUTTON" && node.textContent === "Bekreft og lagre ukeplan");
  assert.ok(confirmButton, "the user receives a separate, explicit confirmation control");
  await confirmButton.listeners.click[0]();
  const confirmation = calls.find((call) => call.url.endsWith("/confirm"));
  assert.deepEqual(confirmation, {
    url: "/api/meal-plan-proposals/plan-1/confirm",
    method: "POST",
    body: { reviewed: true },
  });
  assert.match(content.textContent, /Ukeplanforslaget er bekreftet og lagret/u);
  assert.equal(refreshCount, 1);
});

test("stale week-plan proposals cannot be confirmed again", async () => {
  const harness = assistantHarness(async (url) => {
    if (url === "/api/ai/assistant/chat")
      return { ok: true, json: async () => ({ reply: "Forslag.", actions: [{ type: "meal_plan_proposal", proposal_id: "stale-1", summary: "Forslag.", confirm_endpoint: "/api/meal-plan-proposals/stale-1/confirm" }] }) };
    if (url === "/api/meal-plan-proposals/stale-1")
      return { ok: true, json: async () => ({ id: "stale-1", status: "pending", meal_entries: [{ date: "2026-11-23", recipe_id: "r1", servings: 2 }], existing_meals: {}, expires_at: "2099-01-01T00:00:00Z" }) };
    if (url === "/api/recipes?include_stats=true") return { ok: true, json: async () => [] };
    if (url.endsWith("/confirm")) return { ok: false, status: 409, json: async () => ({ detail: "stale" }) };
    throw new Error(`Unexpected request: ${url}`);
  });
  assistant.initialize(harness.windowRef);
  harness.elements["#assistant-input"].value = "Lagre forslaget";
  await harness.elements["#assistant-form"].listeners.submit[0]({ preventDefault() {} });
  const section = harness.elements["#assistant-messages"].childNodes.at(-1);
  const details = section.childNodes.find((child) => child.tagName === "DETAILS");
  const content = details.childNodes.find((child) => child.tagName === "DIV");
  const loadButton = descendants(content).find((node) => node.tagName === "BUTTON");
  await loadButton.listeners.click[0]();
  const confirmButton = descendants(content).find((node) => node.tagName === "BUTTON" && node.textContent === "Bekreft og lagre ukeplan");
  await confirmButton.listeners.click[0]();

  assert.match(content.textContent, /Forslaget er utdatert\. Ingen endringer ble lagret/u);
  assert.equal(descendants(content).some((node) => node.tagName === "BUTTON" && node.textContent === "Bekreft og lagre ukeplan"), false);
});

test("Oda proposal review opens the existing review surface without confirming it", async () => {
  const heading = new MockElement("h1", "Plan");
  const main = new MockElement("main");
  main.append(heading);
  main.querySelector = () => heading;
  const elements = {
    "#assistant-toggle": new MockElement("button"),
    "#assistant-panel": new MockElement("section"),
    "#assistant-close": new MockElement("button"),
    "#assistant-messages": new MockElement("div"),
    "#assistant-form": new MockElement("form"),
    "#assistant-input": new MockElement("textarea"),
    "#assistant-send": new MockElement("button"),
    "#assistant-status": new MockElement("p"),
    "#main": main,
  };
  const documentRef = {
    title: "Kjøkken",
    querySelector: (selector) => elements[selector] || null,
    createElement: (tag) => new MockElement(tag),
    addEventListener() {},
    defaultView: { getComputedStyle: () => ({}) },
  };
  const calls = [];
  let openedReview = null;
  const windowRef = {
    document: documentRef,
    location: { hash: "#week", pathname: "/" },
    renderOdaProposal: (proposal, request) => { openedReview = { proposal, request }; },
    fetch: async (url, options) => {
      calls.push({ url, method: options.method });
      if (url === "/api/ai/assistant/chat")
        return { ok: true, json: async () => ({
          reply: "Jeg har laget et Oda-forslag.",
          actions: [{ type: "oda_cart_proposal", proposal_id: "oda/1", summary: "Legg til gulrøtter.", confirm_endpoint: "/api/oda/cart/proposals/oda%2F1/confirm" }],
        }) };
      return { ok: true, json: async () => ({ proposal_id: "oda/1", status: "pending", additions: [{ quantity: 1 }] }) };
    },
  };
  assistant.initialize(windowRef);
  elements["#assistant-input"].value = "Legg gulrøtter i handlekurven";
  await elements["#assistant-form"].listeners.submit[0]({ preventDefault() {} });

  const reviewSection = elements["#assistant-messages"].childNodes.at(-1);
  const reviewButton = reviewSection.childNodes.find((child) => child.tagName === "BUTTON");
  await reviewButton.listeners.click[0]();
  assert.deepEqual(calls, [
    { url: "/api/ai/assistant/chat", method: "POST" },
    { url: "/api/oda/cart/proposals/oda%2F1", method: "GET" },
  ]);
  assert.equal(openedReview.proposal.proposal_id, "oda/1");
  assert.deepEqual(openedReview.request, { additions: [] });
});

test("recipe deletion requires an explicit click, uses the typed ID, and navigates after success", async () => {
  const recipeId = "550e8400-e29b-41d4-a716-446655440000";
  const calls = [];
  let navigatedTo = "";
  const harness = assistantHarness(async (url, options) => {
    calls.push({ url, method: options.method, credentials: options.credentials || null, body: options.body || null });
    if (url === "/api/ai/assistant/chat")
      return { ok: true, json: async () => ({
        reply: "Jeg har et forslag om å fjerne oppskriften.",
        actions: [{
          type: "recipe_delete_proposal",
          recipe_id: recipeId,
          recipe_title: "Tomatsuppe med basilikum",
          summary: "Fjern Tomatsuppe med basilikum.",
          confirm_endpoint: "https://example.invalid/unsafe",
          confirm_method: "DELETE",
          delete_endpoint: "https://example.invalid/unsafe",
        }],
      }) };
    if (url === `/api/recipes/${recipeId}`)
      return { ok: true, json: async () => ({ deleted: true }) };
    throw new Error(`Unexpected request: ${url}`);
  });
  harness.windowRef.navigate = (view) => { navigatedTo = view; };
  assistant.initialize(harness.windowRef);
  harness.elements["#assistant-input"].value = "Slett suppa";
  await harness.elements["#assistant-form"].listeners.submit[0]({ preventDefault() {} });

  const section = harness.elements["#assistant-messages"].childNodes.at(-1);
  const card = section.childNodes.find((node) => node.className === "assistant-delete-review");
  assert.match(card.textContent, /Tomatsuppe med basilikum/u);
  assert.match(card.textContent, /Handlingen kan ikke angres/u);
  const remove = descendants(card).find((node) => node.tagName === "BUTTON");
  assert.equal(calls.length, 1, "the assistant response must not delete anything automatically");

  await remove.listeners.click[0]();
  assert.deepEqual(calls[1], {
    url: `/api/recipes/${recipeId}`,
    method: "DELETE",
    credentials: "same-origin",
    body: null,
  });
  assert.equal(navigatedTo, "recipes");
  assert.match(card.textContent, /Oppskriften «Tomatsuppe med basilikum» er slettet/u);
  await remove.listeners.click[0]();
  assert.equal(calls.length, 2, "a completed deletion is never retried");
});

test("missing, network-failed, and server-failed recipe deletions stop without retry", async (t) => {
  const recipeId = "550e8400-e29b-41d4-a716-446655440000";
  const cases = [
    { name: "already missing", status: 404, expected: /Oppskriften finnes ikke lenger/u },
    { name: "network uncertainty", networkError: true, expected: /kan allerede være slettet/u },
    { name: "server uncertainty", status: 500, expected: /kunne ikke bekrefte om oppskriften ble slettet/u },
  ];
  for (const scenario of cases) {
    await t.test(scenario.name, async () => {
      const calls = [];
      const harness = assistantHarness(async (url, options) => {
        calls.push({ url, method: options.method });
        if (url === "/api/ai/assistant/chat")
          return { ok: true, json: async () => ({ reply: "Forslag.", actions: [{
            type: "recipe_delete_proposal",
            recipe_id: recipeId,
            recipe_title: "Linsegryte",
            summary: "Fjern Linsegryte.",
          }] }) };
        if (scenario.networkError) throw new TypeError("Failed to fetch");
        return { ok: false, status: scenario.status, json: async () => ({ detail: "failure" }) };
      });
      assistant.initialize(harness.windowRef);
      harness.elements["#assistant-input"].value = "Slett retten";
      await harness.elements["#assistant-form"].listeners.submit[0]({ preventDefault() {} });
      const section = harness.elements["#assistant-messages"].childNodes.at(-1);
      const card = section.childNodes.find((node) => node.className === "assistant-delete-review");
      const remove = descendants(card).find((node) => node.tagName === "BUTTON");
      await remove.listeners.click[0]();
      assert.match(card.textContent, scenario.expected);
      assert.equal(remove.disabled, true);
      await remove.listeners.click[0]();
      assert.equal(calls.length, 2, "the uncertain or missing result must not be retried automatically");
    });
  }
});

test("the persistent assistant shell is present and its script is cached by the service worker", () => {
  const staticDir = path.join(__dirname, "../src/home_kitchen/static");
  const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  const serviceWorker = fs.readFileSync(path.join(staticDir, "sw.js"), "utf8");
  assert.match(html, /id="assistant-toggle"[\s\S]*?aria-controls="assistant-panel"/u);
  assert.match(html, /id="assistant-messages"[\s\S]*?role="log"/u);
  assert.match(html, /id="assistant-input"[\s\S]*?maxlength="4000"/u);
  assert.match(html, /\/static\/assistant-chat\.js/u);
  assert.match(serviceWorker, /kitchen-shell-v25/u);
  assert.match(serviceWorker, /'\/static\/assistant-chat\.js'/u);
});
