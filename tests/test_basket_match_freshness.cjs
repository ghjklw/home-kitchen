const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const matchView = require("../src/home_kitchen/static/basket-match-view.js");
const source = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
function sourceFunction(name) {
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
  }
  append(child) { this.children.push(child); }
  setAttribute(key, value) { this.attributes[key] = value; }
  get text() { return this.textContent + this.children.map((child) => child.text).join(" "); }
}
const FIRST = "2026-11-16T10:20:00Z";
const SECOND = "2026-11-16T11:30:00Z";
function harness() {
  const need = { id: "need-1", name: "Tomat", quantity: "3", unit: "stk" };
  const state = {
    oda: { connected: true }, basketMatchConnected: true,
    basketMatchPlanKey: "plan", basketMatchPlanEpoch: 1,
    basketMatchNeedVersions: { "need-1": 1 }, basketMatchPages: {},
    basketMatchCurrentPage: {}, basketMatchBusy: {}, basketMatchErrors: {},
    basketMatchJobs: {}, basketMatchObservedAt: {}, basketMatchStarted: true,
    basketMatchGlobalError: "", shoppingPanel: "single",
  };
  const record = {
    planKey: "plan", planEpoch: 1, start: "start", end: "end",
    needVersions: { "need-1": 1 }, needIds: ["need-1"],
  };
  const context = {
    state, window: { KitchenBasketMatchView: matchView },
    currentBasketMatchDates: () => ({ start: "start", end: "end" }),
    currentBasketMatchNeeds: () => [need],
    pauseBasketMatchPolling: () => {},
    scheduleBasketMatchPoll: () => {},
    startBasketMatchJob: () => {},
    dateLabel: (date) => date.toISOString(),
    formatUiCount: String,
    el: (tag, text, className) => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    button: (text, action) => { const button = new Node("button", text); button.action = action; return button; },
    renderBasketMatchCandidate: () => new Node("article", "candidate"),
    $: () => ({ open: false, dataset: {}, close() { this.open = false; } }),
  };
  vm.runInNewContext([
    "isCurrentBasketMatchRecord", "resetBasketMatching", "ensureBasketMatches",
    "applyBasketMatchSnapshot", "renderBasketMatchNeed",
  ].map(sourceFunction).join("\n"), context);
  function apply({ status = "complete", needStatus = "matched", time = FIRST, page = 1, snapshotRecord = record } = {}) {
    context.applyBasketMatchSnapshot({
      status, observed_at: time, detail: status === "failed" ? "Job failed" : null,
      needs: [{ need_id: "need-1", page, status: needStatus, candidates: [], detail: needStatus === "search_unavailable" ? "Søket feilet" : null }],
    }, snapshotRecord);
  }
  return { context, state, record, need, apply, render: () => context.renderBasketMatchNeed(need) };
}

test("only successful completed per-need observations receive a timestamp", () => {
  const ui = harness();
  ui.apply({ status: "queued" });
  ui.apply({ status: "running" });
  ui.apply({ status: "failed" });
  ui.apply({ needStatus: "search_unavailable" });
  assert.deepEqual(Object.keys(ui.state.basketMatchObservedAt), []);
  assert.doesNotMatch(ui.render().text, /Tilgjengelighet sist observert/);
  ui.apply();
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], FIRST);
  ui.apply({ needStatus: "no_results", time: SECOND });
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], SECOND);
});

test("display identifies a past observation and tells users to refresh before explicit review", () => {
  const ui = harness();
  ui.apply();
  const text = ui.render().text;
  assert.match(text, /Tilgjengelighet sist observert: 2026-11-16T10:20:00/);
  assert.match(text, /Tilgjengeligheten kan endre seg/);
  assert.match(text, /Oppdater treffene før du går gjennom og godkjenner produktvalg/);
  assert.doesNotMatch(text, /tilgjengelig nå|oppdatert nå|current availability/i);
});

test("failed refresh retains the last successful time alongside its separate failure state", () => {
  const ui = harness();
  ui.apply();
  ui.apply({ status: "running", needStatus: "searching", time: SECOND });
  ui.apply({ needStatus: "search_unavailable", time: SECOND });
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], FIRST);
  let text = ui.render().text;
  assert.match(text, /2026-11-16T10:20:00/);
  assert.match(text, /Søket er ikke tilgjengelig/);
  assert.match(text, /Søket feilet/);
  ui.state.basketMatchErrors[ui.need.id] = "Tilkoblingen feilet";
  text = ui.render().text;
  assert.match(text, /Tilkoblingen feilet/);
  assert.match(text, /2026-11-16T10:20:00/);
  ui.apply({ time: SECOND });
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], SECOND);
  assert.doesNotMatch(ui.render().text, /Tilkoblingen feilet/);
});

test("a result page never borrows a different page's observation time", () => {
  const ui = harness();
  ui.apply();
  ui.apply({ time: SECOND, page: 2, needStatus: "search_unavailable" });
  assert.doesNotMatch(ui.render().text, /Tilgjengelighet sist observert/);
  ui.apply({ time: SECOND, page: 2 });
  assert.match(ui.render().text, /2026-11-16T11:30:00/);
  ui.state.basketMatchCurrentPage[ui.need.id] = 1;
  assert.match(ui.render().text, /2026-11-16T10:20:00/);
});

test("late context and need-version snapshots cannot replace successful observation state", () => {
  const ui = harness();
  ui.apply();
  ui.apply({ time: SECOND, snapshotRecord: { ...ui.record, planEpoch: 0 } });
  ui.apply({ time: SECOND, snapshotRecord: { ...ui.record, needVersions: { "need-1": 0 } } });
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], FIRST);
  ui.apply({ time: "invalid" });
  ui.apply({ time: null });
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], FIRST);
});

test("plan changes and provider disconnect/reconnect clear observation state", () => {
  const ui = harness();
  ui.apply();
  ui.context.resetBasketMatching();
  assert.deepEqual(Object.keys(ui.state.basketMatchObservedAt), []);
  ui.state.basketMatchObservedAt[ui.need.id] = { 1: FIRST };
  ui.state.oda.connected = false;
  ui.context.ensureBasketMatches();
  assert.deepEqual(Object.keys(ui.state.basketMatchObservedAt), []);
  ui.state.basketMatchObservedAt[ui.need.id] = { 1: FIRST };
  ui.state.oda.connected = true;
  ui.context.ensureBasketMatches();
  assert.deepEqual(Object.keys(ui.state.basketMatchObservedAt), []);
  ui.state.basketMatchObservedAt[ui.need.id] = { 1: FIRST };
  ui.context.ensureBasketMatches(); // The previous reset now stores the matching plan key.
  assert.equal(ui.state.basketMatchObservedAt[ui.need.id][1], FIRST);
  ui.state.basketMatchPlanKey = "changed-plan";
  ui.context.ensureBasketMatches();
  assert.deepEqual(Object.keys(ui.state.basketMatchObservedAt), []);
});
