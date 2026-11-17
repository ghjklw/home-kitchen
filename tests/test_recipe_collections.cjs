"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const KitchenRecipeCollections = require("../src/home_kitchen/static/recipe-collections.js");
const {
  canSelect,
  collectionErrorMessage,
  completionCounts,
  connectionGuidance,
  connectionMessage,
  entryStatus,
  errorMessages,
  importPayload,
  isAllSelected,
  normalizeJob,
  progress,
  selectableEntries,
  selectableIds,
  toggleAllSelection,
} = KitchenRecipeCollections;

const fixture = {
  job_id: "job-1",
  status: "ready",
  kinds: ["liked", "purchased"],
  entries: [
    { id: "10", provider: "oda", title: "Fiskesuppe", badges: ["Liked on Oda"], status: "pending" },
    { id: "11", provider: "oda", title: "Pannekaker", badges: ["Ingredients bought on Oda"], status: "already_imported", recipe_id: "recipe-1" },
    { id: "12", provider: "oda", title: "Tomatsaus", status: "missing_url" },
  ],
  pages_read: 4,
  processed: 0,
  total: 3,
  truncated: false,
  truncated_kinds: [],
  errors: [],
};

test("normalizes a bounded Oda collection review and exposes only importable entries", () => {
  const job = normalizeJob(fixture);
  assert.equal(job.entries.length, 3);
  assert.equal(canSelect(job.entries[0]), true);
  assert.equal(canSelect(job.entries[1]), false);
  assert.equal(canSelect(job.entries[2]), false);
  assert.equal(entryStatus("already_imported"), "Finnes allerede i samlingen");
  assert.equal(progress({ ...job, status: "previewing" }), "Hentet 4 Oda-sider · 3 oppskrifter funnet …");
});

test("builds an explicit reviewed import request from unique selected source IDs", () => {
  assert.deepEqual(importPayload("job/1", ["10"]), {
    path: "/recipe-collections/job%2F1/import",
    body: { selected_ids: ["10"], reviewed: true },
  });
  assert.throws(() => importPayload("job-1", []), /Velg minst én/);
  assert.throws(() => importPayload("job-1", ["10", "10"]), /Velg minst én/);
});

test("rejects malformed jobs instead of showing misleading collection state", () => {
  assert.throws(() => normalizeJob({ ...fixture, status: "unknown" }), /ugyldig importstatus/);
  assert.throws(() => normalizeJob({ ...fixture, entries: [{ id: "1", title: "No status" }] }), /ugyldig oppskriftsrad/);
});

test("reports completed, pre-existing, and failed recipes separately after partial cancellation", () => {
  const job = normalizeJob({
    ...fixture,
    status: "cancelled",
    entries: [
      { id: "1", title: "A", status: "imported" },
      { id: "2", title: "B", status: "already_imported" },
      { id: "3", title: "C", status: "failed" },
      { id: "4", title: "D", status: "pending" },
    ],
  });
  assert.deepEqual(completionCounts(job), { imported: 1, already: 1, failed: 1 });
});

test("shows Settings guidance only for known Oda setup or authorization states", () => {
  const reconnect = (message) => /Oda authorization expired/iu.test(message);
  assert.equal(connectionGuidance({ configured: false, connected: false }, new Error("failed"), reconnect), "setup_required");
  assert.equal(connectionGuidance({ configured: true, connected: false }, new Error("failed"), reconnect), "connect_required");
  assert.equal(connectionGuidance({ configured: true, connected: true }, new Error("Oda authorization expired"), reconnect), "reconnect_required");
  assert.equal(connectionGuidance({ configured: true, connected: true }, Object.assign(new Error("Login required"), { status: 401 }), reconnect), null);
  assert.equal(connectionGuidance({ configured: true, connected: true }, new Error("Fikk ikke kontakt med kjøkkenet"), reconnect), null);
  assert.match(connectionMessage("connect_required"), /Oda-kontoen er ikke tilkoblet/u);
  assert.match(connectionMessage("reconnect_required"), /ny godkjenning/u);
});

test("translates collection error messages and deduplicates rendered errors", () => {
  assert.equal(
    collectionErrorMessage("Could not finish Liked on Oda; preview again to retry."),
    "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Could not finish Ingredients bought on Oda; preview again to retry."),
    "Kunne ikke hente oppskrifter med bestilte ingredienser fra Oda. Prøv å hente på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Collection connection changed or became unavailable; preview again."),
    "Oda-tilkoblingen ble endret eller er utilgjengelig. Hent samlingene på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Your Oda connection expired; connect again"),
    "Oda-tilkoblingen utløp. Koble til på nytt i Innstillinger.",
  );

  const jobWithDualFailures = {
    errors: [
      "Could not finish Liked on Oda; preview again to retry.",
      "Could not finish Ingredients bought on Oda; preview again to retry.",
    ],
  };
  const translated = errorMessages(jobWithDualFailures);
  assert.equal(translated.length, 2);
  assert.equal(translated[0], "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.");
  assert.equal(translated[1], "Kunne ikke hente oppskrifter med bestilte ingredienser fra Oda. Prøv å hente på nytt.");
  assert.notEqual(translated[0], translated[1]);

  const jobWithDuplicates = {
    errors: [
      "Could not finish Liked on Oda; preview again to retry.",
      "Could not finish Liked on Oda; preview again to retry.",
      "Collection connection changed or became unavailable; preview again.",
    ],
  };
  const deduplicated = errorMessages(jobWithDuplicates);
  assert.equal(deduplicated.length, 2);
  assert.deepEqual(deduplicated, [
    "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.",
    "Oda-tilkoblingen ble endret eller er utilgjengelig. Hent samlingene på nytt.",
  ]);
});

test("selectableEntries, selectableIds, isAllSelected, and toggleAllSelection manage importable entries", () => {
  const job = normalizeJob({
    ...fixture,
    entries: [
      { id: "10", provider: "oda", title: "Fiskesuppe", status: "pending" },
      { id: "11", provider: "oda", title: "Pannekaker", status: "already_imported" },
      { id: "12", provider: "oda", title: "Tomatsaus", status: "missing_url" },
      { id: "13", provider: "oda", title: "Lasagne", status: "failed" },
    ],
  });
  assert.deepEqual(selectableEntries(job).map((e) => e.id), ["10", "13"]);
  assert.deepEqual(selectableIds(job), ["10", "13"]);
  assert.equal(isAllSelected(new Set(), job), false);
  assert.equal(isAllSelected(new Set(["10"]), job), false);
  assert.equal(isAllSelected(new Set(["10", "13"]), job), true);

  const afterSelectAll = toggleAllSelection(new Set(["10"]), job);
  assert.deepEqual([...afterSelectAll].sort(), ["10", "13"]);
  assert.equal(isAllSelected(afterSelectAll, job), true);

  const afterClearAll = toggleAllSelection(afterSelectAll, job);
  assert.deepEqual([...afterClearAll], []);
  assert.equal(isAllSelected(afterClearAll, job), false);

  const emptyJob = normalizeJob({ ...fixture, entries: [{ id: "11", title: "Done", status: "already_imported" }] });
  assert.equal(isAllSelected(new Set(), emptyJob), false);
});

test("supports Cookidoo provider normalization, progress text, and connection guidance", () => {
  const cookidooJob = normalizeJob({
    job_id: "job-cookidoo-1",
    provider: "cookidoo",
    status: "previewing",
    kinds: ["custom", "managed"],
    entries: [{ id: "c-1", provider: "cookidoo", title: "Risotto", status: "pending" }],
    pages_read: 2,
    processed: 0,
    total: 1,
  });
  assert.equal(cookidooJob.provider, "cookidoo");
  assert.deepEqual(cookidooJob.kinds, ["custom", "managed"]);
  assert.equal(progress(cookidooJob), "Hentet 2 Cookidoo-sider · 1 oppskrifter funnet …");
  assert.equal(progress({ ...cookidooJob, pages_read: 0 }), "Henter oppskrifter fra Cookidoo …");

  assert.equal(connectionGuidance({ status: "not_configured" }), "setup_required");
  assert.equal(connectionGuidance({ status: "configured" }), "connect_required");
  assert.equal(connectionGuidance({ status: "expired" }), "reconnect_required");
  assert.match(connectionMessage("connect_required", "cookidoo"), /Cookidoo-kontoen er ikke tilkoblet/u);
});

class DomNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.textContent = text == null ? "" : String(text);
    this.className = className || "";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.value = "";
    this.type = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.parentNode = null;
  }
  append(...children) {
    children.forEach((child) => {
      if (!child) return;
      child.parentNode = this;
      this.children.push(child);
    });
  }
  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this.append(...children);
  }
  replaceWith(replacement) {
    if (!this.parentNode) return;
    const idx = this.parentNode.children.indexOf(this);
    if (idx >= 0) {
      replacement.parentNode = this.parentNode;
      this.parentNode.children.splice(idx, 1, replacement);
      this.parentNode = null;
    }
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this, currentTarget: this })); }
  dispatchEvent(type, extra = {}) {
    (this.listeners[type] || []).forEach((fn) => fn({ target: this, currentTarget: this, preventDefault: () => {}, ...extra }));
  }
  focus() { this.focused = true; }
  get isConnected() {
    if (this.parentNode) return this.parentNode.isConnected;
    return this.connected === true;
  }
  findAll(predicate, acc = []) {
    if (predicate(this)) acc.push(this);
    this.children.forEach((child) => child.findAll?.(predicate, acc));
    return acc;
  }
  querySelectorAll(selector) {
    if (selector === 'input[type="checkbox"][data-entry-id]') {
      return this.findAll((n) => n.tagName === "INPUT" && n.type === "checkbox" && n.dataset.entryId !== undefined);
    }
    if (selector.startsWith(".")) {
      const cls = selector.slice(1);
      return this.findAll((n) => n.className.split(/\s+/).includes(cls));
    }
    return this.findAll((n) => n.tagName.toLowerCase() === selector.toLowerCase());
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function buildCollectionsDomHarness({ oda, integrations = [], apiHandler }) {
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
  const startIdx = appSource.indexOf("function recipeCollectionConnectionPrompt(");
  const endIdx = appSource.indexOf("\nfunction importForm(", startIdx);
  assert.ok(startIdx >= 0 && endIdx > startIdx, "recipeCollectionForm source located");
  const fnSource = appSource.slice(startIdx, endIdx);

  const modal = { open: false, dataset: {}, showModal() { this.open = true; }, close() { this.open = false; } };
  const root = new DomNode("div");
  root.connected = true;
  const apiCalls = [];
  const state = {
    oda,
    integrations,
    recipes: [],
    view: "recipes",
    recipeCollectionJob: null,
    recipeCollectionPollGeneration: 0,
  };
  const context = {
    state,
    window: {
      KitchenRecipeCollections,
      KitchenSettingsView: { odaNeedsReconnect: () => false },
    },
    document: { activeElement: null },
    CSS: { escape: (v) => String(v) },
    URL,
    setTimeout: () => 0,
    clearTimeout: () => {},
    $: (sel) => (sel === "#modal" ? modal : root),
    openDialog: (headingText) => {
      modal.showModal();
      root.replaceChildren(new DomNode("h2", headingText));
      return root;
    },
    el: (tag, text = "", className = "") => new DomNode(tag, text, className),
    add: (parent, ...children) => {
      children.filter(Boolean).forEach((child) => parent.append(child));
      return parent;
    },
    button: (text, action, className = "") => {
      const node = new DomNode("button", text, className);
      node.type = "button";
      node.addEventListener("click", action);
      return node;
    },
    empty: (heading, desc) => {
      const box = new DomNode("div", "", "empty");
      box.append(new DomNode("h2", heading), new DomNode("p", desc));
      return box;
    },
    connectionSettingsButton: (label) => new DomNode("button", label, "settings-link"),
    rememberRecipeCollectionJob: (job) => { state.recipeCollectionJob = job; },
    loadConnectionStatus: async () => {},
    refreshView: async () => {},
    recipeImage: () => new DomNode("img"),
    formatUiCount: String,
    notify: () => {},
    api: async (reqPath, method = "GET", body) => {
      apiCalls.push({ path: reqPath, method, body });
      return apiHandler(reqPath, method, body);
    },
  };
  vm.runInNewContext(fnSource, context);
  return { root, state, context, apiCalls };
}

test("DOM: recipeCollectionForm provides unified Oda / Cookidoo tabs and sends provider-specific preview requests", async () => {
  const harness = buildCollectionsDomHarness({
    oda: { configured: true, connected: false },
    integrations: [{ id: "cookidoo", status: "connected" }],
    apiHandler: async (reqPath, method, body) => {
      if (reqPath === "/recipe-collections" && method === "POST") {
        return {
          job_id: "cookidoo-job-1",
          provider: body.provider || "oda",
          status: "ready",
          kinds: body.kinds,
          entries: [
            { id: "c1", provider: "cookidoo", title: "Kremet pasta", status: "pending", badges: [] },
          ],
          pages_read: 1,
          processed: 0,
          total: 1,
          truncated: false,
          truncated_kinds: [],
          errors: [],
        };
      }
      throw new Error(`Unexpected API call: ${method} ${reqPath}`);
    },
  });

  harness.context.recipeCollectionForm();

  const tabs = harness.root.querySelectorAll(".provider-tab");
  assert.equal(tabs.length, 2);
  assert.deepEqual(tabs.map((t) => t.textContent), ["Oda", "Cookidoo"]);
  assert.equal(tabs[0].getAttribute("aria-selected"), "true");
  assert.equal(tabs[1].getAttribute("aria-selected"), "false");

  // Oda is disconnected, so Oda connection prompt is shown while Cookidoo tab remains clickable
  assert.ok(harness.root.querySelector(".connection-prompt"));

  // Switch to Cookidoo
  tabs[1].click();
  assert.equal(tabs[0].getAttribute("aria-selected"), "false");
  assert.equal(tabs[1].getAttribute("aria-selected"), "true");
  assert.equal(harness.root.querySelector("legend").textContent, "Cookidoo-samlinger");

  const previewBtn = harness.root.findAll((n) => n.tagName === "BUTTON" && n.dataset.collectionFocus === "preview")[0];
  assert.equal(previewBtn.textContent, "Hent Cookidoo-samlinger");
  previewBtn.click();
  await new Promise((r) => setTimeout(r, 0));

  assert.equal(harness.apiCalls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(harness.apiCalls[0])), {
    path: "/recipe-collections",
    method: "POST",
    body: { provider: "cookidoo", kinds: ["custom", "managed"] },
  });
  assert.equal(harness.root.querySelectorAll(".collection-entry").length, 1);
});

test("DOM: recipeCollectionForm toggles Velg alle / Fjern alle for selectable entries only", async () => {
  const harness = buildCollectionsDomHarness({
    oda: { configured: true, connected: true },
    apiHandler: async (reqPath, method, body) => {
      if (reqPath === "/recipe-collections" && method === "POST") {
        return {
          ...fixture,
          entries: [
            { id: "10", provider: "oda", title: "Fiskesuppe", badges: ["Liked on Oda"], status: "pending" },
            { id: "11", provider: "oda", title: "Pannekaker", badges: [], status: "already_imported" },
            { id: "13", provider: "oda", title: "Lasagne", badges: [], status: "failed" },
          ],
          total: 3,
        };
      }
      if (reqPath === "/recipe-collections/job-1/import" && method === "POST") {
        return {
          ...fixture,
          status: "importing",
          processed: 0,
          total: body.selected_ids.length,
        };
      }
      throw new Error(`Unexpected API call: ${method} ${reqPath}`);
    },
  });

  harness.context.recipeCollectionForm();
  const previewBtn = harness.root.findAll((n) => n.tagName === "BUTTON" && n.dataset.collectionFocus === "preview")[0];
  previewBtn.click();
  await new Promise((r) => setTimeout(r, 0));

  const toolbar = harness.root.querySelector(".selection-toolbar");
  assert.ok(toolbar);
  assert.equal(toolbar.hidden, false);

  const toggleBtn = harness.root.findAll((n) => n.tagName === "BUTTON" && n.dataset.collectionFocus === "toggle-all")[0];
  const importBtn = harness.root.findAll((n) => n.tagName === "BUTTON" && n.textContent === "Bekreft og importer valgte oppskrifter")[0];
  const entryBoxes = harness.root.querySelectorAll('input[type="checkbox"][data-entry-id]');

  assert.equal(entryBoxes.length, 2);
  assert.equal(toggleBtn.textContent, "Velg alle");
  assert.equal(importBtn.disabled, true);

  // Click "Velg alle"
  toggleBtn.click();
  assert.equal(toggleBtn.textContent, "Fjern alle");
  assert.equal(importBtn.disabled, false);
  assert.deepEqual(entryBoxes.map((b) => b.checked), [true, true]);
  assert.equal(toolbar.querySelector("p").textContent, "2 valgte oppskrifter");

  // Uncheck one entry manually -> reverts button label to "Velg alle"
  entryBoxes[0].checked = false;
  entryBoxes[0].dispatchEvent("change");
  assert.equal(toggleBtn.textContent, "Velg alle");
  assert.equal(toolbar.querySelector("p").textContent, "1 valgte oppskrifter");

  // Click "Velg alle" again -> selects remaining, then click "Fjern alle" -> clears all
  toggleBtn.click();
  assert.equal(toggleBtn.textContent, "Fjern alle");
  assert.deepEqual(entryBoxes.map((b) => b.checked), [true, true]);

  toggleBtn.click();
  assert.equal(toggleBtn.textContent, "Velg alle");
  assert.deepEqual(entryBoxes.map((b) => b.checked), [false, false]);
  assert.equal(importBtn.disabled, true);
  assert.equal(toolbar.querySelector("p").textContent, "0 valgte oppskrifter");
});
