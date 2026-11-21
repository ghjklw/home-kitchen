"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const shoppingView = require("../src/home_kitchen/static/shopping-view.js");

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
    this.ownerDocument = null;
  }
  append(child) { this.children.push(child); }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  contains(node) { return allNodes(this).includes(node); }
  querySelectorAll(selector) {
    assert.equal(selector, "[data-focus-key]");
    return allNodes(this).filter((node) => node.dataset?.focusKey);
  }
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
    identity_status: "compatible",
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
    basketMatchObservedAt: {},
    basketMatchCurrentPage: {},
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
  const document = { activeElement: null };
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
        identityStatus: (candidate) => candidate?.identity_status === "compatible" ? "compatible" : "unverified",
        formatNeedQuantity: (need) => `${need.quantity} ${need.unit}`,
        statusLabel: (status) => status,
      },
      KitchenShoppingView: {
        formatQuantityForDisplay: (value) => String(value),
        formatQuantityForAccessibility: (value) => String(value),
        displayUnit: (...args) => shoppingView.displayUnit(...args),
      },
    },
    formatUiNeedQuantity: (need) => `${need.quantity} ${need.unit}`,
    currentBasketMatchNeeds: () => [safeNeed, unclearNeed],
    openDialog: () => {
      modal.open = true;
      modal.dataset = {};
      modal.root = new Node("dialog-content");
      modal.root.ownerDocument = document;
      return modal.root;
    },
    $(selector) {
      if (selector === "#modal") return modal;
      if (selector === "#modal-content") return modal.root;
      throw new Error(`Unexpected selector ${selector}`);
    },
    document,
    el: (tag, text = "", className = "") => {
      const node = new Node(tag, text, className);
      node.ownerDocument = document;
      return node;
    },
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => {
      const node = new Node("button", text, className);
      node.action = action;
      node.ownerDocument = document;
      return node;
    },
    basketProduct: (product, packages) => new Node("article", `${product.name} ${packages || ""}`),
    renderBasketMatchCandidate: undefined,
    safeImageUrl: () => null,
    recipeImage: (recipe, className) => new Node("img", recipe.title, className),
    officialOdaProductUrl: () => null,
    formatUiQuantity: String,
    toggleBasketBatchChoice: () => {},
    formatUiCount: String,
    api: async (...args) => { requests.push(args); return api ? api(...args) : {}; },
    currentBasketMatchDates: () => ({ start: "2026-11-16", end: "2026-11-22" }),
    range: () => "?start=2026-11-16&end=2026-11-22",
    notify: (message) => notifications.push(message),
    render: () => {},
    refreshView: async () => {},
    startBasketMatchJob: async () => {},
    showBasketMatchPage: () => {},
    dateLabel: () => "Nov 16, 2026",
  };
  const functions = [
    "basketBatchRecommendation",
    "basketBatchIdentityStatus",
    "basketBatchSelectableCandidate",
    "clearStaleBasketBatchIdentityConfirmations",
    "basketBatchSkipReason",
    "toggleBasketBatchChoice",
    "renderBasketMatchCandidate",
    "renderBasketMatchSummary",
    "openBasketBatchReview",
    "renderBasketBatchReview",
    "renderBasketMatchNeed",
    "saveBasketBatchChoices",
  ].map(functionSource).join("\n");
  vm.runInNewContext(functions, context);
  return { context, state, modal, requests, notifications, document, safeNeed, unclearNeed, safeCandidate };
}

test("opening and closing review makes no write, shows retained choices and unresolved reason", () => {
  const value = fixture();
  value.context.openBasketBatchReview();
  assert.equal(value.requests.length, 0);
  assert.match(value.modal.root.text, /Saved Yoghurt/u);
  assert.match(value.modal.root.text, /Second Saved Yoghurt/u);
  assert.match(value.modal.root.text, /Pakningsstørrelse/u);
  assert.match(value.modal.root.text, /Uavklarte varer kan stå over/u);
  assert.match(value.modal.root.text, /Yoghurt 500 g/u);
  assert.match(value.modal.root.text, /Kan stå over/u);
  assert.ok(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Bruk 0 produktforslag"));
  assert.ok(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Erstatt lagrede valg med dette"));
  assert.equal(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Velg til samlet godkjenning"), null);
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
    selections: [{ need_id: "need-safe", candidate_id: "oda:101", packages: 2, confirm_identity: false }],
  });
  assert.equal(value.state.basketBatchChoices["need-safe"], undefined);
});

test("an unverified product requires explicit identity confirmation before batch selection", async () => {
  const value = fixture();
  const candidate = {
    ...value.safeCandidate,
    id: "oda:unverified-yoghurt",
    identity_status: "unverified",
    name: "Yoghurt uten bekreftet identitet",
  };
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [candidate];
  value.context.openBasketBatchReview();
  const confirm = value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Dette er riktig produkt");
  assert.ok(confirm);
  confirm.action();
  assert.equal(value.requests.length, 0);
  assert.equal(value.state.basketBatchChoices[value.safeNeed.id].confirm_identity, true);
  assert.match(value.modal.root.text, /Du har bekreftet at dette er riktig produkt/u);
  await value.context.saveBasketBatchChoices();
  assert.equal(value.requests.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(value.requests[0][2].selections)), [{
    need_id: "need-safe",
    candidate_id: "oda:unverified-yoghurt",
    packages: 2,
    confirm_identity: true,
  }]);
});

test("identity confirmation keeps focus on the updated candidate control", () => {
  const value = fixture();
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [{
    ...value.safeCandidate,
    id: "oda:focus-check",
    identity_status: "unverified",
  }];
  value.context.openBasketBatchReview();
  const confirm = value.modal.root.find((node) =>
    node.tagName === "button" && node.textContent === "Dette er riktig produkt",
  );
  confirm.focus();
  confirm.action();

  const updated = value.modal.root.find((node) =>
    node.tagName === "button" && node.textContent === "Bekreftelsen er valgt",
  );
  assert.ok(updated);
  assert.equal(value.document.activeElement, updated);
  assert.equal(updated.attributes["aria-pressed"], "true");
  assert.equal(updated.focused, true);
});

test("identity confirmation does not restore focus when the active element is outside its modal", () => {
  const value = fixture();
  const candidate = {
    ...value.safeCandidate,
    id: "oda:external-focus-check",
    identity_status: "unverified",
  };
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [candidate];
  value.context.openBasketBatchReview();
  const outside = new Node("button", "Other context");
  outside.ownerDocument = value.document;
  outside.focus();

  value.context.toggleBasketBatchChoice(candidate, value.safeNeed, "job-current", true);

  const updated = value.modal.root.find((node) =>
    node.tagName === "button" && node.textContent === "Bekreftelsen er valgt",
  );
  assert.ok(updated);
  assert.equal(value.document.activeElement, outside);
  assert.notEqual(updated.focused, true);
});

test("identity confirmation cannot make a candidate with unknown package facts selectable", () => {
  const value = fixture();
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [{
    id: "oda:unverified-no-pack",
    identity_status: "unverified",
    available: true,
    name: "Produkt uten pakningsstørrelse",
  }];
  value.context.openBasketBatchReview();
  assert.equal(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Dette er riktig produkt"), null);
  assert.match(value.modal.root.text, /Pakningsstørrelse, måleenhet/u);
  assert.equal(value.state.basketBatchChoices[value.safeNeed.id], undefined);
  assert.equal(value.requests.length, 0);
});

test("changing shopping period invalidates a staged identity confirmation", async () => {
  const value = fixture();
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [{
    ...value.safeCandidate,
    identity_status: "unverified",
  }];
  value.context.openBasketBatchReview();
  value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Dette er riktig produkt").action();
  value.context.currentBasketMatchDates = () => ({ start: "2026-11-23", end: "2026-11-29" });
  await value.context.saveBasketBatchChoices();
  assert.equal(value.requests.length, 0);
  assert.equal(value.state.basketBatchChoices[value.safeNeed.id].confirm_identity, false);
  value.context.openBasketBatchReview();
  assert.match(value.modal.root.text, /Tidligere valgt/u);
  assert.equal(value.modal.root.find((node) => node.tagName === "button" && node.textContent === "Bruk 0 produktforslag").disabled, true);
});

test("a stale-job response preserves staged choices and requires explicit re-review", async () => {
  const value = fixture({ api: async () => {
    const error = new Error("Product matches expired. Refresh matches.");
    error.status = 409;
    throw error;
  } });
  value.state.basketBatchChoices["need-safe"] = {
    need_id: "need-safe", candidate_id: "oda:101", packages: 2,
    name: "Yoghurt 500 g", match_job_id: "job-current", unit: "beger", start: "2026-11-16", end: "2026-11-22",
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
    name: "Yoghurt 500 g", match_job_id: "job-current", unit: "beger", start: "2026-11-16", end: "2026-11-22",
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
    name: "Yoghurt 500 g", match_job_id: "job-current", unit: "beger", start: "2026-11-16", end: "2026-11-22",
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
    name: "Yoghurt 500 g", match_job_id: "job-current", unit: "beger", start: "2026-11-16", end: "2026-11-22",
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

function allNodes(node) {
  return [node, ...node.children.flatMap(allNodes)];
}

function addAlternatives(value) {
  const candidates = value.state.basketMatchPages[value.safeNeed.id][1].candidates;
  candidates.unshift({ id: "oda:bad-first", name: "Unclear first", available: true });
  candidates.push(
    { ...value.safeCandidate, id: "oda:102", name: "Alternative yoghurt" },
    { ...value.safeCandidate, id: "oda:103", name: "Third yoghurt" },
    { ...value.safeCandidate, id: "oda:unavailable", name: "Unavailable yoghurt", available: false },
  );
  value.state.basketMatchPages[value.unclearNeed.id][1].candidates.push(
    { id: "oda:203", name: "Another unclear tomato", available: true },
    { id: "oda:204", name: "Unavailable tomato", available: false },
  );
}

test("collective summary counts actionable needs once and reports eligible alternatives separately", () => {
  const value = fixture();
  const needs = [];
  for (let index = 0; index < 25; index += 1) {
    const need = { id: `need-${index}`, name: `Vare ${index}`, quantity: "250", unit: "ml" };
    needs.push(need);
    const count = index < 10 ? 3 : index < 18 ? 4 : 0;
    const candidates = Array.from({ length: count }, (_, candidateIndex) => ({
      id: `oda:${index}:${candidateIndex}`,
      name: `Produkt ${index}:${candidateIndex}`,
      pack_unit: "ml",
      identity_status: "compatible",
      recommended_packages: 1,
      recommended_fulfillment_quantity: "250",
      available: true,
    }));
    value.state.basketMatchPages[need.id] = {
      1: { match_job_id: "job-current", status: count ? "matched" : "no_results", candidates },
    };
  }
  value.context.currentBasketMatchNeeds = () => needs;
  const root = new Node("main");
  value.context.renderBasketBatchReview(root);
  assert.match(root.text, /18 varer har forslag som kan velges direkte/u);
  assert.match(root.text, /7 varer må stå over eller avklares/u);
  assert.match(root.text, /44 ekstra valgbare alternativer/u);
  assert.equal(value.requests.length, 0);
  assert.deepEqual(Object.keys(value.state.basketBatchChoices), []);
});

test("collective review shows one eligible primary and collapsed alternatives per need", () => {
  const value = fixture();
  addAlternatives(value);
  value.context.openBasketBatchReview();
  const sections = allNodes(value.modal.root).filter((node) => node.className === "basket-batch-need");
  const primary = sections[0].children.filter((node) => node.className === "basket-batch-option");
  assert.equal(primary.length, 1);
  assert.match(primary[0].text, /Yoghurt 500 g/u);
  const alternatives = sections[0].find((node) => node.className === "basket-batch-alternatives");
  assert.ok(alternatives);
  assert.ok(!alternatives.open);
  assert.match(alternatives.text, /Flere treff \(4\)/u);
  const unresolved = sections[1];
  assert.equal((unresolved.text.match(/Kan stå over:/gu) || []).length, 1);
  assert.equal(unresolved.children.filter((node) => node.className === "basket-batch-option").length, 0);
  const unresolvedMatches = unresolved.find((node) => node.className === "basket-batch-alternatives");
  assert.ok(!unresolvedMatches.open);
  assert.match(unresolvedMatches.text, /Vis uavklarte treff \(3\)/u);
  assert.equal(allNodes(unresolved).filter((node) => node.attributes["aria-pressed"] !== undefined).length, 0);
  assert.equal(value.requests.length, 0);
  assert.deepEqual(Object.keys(value.state.basketBatchChoices), []);
  assert.equal(value.state.basket.needs[0].selections.length, 2);
});

test("selecting an alternative promotes it to the primary without saving", () => {
  const value = fixture();
  addAlternatives(value);
  value.context.openBasketBatchReview();
  let alternatives = value.modal.root.find((node) => node.className === "basket-batch-alternatives");
  alternatives.open = true;
  const option = alternatives.find((node) => node.className === "basket-batch-option" && node.text.includes("Alternative yoghurt"));
  option.find((node) => node.tagName === "button").action();
  assert.equal(value.state.basketBatchChoices[value.safeNeed.id].candidate_id, "oda:102");
  assert.equal(Object.keys(value.state.basketBatchChoices).length, 1);
  const section = value.modal.root.find((node) => node.className === "basket-batch-need");
  const primary = section.children.find((node) => node.className === "basket-batch-option");
  assert.match(primary.text, /Alternative yoghurt/u);
  assert.equal(primary.find((node) => node.tagName === "button").attributes["aria-pressed"], "true");
  assert.equal(value.requests.length, 0);
  assert.equal(value.state.basket.needs[0].selections.length, 2);
});

test("busy and stale matches do not count as actionable needs", () => {
  const value = fixture();
  addAlternatives(value);
  value.state.basketMatchBusy[value.safeNeed.id] = { page: 1 };
  let root = new Node("main");
  value.context.renderBasketBatchReview(root);
  assert.match(root.text, /0 varer har forslag som kan velges direkte/u);
  assert.match(root.text, /2 varer må stå over eller avklares/u);
  value.state.basketBatchNeedsRefresh = true;
  root = new Node("main");
  value.context.renderBasketBatchReview(root);
  assert.match(root.text, /0 varer har forslag som kan velges direkte/u);
  assert.doesNotMatch(root.text, /ekstra valgbare alternativer/u);
  assert.equal(value.requests.length, 0);
});

test("provider pagination detail renders in Bokmål without mutating provider evidence", () => {
  const value = fixture();
  const result = value.state.basketMatchPages[value.safeNeed.id][1];
  result.detail = "More provider results are available on the next page.";
  result.has_more = true;
  const root = value.context.renderBasketMatchNeed(value.safeNeed);
  assert.match(root.text, /Flere treff fra leverandøren er tilgjengelige på neste side/u);
  assert.doesNotMatch(root.text, /More provider results/u);
  assert.equal(result.detail, "More provider results are available on the next page.");
  assert.equal(value.requests.length, 0);
});

test("candidate package size without recommendation displays localized unit", () => {
  const value = fixture();
  const candidate = {
    id: "oda:201",
    name: "Yoghurt Beger",
    pack_quantity: "1",
    pack_unit: "item",
    recommended_packages: null,
    recommended_fulfillment_quantity: null,
  };
  const rendered = value.context.renderBasketMatchCandidate(candidate, value.safeNeed);
  assert.match(rendered.text, /Pakke 1 stk\. · antall er ikke beregnet/u);
});

test("opening batch review with a target need targets that need section", () => {
  const value = fixture();
  value.context.openBasketBatchReview(value.safeNeed.id);
  const sections = allNodes(value.modal.root).filter((node) => node.className?.includes("basket-batch-need"));
  const target = sections.find((node) => node.dataset?.needId === value.safeNeed.id);
  assert.ok(target, "targeted need section exists with matching data-need-id");
  assert.match(target.text, /Yoghurt/u);
  const focusable = target.find((node) => ["button", "input", "summary"].includes(node.tagName?.toLowerCase()));
  assert.ok(focusable, "interactive control inside target section exists");
  assert.equal(focusable.focused, true, "interactive control inside target section is focused");
  assert.equal(value.document.activeElement, focusable, "activeElement updated to targeted control");
});

test("batch review allows removing a saved product selection", async () => {
  const value = fixture();
  value.context.openBasketBatchReview();
  const removeButtons = allNodes(value.modal.root).filter(
    (node) => node.tagName === "button" && node.textContent === "Fjern produktvalg",
  );
  assert.ok(removeButtons.length > 0, "has remove buttons for saved selections");
  await removeButtons[0].action();
  assert.equal(value.requests.length, 1);
  assert.equal(value.requests[0][0], "/basket/selections/need-safe/old-1?start=2026-11-16&end=2026-11-22");
  assert.equal(value.requests[0][1], "DELETE");
});

test("batch review and need card expose Bokmål query refine controls and retry ignores stale responses", async () => {
  const matchView = require("../src/home_kitchen/static/basket-match-view.js");
  const resolvers = [];
  const requests = [];
  const safeNeed = { id: "need-safe", name: "Yoghurt", quantity: "2", unit: "beger" };
  const state = {
    oda: { connected: true },
    view: "shopping",
    shoppingPanel: "oda",
    basket: { needs: [{ need: safeNeed, selections: [] }] },
    basketMatchPlanKey: matchView.planKey("2026-11-16", "2026-11-22", [safeNeed]),
    basketMatchPlanEpoch: 1,
    basketMatchNeedVersions: { [safeNeed.id]: 0 },
    basketMatchPages: {
      [safeNeed.id]: { 1: { match_job_id: "job-1", status: "matched", search_query: "Yoghurt", candidates: [] } },
    },
    basketMatchCurrentPage: { [safeNeed.id]: 1 },
    basketMatchBusy: {},
    basketMatchErrors: {},
    basketMatchObservedAt: {},
    basketMatchQueries: {},
    basketMatchJobs: {},
    basketMatchLatestJobId: "job-1",
    basketBatchReviewJobId: "job-1",
    basketBatchChoices: {},
    basketBatchSaving: false,
    basketBatchSaveError: "",
    basketBatchNeedsRefresh: false,
    basketBatchNeedsCorrection: false,
    basketMatchStartSequence: 0,
  };
  const modal = { open: false, dataset: {}, close() { this.open = false; } };
  const document = { activeElement: null };
  const context = {
    state,
    navigator: { onLine: true },
    window: {
      KitchenBasketMatchView: matchView,
      KitchenShoppingView: {
        formatQuantityForDisplay: String,
        formatQuantityForAccessibility: String,
        displayUnit: (...args) => shoppingView.displayUnit(...args),
      },
    },
    formatUiNeedQuantity: (need) => `${need.quantity} ${need.unit}`,
    currentBasketMatchNeeds: () => [safeNeed],
    currentBasketMatchDates: () => ({ start: "2026-11-16", end: "2026-11-22" }),
    openDialog: () => {
      modal.open = true;
      modal.dataset = {};
      modal.root = new Node("dialog-content");
      modal.root.ownerDocument = document;
      return modal.root;
    },
    $(selector) {
      if (selector === "#modal") return modal;
      if (selector === "#modal-content") return modal.root;
      throw new Error(`Unexpected selector ${selector}`);
    },
    document,
    el: (tag, text = "", className = "") => {
      const node = new Node(tag, text, className);
      node.ownerDocument = document;
      return node;
    },
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => {
      const node = new Node("button", text, className);
      node.action = action;
      node.ownerDocument = document;
      return node;
    },
    basketProduct: (product, packages) => new Node("article", `${product.name} ${packages || ""}`),
    safeImageUrl: () => null,
    recipeImage: (recipe, className) => new Node("img", recipe.title, className),
    officialOdaProductUrl: () => null,
    formatUiQuantity: String,
    formatUiCount: String,
    dateLabel: () => "Nov 16, 2026",
    notify: () => {},
    render: () => {},
    api: (path, method, body) => new Promise((resolve) => {
      requests.push({ path, method, body });
      resolvers.push(resolve);
    }),
  };
  vm.runInNewContext([
    "isCurrentBasketMatchRecord",
    "basketBatchRecommendation",
    "basketBatchIdentityStatus",
    "basketBatchSelectableCandidate",
    "clearStaleBasketBatchIdentityConfirmations",
    "basketBatchSkipReason",
    "toggleBasketBatchChoice",
    "renderBasketMatchCandidate",
    "applyBasketMatchSnapshot",
    "scheduleBasketMatchPoll",
    "retryBasketMatchNeed",
    "openBasketBatchReview",
    "renderBasketMatchNeed",
  ].map(functionSource).join("\n"), context);

  context.openBasketBatchReview();
  const refineInput = modal.root.find((node) => node.tagName === "input");
  const refineButton = modal.root.find((node) => node.tagName === "button" && node.textContent === "Søk på nytt");
  assert.ok(refineInput);
  assert.ok(refineButton);
  assert.equal(refineInput.attributes["aria-label"], "Tilpass søkeord for Yoghurt");

  const firstCall = context.retryBasketMatchNeed(safeNeed.id, "Første søk", { inModal: true });
  const secondCall = context.retryBasketMatchNeed(safeNeed.id, "Andre søk", { inModal: true });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, "/basket/match-jobs/job-1/needs/need-safe/retry");
  assert.equal(requests[1].body.query, "Andre søk");

  // Resolve newer request first
  resolvers[1]({
    job_id: "job-1",
    status: "complete",
    observed_at: "2026-11-20T12:00:00Z",
    needs: [{
      need_id: safeNeed.id,
      page: 1,
      status: "matched",
      search_query: "Andre søk",
      revision: 2,
      candidates: [{ id: "oda:new", name: "Nytt treff", available: true }],
    }],
  });
  await secondCall;
  assert.equal(state.basketMatchPages[safeNeed.id][1].candidates[0].id, "oda:new");

  // Late response from firstCall must be ignored
  resolvers[0]({
    job_id: "job-1",
    status: "complete",
    observed_at: "2026-11-20T11:59:00Z",
    needs: [{
      need_id: safeNeed.id,
      page: 1,
      status: "matched",
      search_query: "Første søk",
      revision: 1,
      candidates: [{ id: "oda:old", name: "Gammelt treff", available: true }],
    }],
  });
  await firstCall;
  assert.equal(state.basketMatchPages[safeNeed.id][1].candidates[0].id, "oda:new");
  assert.equal(state.basketMatchQueries[safeNeed.id], "Andre søk");
});

test("createOdaCartProposal refreshes availability first, surfaces unavailable items, and keeps valid ones", async () => {
  const matchView = require("../src/home_kitchen/static/basket-match-view.js");
  const requests = [];
  const needA = { id: "need-a", name: "Melk", quantity: "1", unit: "l" };
  const needB = { id: "need-b", name: "Egg", quantity: "6", unit: "stk" };
  const state = {
    oda: { connected: true },
    cartProposalLoading: false,
    basketAvailabilityReport: null,
    basketMatchQueries: {},
    basket: {
      needs: [
        {
          need: needA,
          selections: [{ product: { provider: "oda", product_id: "101", name: "Tine Helmelk", price: "29.90" }, packages: 1 }],
        },
        {
          need: needB,
          selections: [{ product: { provider: "oda", product_id: "202", name: "Prior Egg", price: "49.90" }, packages: 1 }],
        },
      ],
    },
  };
  const modal = { open: false, dataset: {}, close() { this.open = false; } };
  const document = { activeElement: null };
  let proposalRendered = null;
  const context = {
    state,
    window: { KitchenBasketMatchView: matchView },
    currentBasketMatchDates: () => ({ start: "2026-11-16", end: "2026-11-22" }),
    range: () => "?start=2026-11-16&end=2026-11-22",
    formatUiCount: String,
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action, className = "") => { const node = new Node("button", text, className); node.action = action; return node; },
    openDialog: () => {
      modal.open = true;
      modal.dataset = {};
      modal.root = new Node("dialog-content");
      modal.root.ownerDocument = document;
      return modal.root;
    },
    $(selector) {
      if (selector === "#modal") return modal;
      if (selector === "#modal-content") return modal.root;
      throw new Error(`Unexpected selector ${selector}`);
    },
    render: () => {},
    notify: () => {},
    renderOdaProposal: (proposal, req) => { proposalRendered = { proposal, req }; },
    api: async (path, method, body) => {
      requests.push({ path, method, body });
      if (path === "/basket/availability-refresh") {
        return {
          checked_at: "2026-11-20T12:00:00Z",
          all_available: false,
          items: [
            {
              need_id: "need-a",
              need_name: "Melk",
              product_id: "101",
              product_name: "Tine Helmelk",
              packages: 1,
              available: true,
              availability_status: "available",
              availability_description: "Oda oppgir at varen er tilgjengelig.",
              previous_price: "29.90",
              current_price: "29.90",
              price_changed: false,
              search_query: "Tine Helmelk",
            },
            {
              need_id: "need-b",
              need_name: "Egg",
              product_id: "202",
              product_name: "Prior Egg",
              packages: 1,
              available: false,
              availability_status: "unavailable",
              availability_description: "Oda oppgir at varen ikke er tilgjengelig.",
              previous_price: "49.90",
              current_price: "49.90",
              price_changed: false,
              search_query: "Prior Egg",
            },
          ],
          unavailable_items: [{ need_id: "need-b", product_id: "202", product_name: "Prior Egg", availability_status: "unavailable" }],
          changed_items: [{ need_id: "need-b", product_id: "202" }],
        };
      }
      if (path === "/oda/cart/proposals") {
        return { proposal_id: "prop-1", status: "pending", additions: [] };
      }
      return {};
    },
  };
  vm.runInNewContext([
    "collectSelectedAvailabilityItems",
    "submitOdaCartProposal",
    "renderBasketAvailabilityReview",
    "createOdaCartProposal",
  ].map(functionSource).join("\n"), context);

  await context.createOdaCartProposal({
    queries: ["Tine Helmelk", "Prior Egg"],
    additions: [{ product_id: 101, quantity: 1 }, { product_id: 202, quantity: 1 }],
  });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/basket/availability-refresh");
  assert.equal(modal.open, true);
  assert.match(modal.root.text, /Prior Egg/u);
  assert.match(modal.root.text, /Ikke tilgjengelig hos Oda/u);
  assert.match(modal.root.text, /1 valgte varer er bekreftet tilgjengelige hos Oda og beholdes/u);

  const proceed = modal.root.find((node) =>
    node.tagName === "button" && node.textContent.includes("Fortsett til Oda-handlekurven med 1 tilgjengelige varer"),
  );
  assert.ok(proceed);
  await proceed.action();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].path, "/oda/cart/proposals");
  assert.deepEqual(JSON.parse(JSON.stringify(requests[1].body.additions)), [{ product_id: 101, quantity: 1 }]);
  assert.ok(proposalRendered);
});

test("issue 135: unverified candidates are not shown as product suggestions in need view, summary, or batch review", () => {
  const value = fixture();
  const unverifiedWithPack = {
    ...value.safeCandidate,
    id: "oda:unverified-item",
    identity_status: "unverified",
    name: "Uavklart produkt",
  };
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [unverifiedWithPack];
  value.state.basketMatchPages[value.unclearNeed.id][1].candidates = [];
  value.state.basketMatchPages[value.unclearNeed.id][1].status = "no_results";

  const summaryPanel = value.context.renderBasketMatchSummary();
  assert.match(summaryPanel.text, /0 av 2 varer har produktforslag/u);

  const needPanel = value.context.renderBasketMatchNeed(value.safeNeed);
  assert.doesNotMatch(needPanel.text, /Foreslåtte produkter/u);
  assert.doesNotMatch(needPanel.text, /Forslagene er klare/u);
  assert.match(needPanel.text, /Ingen sikre produktforslag funnet/u);
  assert.equal(needPanel.children.filter((n) => n.className === "basket-match-candidate").length, 0);
  const disclosure = needPanel.find((n) => n.className === "basket-match-alternatives");
  assert.ok(disclosure);
  assert.match(disclosure.text, /Vis uavklarte treff \(1\)/u);
  assert.ok(needPanel.find((n) => n.className === "basket-match-refine"));

  value.context.openBasketBatchReview();
  const sections = allNodes(value.modal.root).filter((n) => n.className === "basket-batch-need");
  assert.equal(sections[0].children.filter((n) => n.className === "basket-batch-option").length, 0);
  const batchDisclosure = sections[0].find((n) => n.className === "basket-batch-alternatives");
  assert.ok(batchDisclosure);
  assert.match(batchDisclosure.text, /Vis uavklarte treff \(1\)/u);

  // When both unverified and compatible candidates exist, the compatible candidate is suggested first.
  value.state.basketMatchPages[value.safeNeed.id][1].candidates = [unverifiedWithPack, value.safeCandidate];
  const mixedPanel = value.context.renderBasketMatchNeed(value.safeNeed);
  assert.match(mixedPanel.text, /Foreslåtte produkter/u);
  const topCandidate = mixedPanel.children.find((n) => n.className === "basket-match-candidate");
  assert.ok(topCandidate);
  assert.match(topCandidate.text, /Yoghurt 500 g/u);
});
