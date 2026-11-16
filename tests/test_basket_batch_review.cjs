"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
function functionSource(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([\\s\\S]*?\\n\\}`, "u"));
  assert.ok(match, `${name} is defined`);
  return match[0];
}

class Node {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.disabled = false;
    this.dataset = {};
  }
  append(child) { this.children.push(child); }
  setAttribute(key, value) { this.attributes[key] = value; }
  get text() { return this.textContent + " " + this.children.map((child) => child.text || "").join(" "); }
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) {
      const found = child.find?.(predicate);
      if (found) return found;
    }
    return null;
  }
}

function fixture({ api } = {}) {
  const safeNeed = { id: "need-safe", name: "Yoghurt", quantity: "2", unit: "beger" };
  const unclearNeed = { id: "need-unclear", name: "Tomat", quantity: "3", unit: "stk" };
  const safeCandidate = {
    id: "oda:101",
    provider: "oda",
    product_id: "101",
    name: "Yoghurt 500 g",
    pack_quantity: "1",
    pack_unit: "beger",
    recommended_packages: 2,
    recommended_fulfillment_quantity: "2",
    available: true,
  };
  const saved = {
    need: safeNeed,
    selections: [
      { product: { name: "Saved Yoghurt", product_id: "old-1" }, packages: 1 },
      { product: { name: "Second Saved Yoghurt", product_id: "old-2" }, packages: 1 },
    ],
  };
  const state = {
    oda: { connected: true },
    basket: { needs: [saved, { need: unclearNeed, selections: [] }] },
    basketMatchPages: {
      [safeNeed.id]: { 1: { match_job_id: "job-current", status: "matched", candidates: [safeCandidate] } },
      [unclearNeed.id]: { 1: { match_job_id: "job-current", status: "matched", candidates: [{ id: "oda:202", available: true }] } },
    },
    basketMatchBusy: {},
    basketMatchErrors: {},
    basketBatchReviewJobId: "job-current",
    basketBatchChoices: {},
    basketBatchSaving: false,
    basketBatchSaveError: "",
    basketBatchNeedsRefresh: false,
    basketBatchNeedsCorrection: false,
    basketMatchPlanEpoch: 1,
    cartProposalHandled: false,
  };
  const requests = [];
  const notifications = [];
  const modal = {
    open: false,
    dataset: {},
    close() { this.open = false; },
  };
  const context = {
    state,
    window: {
      KitchenBasketMatchView: {
        selectionRecommendation: (candidate, unit) => candidate.recommended_packages && candidate.pack_unit === unit
          ? { packages: candidate.recommended_packages, fulfillment: candidate.recommended_fulfillment_quantity }
          : null,
        formatNeedQuantity: (need) => `${need.quantity} ${need.unit}`,
        statusLabel: (status) => status,
      },
      KitchenShoppingView: { formatQuantityForDisplay: (value) => String(value) },
    },
    currentBasketMatchNeeds: () => [safeNeed, unclearNeed],
    openDialog: () => {
      modal.open = true;
      modal.dataset = {};
      modal.root = new Node("dialog-content");
      return modal.root;
    },
    $(selector) { assert.equal(selector, "#modal"); return modal; },
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => {
      const node = new Node("button", text, className);
      node.action = action;
      return node;
    },
    basketProduct: (product, packages) => new Node("article", `${product.name} ${packages || ""}`),
    renderBasketMatchCandidate: (candidate) => new Node("article", candidate.name || "unlabelled candidate"),
    formatUiCount: String,
    api: async (...args) => { requests.push(args); return api ? api(...args) : {}; },
    currentBasketMatchDates: () => ({ start: "2026-11-16", end: "2026-11-22" }),
    notify: (message) => notifications.push(message),
    render: () => {},
    refreshView: async () => {},
    startBasketMatchJob: async () => {},
  };
  const functions = [
    "basketBatchRecommendation",
    "basketBatchSkipReason",
    "toggleBasketBatchChoice",
    "openBasketBatchReview",
    "saveBasketBatchChoices",
  ].map(functionSource).join("\n");
  vm.runInNewContext(functions, context);
  return { context, state, modal, requests, notifications, safeNeed, unclearNeed, safeCandidate };
}

test("opening and closing review makes no write, shows retained choices and unresolved reason", () => {
  const value = fixture();
  value.context.openBasketBatchReview();
  assert.equal(value.requests.length, 0);
  assert.match(value.modal.root.text, /Saved Yoghurt/u);
  assert.match(value.modal.root.text, /Second Saved Yoghurt/u);
  assert.match(value.modal.root.text, /Pakningsstørrelse/u);
  assert.match(value.modal.root.text, /Uavklarte varer kan stå over/u);
  value.modal.close();
  assert.equal(value.requests.length, 0);
  assert.equal(value.state.basketBatchChoices["need-safe"], undefined);
});

test("selecting a recommendation stays local until one explicit batch approval", async () => {
  const value = fixture();
  value.context.openBasketBatchReview();
  const select = value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Erstatt lagrede valg med dette");
  assert.ok(select);
  await select.action();
  assert.equal(value.requests.length, 0);
  assert.equal(value.state.basketBatchChoices["need-safe"].candidate_id, "oda:101");
  assert.match(value.modal.root.text, /vil erstatte 2 lagrede valg/u);
  const approve = value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Bruk 1 produktforslag");
  assert.ok(approve);
  await value.context.saveBasketBatchChoices();
  assert.equal(value.requests.length, 1);
  assert.equal(value.requests[0][0], "/basket/selections/batch");
  assert.equal(value.requests[0][1], "PUT");
  assert.deepEqual(JSON.parse(JSON.stringify(value.requests[0][2])), {
    start: "2026-11-16",
    end: "2026-11-22",
    job_id: "job-current",
    selections: [{ need_id: "need-safe", candidate_id: "oda:101", packages: 2 }],
  });
  assert.equal(value.state.basketBatchChoices["need-safe"], undefined);
});

test("a stale-job response preserves staged choices and requires explicit re-review", async () => {
  const value = fixture({ api: async () => {
    const error = new Error("Product matches expired. Refresh matches.");
    error.status = 409;
    throw error;
  } });
  value.state.basketBatchChoices["need-safe"] = {
    need_id: "need-safe", candidate_id: "oda:101", packages: 2,
    name: "Yoghurt 500 g", match_job_id: "job-current",
  };
  await value.context.saveBasketBatchChoices();
  assert.equal(value.requests.length, 1);
  assert.equal(value.state.basketBatchChoices["need-safe"].candidate_id, "oda:101");
  assert.equal(value.state.basketBatchNeedsRefresh, true);
  assert.match(value.modal.root.text, /Oppdater dem og gjennomgå forslagene på nytt/u);
});

test("422 keeps choices but blocks identical retry until a correction", async () => {
  const value = fixture({ api: async () => {
    const error = new Error("selections.0.packages: Input should be less than or equal to 99");
    error.status = 422;
    error.detail = "body.selections.0.packages: Input should be less than or equal to 99";
    throw error;
  } });
  value.state.basketBatchChoices["need-safe"] = {
    need_id: "need-safe", candidate_id: "oda:101", packages: 2,
    name: "Yoghurt 500 g", match_job_id: "job-current",
  };
  await value.context.saveBasketBatchChoices();
  assert.equal(value.state.basketBatchChoices["need-safe"].candidate_id, "oda:101");
  assert.equal(value.state.basketBatchNeedsCorrection, true);
  assert.match(value.state.basketBatchSaveError, /Yoghurt/u);
  assert.equal(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Bruk 1 produktforslag").disabled, true);
});

test("a network error keeps the exact request available for an idempotent retry", async () => {
  let attempt = 0;
  const value = fixture({ api: async () => {
    attempt += 1;
    if (attempt === 1) throw new Error("Network unavailable");
    return { needs: [] };
  } });
  value.state.basketBatchChoices["need-safe"] = {
    need_id: "need-safe", candidate_id: "oda:101", packages: 2,
    name: "Yoghurt 500 g", match_job_id: "job-current",
  };
  await value.context.saveBasketBatchChoices();
  assert.equal(value.state.basketBatchChoices["need-safe"].candidate_id, "oda:101");
  assert.equal(value.state.basketBatchNeedsRefresh, false);
  assert.equal(value.state.basketBatchNeedsCorrection, false);
  await value.context.saveBasketBatchChoices();
  assert.equal(value.requests.length, 2);
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.requests[0][2])),
    JSON.parse(JSON.stringify(value.requests[1][2])),
  );
  assert.equal(value.state.basketBatchChoices["need-safe"], undefined);
});

test("a late response for an old period cannot clear choices from the new period", async () => {
  let resolveApi;
  const value = fixture({ api: () => new Promise((resolve) => { resolveApi = resolve; }) });
  value.state.basketBatchChoices["need-safe"] = {
    need_id: "need-safe", candidate_id: "oda:101", packages: 2,
    name: "Yoghurt 500 g", match_job_id: "job-current",
  };
  const saving = value.context.saveBasketBatchChoices();
  value.state.basketMatchPlanEpoch += 1;
  value.state.basketBatchChoices = { "need-new": { need_id: "need-new", candidate_id: "oda:303" } };
  value.state.basketBatchSaving = false;
  resolveApi({ needs: [] });
  await saving;
  assert.equal(value.state.basketBatchChoices["need-new"].candidate_id, "oda:303");
  assert.equal(value.state.basketBatchChoices["need-safe"], undefined);
  assert.match(value.notifications[0], /2026-11-16/u);
});
