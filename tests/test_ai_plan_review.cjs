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
// Render the actual app functions with a small DOM; API calls never leave this test.
class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.disabled = false;
    this.checked = false;
    this.className = "";
    this.ownText = "";
  }
  set textContent(value) { this.ownText = value; this.children = []; }
  get textContent() { return this.ownText + this.children.map((node) => node.textContent).join(""); }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.ownText = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, action) { this.listeners[name] = action; }
  async click() { if (!this.disabled) await this.listeners.click?.(); }
  change(checked) {
    if (!this.disabled) {
      this.checked = checked;
      this.listeners.change?.();
    }
  }
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
const meals = [
  { date: "2026-11-16", recipe_id: "soup", servings: 2, label: "Middag" },
  { date: "2026-11-17", recipe_id: "pasta", servings: 3, scheduled_time: "18:00" },
];
async function render({ draft = { meals, notes: ["Bruk opp restene"] }, respond, refreshFails = false } = {}) {
  const root = new Node("dialog");
  const calls = [];
  let refreshes = 0;
  const suggest = new Function("document", "openDialog", "state", "api", "refreshView", "iso", "day", "dateLabel", "mealAgendaMetadata", `
    ${sourceFunction("el")}
    ${sourceFunction("add")}
    ${sourceFunction("button")}
    return (${sourceFunction("suggestWeek")});
  `)(
    { createElement: (tag) => new Node(tag) },
    () => root,
    { recipes: [{ id: "soup", title: "Suppe" }, { id: "pasta", title: "Pasta" }], meals: [{ id: "existing" }] },
    async (endpoint, method, body) => {
      calls.push({ endpoint, method, body });
      if (endpoint === "/ai/plan") return draft;
      if (respond) return respond(endpoint, method, body);
      return { id: "proposal/1", status: endpoint.endsWith("/confirm") ? "confirmed" : "pending" };
    },
    async () => { refreshes++; if (refreshFails) throw new Error("Refresh failed"); },
    (value) => value, () => "2026-11-16", (date) => date.toISOString().slice(0, 10),
    (meal) => `${meal.servings} porsjoner`,
  );
  await suggest();
  return {
    root, calls,
    checks: descendants(root).filter((node) => node.tagName === "input"),
    apply: descendants(root).find((node) => node.tagName === "button" && node.className === "primary"),
    status: descendants(root).find((node) => node.attributes.role === "status"),
    refreshes: () => refreshes,
  };
}

test("renders additions and notes without writes until explicit batch apply", async () => {
  const ui = await render();
  assert.equal(ui.checks.length, 2);
  assert.ok(ui.checks.every((node) => node.checked && !node.disabled));
  assert.match(ui.root.textContent, /Bruk opp restene/);
  assert.match(ui.root.textContent, /Eksisterende måltider beholdes/);
  assert.deepEqual(ui.calls, [{ endpoint: "/ai/plan", method: "POST", body: { start: "2026-11-16", days: 7 } }]);
  await ui.apply.click();
  assert.deepEqual(ui.calls.slice(1), [
    { endpoint: "/meal-plan-proposals", method: "POST", body: { meals } },
    { endpoint: "/meal-plan-proposals/proposal%2F1/confirm", method: "POST", body: { reviewed: true } },
  ]);
  assert.equal(ui.refreshes(), 1);
  assert.ok(ui.apply.disabled);
  assert.ok(ui.checks.every((node) => node.disabled));
  await ui.apply.click();
  assert.equal(ui.calls.length, 3);
});

test("unchecked additions are excluded and empty selection cannot apply", async () => {
  const ui = await render();
  ui.checks.forEach((node) => node.change(false));
  assert.ok(ui.apply.disabled);
  await ui.apply.click();
  assert.equal(ui.calls.length, 1);
  ui.checks[1].change(true);
  assert.equal(ui.apply.disabled, false);
  await ui.apply.click();
  assert.deepEqual(ui.calls[1].body, { meals: [meals[1]] });
});

test("missing recipes cannot be selected or submitted", async () => {
  const ui = await render({ draft: { meals: [...meals, { ...meals[0], recipe_id: "missing" }] } });
  assert.ok(ui.checks[2].disabled);
  assert.equal(ui.checks[2].checked, false);
  assert.match(ui.root.textContent, /Oppskriften finnes ikke lenger/);
  await ui.apply.click();
  assert.deepEqual(ui.calls[1].body, { meals });
});

test("proposal creation failure retains review and permits changing selection before retry", async () => {
  let attempts = 0;
  const ui = await render({ respond: (endpoint) => {
    if (endpoint === "/meal-plan-proposals" && ++attempts === 1) throw new Error("Proposal failed");
    return { id: "retry" };
  } });
  await ui.apply.click();
  assert.match(ui.status.textContent, /Proposal failed/);
  assert.equal(ui.checks[0].disabled, false);
  assert.equal(ui.refreshes(), 0);
  ui.checks[0].change(false);
  await ui.apply.click();
  assert.deepEqual(ui.calls[2].body, { meals: [meals[1]] });
  assert.equal(ui.calls[3].endpoint, "/meal-plan-proposals/retry/confirm");
});

test("uncertain confirm response retries the same proposal with its selection locked", async () => {
  let confirmations = 0;
  const ui = await render({ respond: (endpoint) => {
    if (endpoint.endsWith("/confirm") && ++confirmations === 1) throw new Error("Connection lost");
    return { id: "stable" };
  } });
  await ui.apply.click();
  assert.match(ui.status.textContent, /Connection lost/);
  assert.equal(ui.apply.disabled, false);
  assert.ok(ui.checks.every((node) => node.disabled));
  ui.checks[0].change(false);
  assert.ok(ui.checks[0].checked);
  assert.equal(ui.refreshes(), 0);
  await ui.apply.click();
  assert.deepEqual(ui.calls.slice(2).map((call) => call.endpoint), [
    "/meal-plan-proposals/stable/confirm", "/meal-plan-proposals/stable/confirm",
  ]);
  assert.deepEqual(ui.calls[3].body, { reviewed: true });
  assert.equal(ui.refreshes(), 1);
});

test("in-flight apply disables selection and prevents duplicate submissions", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const ui = await render({ respond: async () => { await pending; return { id: "pending" }; } });
  const first = ui.apply.click();
  assert.ok(ui.apply.disabled);
  assert.ok(ui.checks.every((node) => node.disabled));
  await ui.apply.click();
  assert.equal(ui.calls.length, 2);
  release();
  await first;
  assert.equal(ui.calls.length, 3);
});

test("refresh failure after successful confirmation never enables a second apply", async () => {
  const ui = await render({ refreshFails: true });
  await ui.apply.click();
  assert.match(ui.status.textContent, /Måltidene er lagt til, men ukevisningen/);
  assert.ok(ui.apply.disabled);
  await ui.apply.click();
  assert.equal(ui.calls.length, 3);
});

test("empty AI drafts render without a batch action", async () => {
  const ui = await render({ draft: { meals: [], notes: [] } });
  assert.equal(ui.apply, undefined);
  assert.match(ui.root.textContent, /Fikk ingen måltidsforslag/);
  assert.equal(ui.calls.length, 1);
});

test("confirmation transport errors do not claim that meals were not saved", async () => {
  const api = new Function("fetch", `return (${sourceFunction("api")});`)(async () => {
    throw new Error("Lost connection");
  });
  await assert.rejects(api("/meal-plan-proposals/stable/confirm", "POST", { reviewed: true }), (error) => {
    assert.match(error.message, /Fikk ikke svar på bekreftelsen/);
    assert.doesNotMatch(error.message, /ikke lagret/);
    return true;
  });
  await assert.rejects(api("/meals", "POST", meals[0]), /Endringene dine er ikke lagret/);
});

test("stale confirmation unlocks selection and creates a new proposal on explicit retry", async () => {
  let proposals = 0;
  const ui = await render({ respond: (endpoint) => {
    if (endpoint === "/meal-plan-proposals") return { id: `proposal-${++proposals}` };
    if (endpoint === "/meal-plan-proposals/proposal-1/confirm") {
      const error = new Error("Stale proposal");
      error.status = 409;
      throw error;
    }
    return { status: "confirmed" };
  } });
  await ui.apply.click();
  assert.match(ui.status.textContent, /Forslaget er utdatert eller utløpt/);
  assert.match(ui.status.textContent, /bekreft på nytt/);
  assert.equal(ui.apply.disabled, false);
  assert.ok(ui.checks.every((node) => !node.disabled && node.checked));
  assert.equal(ui.refreshes(), 0);
  assert.equal(ui.calls.length, 3);
  ui.checks[0].change(false);
  await ui.apply.click();
  assert.deepEqual(ui.calls.slice(3), [
    { endpoint: "/meal-plan-proposals", method: "POST", body: { meals: [meals[1]] } },
    { endpoint: "/meal-plan-proposals/proposal-2/confirm", method: "POST", body: { reviewed: true } },
  ]);
  assert.equal(ui.refreshes(), 1);
  assert.ok(ui.apply.disabled);
});
