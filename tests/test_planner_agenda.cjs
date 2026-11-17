const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);
const helper = source.match(/function agendaMeals\(meals, date\) \{([\s\S]*?)\n\}/);
assert.ok(helper, "app.js should define the selected-date agenda selector");
const agendaMeals = new Function("meals", "date", helper[1]);
function sourceFunction(name) {
  const match = source.match(new RegExp(`function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `app.js should define ${name}`);
  return match[0];
}
const mealSlotLabel = new Function(
  `return (${sourceFunction("mealSlotLabel")})`,
)();
const formatUiTime = new Function(
  `return (${sourceFunction("formatUiTime")})`,
)();
const mealAgendaMetadata = new Function(
  "formatServings",
  "mealSlotLabel",
  "formatUiTime",
  `return (${sourceFunction("mealAgendaMetadata")})`,
)((value) => String(value) + (Number(value) === 1 ? " porsjon" : " porsjoner"), mealSlotLabel, formatUiTime);
const buildMealWriteRequest = new Function(
  `return (${sourceFunction("buildMealWriteRequest")})`,
)();

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
    this.parentNode = null;
    this.classList = {
      contains: (cls) => this.className.split(/\s+/).filter(Boolean).includes(cls),
      toggle: (cls, force) => {
        const set = new Set(this.className.split(/\s+/).filter(Boolean));
        const shouldAdd = force !== undefined ? Boolean(force) : !set.has(cls);
        if (shouldAdd) set.add(cls);
        else set.delete(cls);
        this.className = [...set].join(" ");
      },
    };
  }
  append(...children) {
    children.forEach((child) => {
      if (!child) return;
      child.parentNode = this;
      this.children.push(child);
    });
  }
  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn({ target: this, currentTarget: this })); }
  dispatchEvent(type) { (this.listeners[type] || []).forEach((fn) => fn({ target: this, currentTarget: this })); }
  findAll(predicate, acc = []) {
    if (predicate(this)) acc.push(this);
    this.children.forEach((child) => child.findAll?.(predicate, acc));
    return acc;
  }
  querySelectorAll(selector) {
    const classNames = selector.startsWith(".") ? selector.slice(1).split(".") : null;
    return this.findAll((node) => {
      if (classNames) return classNames.every((cls) => node.classList.contains(cls));
      return node.tagName.toLowerCase() === selector.toLowerCase();
    });
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function buildWeekHarness({ week, selectedDate, meals = [], recipes = [] }) {
  const main = new DomNode("main");
  const mealFormCalls = [];
  let refreshCount = 0;
  let syncUrlCount = 0;
  const state = {
    week,
    selectedDate,
    meals,
    recipes,
  };
  const context = {
    state,
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
    title: (heading, eyebrow, subtitle, actions = []) => {
      const wrap = new DomNode("header", "", "page-title");
      wrap.append(new DomNode("h1", heading));
      actions.forEach((act) => wrap.append(act));
      return wrap;
    },
    suggestWeek: () => {},
    mealForm: (...args) => mealFormCalls.push(args),
    viewRecipe: () => {},
    openPlannedMeal: () => {},
    mutate: async () => {},
    notify: () => {},
    refreshView: async () => { refreshCount += 1; },
    syncPlannerUrl: () => { syncUrlCount += 1; },
    formatServings: (v) => `${v} ${Number(v) === 1 ? "porsjon" : "porsjoner"}`,
    mealSlotLabel,
    formatUiTime,
    mealAgendaMetadata,
    agendaMeals,
  };
  const fnNames = [
    "localDate",
    "iso",
    "startOfWeek",
    "day",
    "parseDate",
    "dateLabel",
    "weekControls",
    "renderWeek",
    "renderAgendaMeal",
  ];
  vm.runInNewContext(fnNames.map(sourceFunction).join("\n"), context);
  return {
    main,
    state,
    context,
    mealFormCalls,
    getRefreshCount: () => refreshCount,
    getSyncUrlCount: () => syncUrlCount,
  };
}

test("selects all existing meals for one date without sorting or mutating them", () => {
  const rows = [
    { id: "late", date: "2026-11-18", scheduled_time: "19:00" },
    { id: "other-day", date: "2026-11-19", slot: "dinner" },
    { id: "early", date: "2026-11-18", scheduled_time: "08:00" },
    { id: "without-slot", date: "2026-11-18" },
  ];

  assert.deepEqual(
    agendaMeals(rows, "2026-11-18").map((meal) => meal.id),
    ["late", "early", "without-slot"],
  );
  assert.deepEqual(rows.map((meal) => meal.id), ["late", "other-day", "early", "without-slot"]);
});

test("formats selected-date rows using labels and scheduled times, with legacy slots localized", () => {
  const rows = agendaMeals(
    [
      { id: "a", date: "2026-11-18", label: "Matpakke", scheduled_time: "12:30:00", servings: 3 },
      { id: "b", date: "2026-11-19", label: "Middag", scheduled_time: null, servings: 2 },
      { id: "c", date: "2026-11-18", slot: "breakfast", servings: 1 },
    ],
    "2026-11-18",
  );
  assert.deepEqual(rows.map((meal) => meal.id), ["a", "c"]);
  assert.equal(mealAgendaMetadata(rows[0]), "Matpakke · 12:30 · 3 porsjoner");
  assert.equal(mealAgendaMetadata(rows[1]), "Frokost · 1 porsjon");
});

test("creates append-only meal requests and updates only the selected meal ID", () => {
  const values = {
    date: "2026-11-18",
    label: "  Matpakke  ",
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: "3",
  };
  const added = buildMealWriteRequest(null, values);
  assert.equal(added.path, "/meals");
  assert.equal(added.method, "POST");
  assert.deepEqual(added.payload, {
    date: "2026-11-18",
    label: "Matpakke",
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: 3,
  });
  const updated = buildMealWriteRequest({ id: "meal/1" }, { ...values, label: "  " });
  assert.equal(updated.path, "/meals/meal%2F1");
  assert.equal(updated.method, "PUT");
  assert.deepEqual(updated.payload, {
    date: "2026-11-18",
    label: null,
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: 3,
  });
  assert.equal("slot" in updated.payload, false);
});

test("unnamed flexible meals have no hidden default meal type", () => {
  assert.equal(mealAgendaMetadata({ servings: 2 }), "2 porsjoner");
  const request = buildMealWriteRequest(null, {
    date: "2026-11-18",
    label: "",
    scheduled_time: "",
    recipe_id: "recipe-1",
    servings: 2,
  });
  assert.equal(request.payload.label, null);
  assert.equal(request.payload.scheduled_time, null);
  assert.equal("slot" in request.payload, false);
});

test("startOfWeek aligns any weekday to Monday of that week", () => {
  const harness = buildWeekHarness({
    week: new Date(2026, 10, 18, 12),
    selectedDate: new Date(2026, 10, 18, 12),
  });
  const { startOfWeek, iso } = harness.context;
  assert.equal(iso(startOfWeek(new Date(2026, 10, 18, 12))), "2026-11-16"); // Wed -> Mon
  assert.equal(iso(startOfWeek(new Date(2026, 10, 16, 12))), "2026-11-16"); // Mon -> Mon
  assert.equal(iso(startOfWeek(new Date(2026, 10, 22, 12))), "2026-11-16"); // Sun -> Mon
});

test("renderWeek renders a 7-day Monday-to-Sunday planning grid with meals, empty states, and quick-add buttons", () => {
  const wednesday = new Date(2026, 10, 18, 12);
  const harness = buildWeekHarness({
    week: wednesday,
    selectedDate: wednesday,
    recipes: [{ id: "r-1", title: "Fiskesuppe" }],
    meals: [
      { id: "m-1", date: "2026-11-16", recipe_id: "r-1", servings: 2, label: "Middag" },
      { id: "m-2", date: "2026-11-18", recipe_id: "r-1", servings: 4, scheduled_time: "18:00" },
    ],
  });

  harness.context.renderWeek(harness.main);

  assert.equal(harness.context.iso(harness.state.week), "2026-11-16");
  const grid = harness.main.querySelector(".week-grid");
  assert.ok(grid, "week-grid should be rendered");
  const dayCards = grid.querySelectorAll(".day");
  assert.equal(dayCards.length, 7);
  assert.deepEqual(
    dayCards.map((card) => card.dataset.date),
    [
      "2026-11-16",
      "2026-11-17",
      "2026-11-18",
      "2026-11-19",
      "2026-11-20",
      "2026-11-21",
      "2026-11-22",
    ],
  );

  // Selected day highlight on Wednesday
  assert.equal(dayCards[2].classList.contains("selected-day"), true);
  assert.equal(dayCards[0].classList.contains("selected-day"), false);

  // Meals rendered on Monday and Wednesday; empty states on other days
  assert.equal(dayCards[0].querySelectorAll(".agenda-meal").length, 1);
  assert.equal(dayCards[0].querySelector(".meal-title").textContent, "Fiskesuppe");
  assert.equal(dayCards[1].querySelector(".empty-day").textContent, "Ingen planlagte måltider");
  assert.equal(dayCards[2].querySelectorAll(".agenda-meal").length, 1);

  // Quick-add button sets selectedDate and invokes mealForm with that day's ISO date
  const fridayAdd = dayCards[4].querySelector(".add-meal-button");
  assert.ok(fridayAdd);
  assert.equal(fridayAdd.textContent, "+ Legg til måltid");
  assert.equal(fridayAdd.dataset.date, "2026-11-20");
  fridayAdd.click();
  assert.equal(harness.context.iso(harness.state.selectedDate), "2026-11-20");
  assert.deepEqual(harness.mealFormCalls, [["2026-11-20"]]);
});

test("weekControls navigates previous/next week and jumps to selected date while keeping Monday alignment", async () => {
  const wednesday = new Date(2026, 10, 18, 12);
  const harness = buildWeekHarness({
    week: new Date(2026, 10, 16, 12),
    selectedDate: wednesday,
  });

  harness.context.renderWeek(harness.main);
  const toolbar = harness.main.querySelector(".period-toolbar");
  assert.ok(toolbar);
  const prevBtn = toolbar.findAll((n) => n.dataset?.focusKey === "planner-previous-week")[0];
  const nextBtn = toolbar.findAll((n) => n.dataset?.focusKey === "planner-next-week")[0];
  const thisWeekBtn = toolbar.findAll((n) => n.dataset?.focusKey === "planner-this-week")[0];
  const jumpInput = toolbar.findAll((n) => n.dataset?.focusKey === "planner-date-jump")[0];
  assert.equal(prevBtn.textContent, "Forrige uke");
  assert.equal(nextBtn.textContent, "Neste uke");
  assert.equal(thisWeekBtn.textContent, "Denne uken");
  assert.equal(jumpInput.value, "2026-11-18");

  nextBtn.click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(harness.context.iso(harness.state.week), "2026-11-23");
  assert.equal(harness.context.iso(harness.state.selectedDate), "2026-11-25");
  assert.equal(harness.getSyncUrlCount(), 1);
  assert.equal(harness.getRefreshCount(), 1);

  prevBtn.click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(harness.context.iso(harness.state.week), "2026-11-16");
  assert.equal(harness.context.iso(harness.state.selectedDate), "2026-11-18");

  jumpInput.value = "2026-12-03"; // Thursday
  jumpInput.dispatchEvent("change");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(harness.context.iso(harness.state.selectedDate), "2026-12-03");
  assert.equal(harness.context.iso(harness.state.week), "2026-11-30"); // Monday
});
