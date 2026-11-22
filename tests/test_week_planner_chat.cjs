const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const chat = require("../src/home_kitchen/static/week-planner-chat.js");
const appSource = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);

class DomNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.ownText = text == null ? "" : String(text);
    this.className = className || "";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.type = "";
    this.id = "";
    this.name = "";
    this.placeholder = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.parentNode = null;
    this.focused = false;
    this.classList = {
      contains: (cls) => this.className.split(/\s+/).filter(Boolean).includes(cls),
    };
  }
  set textContent(value) {
    this.ownText = value == null ? "" : String(value);
    this.children = [];
  }
  get textContent() {
    return this.ownText + this.children.map((child) => child.textContent).join("");
  }
  append(...children) {
    children.forEach((child) => {
      if (!child) return;
      child.parentNode = this;
      this.children.push(child);
    });
  }
  prepend(...children) {
    children.filter(Boolean).reverse().forEach((child) => {
      child.parentNode = this;
      this.children.unshift(child);
    });
  }
  replaceChildren(...children) {
    this.ownText = "";
    this.children = [];
    this.append(...children);
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
  }
  getAttribute(key) {
    return this.attributes[key] ?? null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  contains(target) {
    if (this === target) return true;
    return this.children.some((child) => child.contains?.(target));
  }
  focus() {
    this.focused = true;
  }
  scrollIntoView() {}
  async click() {
    if (this.disabled) return;
    if (this.type === "submit") {
      let current = this.parentNode;
      while (current && current.tagName !== "FORM") current = current.parentNode;
      if (current) {
        await current.dispatchEvent("submit", { preventDefault() {} });
        return;
      }
    }
    for (const fn of this.listeners.click || []) {
      await fn({ target: this, currentTarget: this, preventDefault() {} });
    }
  }
  async dispatchEvent(type, extra = {}) {
    const event = { target: this, currentTarget: this, preventDefault() {}, ...extra };
    for (const fn of this.listeners[type] || []) {
      await fn(event);
    }
  }
  findAll(predicate, acc = []) {
    if (predicate(this)) acc.push(this);
    this.children.forEach((child) => child.findAll?.(predicate, acc));
    return acc;
  }
  querySelector(selector) {
    if (selector.startsWith(".")) {
      const cls = selector.slice(1);
      return this.findAll((n) => n.classList.contains(cls))[0] || null;
    }
    if (selector.startsWith("#")) {
      const id = selector.slice(1);
      return this.findAll((n) => n.id === id)[0] || null;
    }
    return this.findAll((n) => n.tagName.toLowerCase() === selector.toLowerCase())[0] || null;
  }
  querySelectorAll(selector) {
    if (selector.startsWith(".")) {
      const cls = selector.slice(1);
      return this.findAll((n) => n.classList.contains(cls));
    }
    return this.findAll((n) => n.tagName.toLowerCase() === selector.toLowerCase());
  }
}

function createFakeDocument() {
  return {
    createElement: (tag) => new DomNode(tag),
  };
}

function renderChatPanel({ apiImpl, state = {}, refreshImpl } = {}) {
  const doc = createFakeDocument();
  const calls = [];
  let refreshes = 0;
  let settingsClicked = 0;
  const panel = chat.renderSection({
    state,
    weekStartIso: "2026-11-23",
    days: 7,
    document: doc,
    dateLabel: (d) => d.toISOString().slice(0, 10),
    formatUiTime: (t) => String(t).slice(0, 5),
    connectionSettingsButton: () => {
      const btn = doc.createElement("button");
      btn.textContent = "Innstillinger";
      btn.addEventListener("click", () => {
        settingsClicked += 1;
      });
      return btn;
    },
    api: async (endpoint, method, body) => {
      calls.push({ endpoint, method, body });
      return apiImpl(endpoint, method, body);
    },
    refreshView: async () => {
      refreshes += 1;
      if (refreshImpl) await refreshImpl();
    },
  });
  return {
    panel,
    state,
    calls,
    refreshes: () => refreshes,
    settingsClicked: () => settingsClicked,
    textarea: () => panel.querySelector("#week-chat-input"),
    applyToggle: () => panel.querySelector("#week-chat-apply-toggle"),
    form: () => panel.querySelector("form"),
    sendBtn: () => panel.querySelector(".week-chat-form-actions button"),
    status: () => panel.querySelector(".week-chat-status"),
    error: () => panel.querySelector(".week-chat-error"),
    log: () => panel.querySelector(".week-chat-log"),
    chips: () => panel.querySelectorAll(".week-chat-chip"),
    turns: () => panel.querySelectorAll(".week-chat-turn"),
    resetBtn: () =>
      panel.querySelectorAll("button").find((btn) => btn.textContent === "Ny samtale"),
    toggleBtn: () =>
      panel.querySelectorAll("button").find((btn) => btn.getAttribute("aria-expanded") !== null),
  };
}

test("buildChatPayload trims text, validates limits, and keeps prior turns", () => {
  assert.throws(
    () => chat.buildChatPayload({ weekStartIso: "2026-11-23", message: "   " }),
    /Skriv inn en melding/,
  );
  const payload = chat.buildChatPayload({
    weekStartIso: "2026-11-23",
    days: 7,
    message: "  Legg til fiskesuppe  ",
    turns: [
      { role: "user", content: "Hei" },
      { role: "assistant", content: "Hei! Hva vil du lage?" },
      { role: "system", content: "ignore" },
    ],
    applyChanges: false,
  });
  assert.deepEqual(payload, {
    start: "2026-11-23",
    days: 7,
    message: "Legg til fiskesuppe",
    history: [
      { role: "user", content: "Hei" },
      { role: "assistant", content: "Hei! Hva vil du lage?" },
    ],
    apply_changes: false,
  });
});

test("changeActionLabel and formatChangeSummary format add, replace, and remove in Bokmål", () => {
  assert.equal(chat.changeActionLabel({ action: "add", status: "applied" }), "Lagt til");
  assert.equal(chat.changeActionLabel({ action: "replace", status: "proposed" }), "Foreslått byttet ut");
  assert.equal(chat.changeActionLabel({ action: "remove", status: "applied" }), "Fjernet");

  const summary = chat.formatChangeSummary(
    {
      action: "replace",
      status: "applied",
      date: "2026-11-24",
      recipe_title: "Laks i form",
      previous_recipe_title: "Tomatsuppe",
      servings: 4,
      label: "Middag",
      scheduled_time: "17:30:00",
    },
    { formatDate: (d) => d, formatTime: (t) => t.slice(0, 5) },
  );
  assert.equal(summary.title, "Tomatsuppe → Laks i form");
  assert.equal(summary.meta, "2026-11-24 · Middag · 17:30 · 4 porsjoner");
});

test("multi-turn week chat renders accessible controls, applies changes, and preserves history", async () => {
  let turnCount = 0;
  const ui = renderChatPanel({
    apiImpl: async (endpoint, _method, body) => {
      turnCount += 1;
      if (turnCount === 1) {
        assert.deepEqual(body.history, []);
        return {
          reply: "La til Fiskesuppe på mandag.",
          applied: true,
          notes: ["Brukte gulrøtter du har hjemme."],
          changes: [
            {
              action: "add",
              status: "applied",
              meal_id: "m-1",
              date: "2026-11-23",
              recipe_id: "r-1",
              recipe_title: "Fiskesuppe",
              servings: 4,
            },
          ],
        };
      }
      return {
        reply: "Byttet ut mandag til Pasta og fjernet fredag.",
        applied: true,
        changes: [
          {
            action: "replace",
            status: "applied",
            meal_id: "m-1",
            date: "2026-11-23",
            recipe_id: "r-2",
            recipe_title: "Pasta",
            previous_recipe_title: "Fiskesuppe",
            servings: 4,
          },
          {
            action: "remove",
            status: "applied",
            meal_id: "m-2",
            date: "2026-11-27",
            recipe_id: "r-3",
            recipe_title: "Taco",
            servings: 4,
          },
        ],
      };
    },
  });

  // Accessibility checks
  assert.equal(ui.panel.getAttribute("aria-labelledby"), "week-chat-heading");
  assert.equal(ui.log().getAttribute("role"), "log");
  assert.equal(ui.status().getAttribute("role"), "status");
  assert.equal(ui.error().getAttribute("role"), "alert");
  assert.equal(
    ui.panel.querySelector("label").parentNode.querySelector('label[for="week-chat-apply-toggle"]') !== null ||
      ui.panel.querySelectorAll("label").some((l) => l.getAttribute("for") === "week-chat-input"),
    true,
  );

  // Quick prompt chip fills textarea without sending
  ui.chips()[0].click();
  assert.equal(ui.textarea().value, chat.QUICK_PROMPTS[0]);
  assert.equal(ui.calls.length, 0);

  // Turn 1 submit
  await ui.form().dispatchEvent("submit");
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.refreshes(), 1);
  assert.equal(ui.turns().length, 2);
  assert.match(ui.panel.textContent, /La til Fiskesuppe på mandag/);
  assert.match(ui.panel.textContent, /Brukte gulrøtter du har hjemme/);
  assert.equal(ui.textarea().value, "");

  // Turn 2 includes Turn 1 in history
  ui.textarea().value = "Bytt mandag til pasta og fjern fredag";
  await ui.form().dispatchEvent("submit");
  assert.equal(ui.calls.length, 2);
  assert.deepEqual(ui.calls[1].body.history, [
    { role: "user", content: chat.QUICK_PROMPTS[0] },
    { role: "assistant", content: "La til Fiskesuppe på mandag." },
  ]);
  assert.equal(ui.refreshes(), 2);
  assert.equal(ui.turns().length, 4);
  assert.match(ui.panel.textContent, /Fiskesuppe → Pasta/);
  assert.match(ui.panel.textContent, /Fjernet/);
});

test("propose mode stages changes and applies them only after clicking apply button", async () => {
  const proposedChanges = [
    {
      action: "add",
      status: "proposed",
      meal_id: null,
      date: "2026-11-24",
      recipe_id: "r-1",
      recipe_title: "Laks i form",
      servings: 4,
    },
  ];
  const ui = renderChatPanel({
    apiImpl: async (endpoint, _method, body) => {
      if (endpoint === "/ai/week-chat") {
        assert.equal(body.apply_changes, false);
        return {
          reply: "Jeg foreslår Laks i form på tirsdag.",
          applied: false,
          changes: proposedChanges,
        };
      }
      if (endpoint === "/ai/week-chat/apply") {
        assert.equal(body.reviewed, true);
        return {
          applied_changes: [{ ...proposedChanges[0], status: "applied", meal_id: "m-9" }],
          meals: [],
        };
      }
      throw new Error(`Unexpected endpoint: ${endpoint}`);
    },
  });

  ui.applyToggle().checked = false;
  await ui.applyToggle().dispatchEvent("change");
  ui.textarea().value = "Foreslå tirsdag";
  await ui.form().dispatchEvent("submit");

  assert.equal(ui.refreshes(), 0);
  assert.match(ui.panel.textContent, /Foreslått lagt til/);
  const applyBtn = ui.panel
    .querySelectorAll("button")
    .find((btn) => btn.textContent === "Bruk foreslåtte endringer");
  assert.ok(applyBtn, "Expected apply button for proposed changes");

  await applyBtn.click();
  assert.equal(ui.calls.length, 2);
  assert.equal(ui.calls[1].endpoint, "/ai/week-chat/apply");
  assert.equal(ui.refreshes(), 1);
  assert.match(ui.panel.textContent, /Lagt til/);
  assert.equal(
    ui.panel.querySelectorAll("button").some((btn) => btn.textContent === "Bruk foreslåtte endringer"),
    false,
  );
});

test("503 error exposes settings link, preserves draft text, and reset clears turns", async () => {
  const ui = renderChatPanel({
    apiImpl: async () => {
      const err = new Error("Konfigurer en KI-modell");
      err.status = 503;
      throw err;
    },
  });
  ui.textarea().value = "Lag ukeplan";
  await ui.form().dispatchEvent("submit");

  assert.match(ui.error().textContent, /Konfigurer en KI-modell/);
  assert.equal(ui.state.weekChat.draft, "Lag ukeplan");
  assert.ok(ui.error().querySelector("button"));

  // Toggle collapse & expand
  await ui.toggleBtn().click();
  assert.equal(ui.toggleBtn().getAttribute("aria-expanded"), "false");
  assert.equal(ui.panel.querySelector(".week-chat-body").hidden, true);
  await ui.toggleBtn().click();
  assert.equal(ui.toggleBtn().getAttribute("aria-expanded"), "true");

  // Add a turn and reset
  ui.state.weekChat.turns.push({ role: "user", content: "Hei" });
  const ui2 = renderChatPanel({ state: ui.state, apiImpl: async () => ({}) });
  await ui2.resetBtn().click();
  assert.equal(ui2.state.weekChat.turns.length, 0);
  assert.match(ui2.status().textContent, /Startet en ny samtale/);
});

test("app.js integrates week-planner-chat in renderWeek and allows idle background refresh", () => {
  assert.match(appSource, /KitchenWeekPlannerChat/);
  assert.match(appSource, /Planlegg med KI-chat/);
  assert.match(appSource, /hasOpenWeekForm/);

  const match = appSource.match(/function hasOpenWeekForm\([\s\S]*?\n\}/u);
  assert.ok(match, "app.js should define hasOpenWeekForm");
  const context = {
    state: { weekChat: { sending: false, applyingTurnIndex: null, draft: "" } },
    document: { activeElement: null },
    $: () => null,
  };
  vm.runInNewContext(`${match[0]}\nglobalThis.hasOpenWeekForm = hasOpenWeekForm;`, context);

  const chatInput = new DomNode("textarea");
  const chatForm = new DomNode("form", "", "week-chat-form");
  chatForm.append(chatInput);
  const main = new DomNode("main");
  main.append(chatForm);

  // Idle chat form must NOT block background week refresh
  assert.equal(context.hasOpenWeekForm(main, null), false);
  // Focused chat input DOES block refresh
  assert.equal(context.hasOpenWeekForm(main, chatInput), true);
  // Draft text or in-flight send DOES block refresh
  context.state.weekChat.draft = "Foreslå fisk";
  assert.equal(context.hasOpenWeekForm(main, null), true);
  context.state.weekChat.draft = "";
  context.state.weekChat.sending = true;
  assert.equal(context.hasOpenWeekForm(main, null), true);
});
