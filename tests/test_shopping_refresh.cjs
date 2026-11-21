const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { formatQuantityForAccessibility } = require("../src/home_kitchen/static/shopping-view.js");

const appSource = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);
const accessibleFormatter = appSource.match(/function formatUiAccessibleQuantity\(value\) \{[\s\S]*?\n\}/u);
assert.ok(accessibleFormatter, "the concise accessible quantity formatter is defined");
const quantityContext = {
  window: {
    KitchenShoppingView: {
      formatQuantityForAccessibility,
    },
  },
};
vm.runInNewContext(
  accessibleFormatter[0] + "\nglobalThis.formatUiAccessibleQuantity = formatUiAccessibleQuantity;",
  quantityContext,
);
assert.equal((appSource.match(/formatUiAccessibleQuantity\(/gu) || []).length, 4);

const refreshHelpersBlock = appSource.match(
  /function shouldRefreshHouseholdView\([\s\S]*?\nasync function refreshWeekIfSafe\(force = false\) \{[\s\S]*?\n\}/u,
);
assert.ok(refreshHelpersBlock, "household refresh guards and safe refresh helpers are defined");
const refineGuardHelper = appSource.match(
  /function isBasketRefineFormActiveOrDirty\([\s\S]*?\n\}/,
);
assert.ok(refineGuardHelper, "the basket refine guard helper is present");
const openFormHelper = appSource.match(
  /function hasOpenShoppingForm\([\s\S]*?\n\}/,
);
assert.ok(openFormHelper, "the open shopping form helper is present");

const returnListenersBlock = appSource.match(
  /window\.addEventListener\("online", \(\) => \{[\s\S]*?\ndocument\.addEventListener\("visibilitychange", refreshActiveHouseholdViewOnReturn\);/u,
);
assert.ok(returnListenersBlock, "online/focus/pageshow/visibilitychange return refresh listeners are defined");

const modalDirtyAndCloseBlock = appSource.match(
  /const markMealPlanEditorDirty = \(\) => \{[\s\S]*?\n\}\);(?=\n\$\("#modal"\)\.addEventListener\("close", \(\) => restoreModalReturnFocus)/u,
);
assert.ok(modalDirtyAndCloseBlock, "modal dirty tracking and pending-refresh close handlers are defined");

const context = {};
vm.runInNewContext(
  `
  function shoppingRefreshLabel() { return ""; }
  function updateShoppingRefreshControl() {}
  ${refineGuardHelper[0]}
  ${openFormHelper[0]}
  ${refreshHelpersBlock[0]}
  globalThis.hasOpenShoppingForm = hasOpenShoppingForm;
  globalThis.shouldRefreshHouseholdView = shouldRefreshHouseholdView;
  globalThis.shouldRefreshShopping = shouldRefreshShopping;
  globalThis.shouldRefreshWeek = shouldRefreshWeek;
  globalThis.isCurrentPeriodLoad = isCurrentPeriodLoad;
  globalThis.isCurrentShoppingLoad = isCurrentShoppingLoad;
  globalThis.isCurrentWeekLoad = isCurrentWeekLoad;
  `,
  context,
);

function createReturnHarness({
  view = "week",
  visibilityState = "visible",
  modalOpen = false,
  photoModalOpen = false,
  mainFormOpen = false,
  now = 10_000,
} = {}) {
  let currentNow = now;
  let refreshCount = 0;
  const refreshViews = [];
  const notices = [];
  const windowListeners = new Map();
  const documentListeners = new Map();
  const modalListeners = new Map();
  const photoModalListeners = new Map();
  const scheduled = [];

  const modal = {
    open: modalOpen,
    addEventListener(type, fn) {
      if (!modalListeners.has(type)) modalListeners.set(type, []);
      modalListeners.get(type).push(fn);
    },
    dispatch(type) {
      for (const fn of modalListeners.get(type) || []) fn({});
    },
    close() {
      this.open = false;
      this.dispatch("close");
    },
  };
  const photoModal = {
    open: photoModalOpen,
    addEventListener(type, fn) {
      if (!photoModalListeners.has(type)) photoModalListeners.set(type, []);
      photoModalListeners.get(type).push(fn);
    },
    close() {
      this.open = false;
      for (const fn of photoModalListeners.get("close") || []) fn({});
    },
  };

  const state = {
    view,
    shoppingLastRefreshedAt: null,
    shoppingRefreshInFlight: false,
    shoppingRefreshPending: false,
    shoppingRefreshLastEventAt: 0,
    shoppingRefreshRequestCount: 0,
    shoppingLoadSequence: 0,
    shoppingWriteInFlight: 0,
    weekLastRefreshedAt: null,
    weekRefreshInFlight: false,
    weekRefreshPending: false,
    weekRefreshLastEventAt: 0,
    weekRefreshRequestCount: 0,
    weekLoadSequence: 0,
    weekWriteInFlight: 0,
    mealPlanEditorDirty: false,
  };

  let mainForm = mainFormOpen;

  const harnessContext = {
    state,
    Date: { now: () => currentNow },
    setTimeout: (fn) => {
      scheduled.push(fn);
      return scheduled.length;
    },
    $: (selector) => {
      if (selector === "#modal") return modal;
      if (selector === "#private-photo-modal") return photoModal;
      if (selector === "#main form") return mainForm ? {} : null;
      return null;
    },
    notify: (msg, isErr) => notices.push({ msg, isErr: Boolean(isErr) }),
    updateShoppingRefreshControl: () => {},
    refreshView: async () => {
      refreshCount += 1;
      refreshViews.push(state.view);
      if (state.view === "shopping") state.shoppingRefreshPending = false;
      if (state.view === "week") state.weekRefreshPending = false;
      return true;
    },
    window: {
      addEventListener(type, fn) {
        if (!windowListeners.has(type)) windowListeners.set(type, []);
        windowListeners.get(type).push(fn);
      },
    },
    document: {
      get visibilityState() {
        return visibilityState;
      },
      addEventListener(type, fn) {
        if (!documentListeners.has(type)) documentListeners.set(type, []);
        documentListeners.get(type).push(fn);
      },
    },
  };

  vm.runInNewContext(
    `
    ${refineGuardHelper[0]}
    ${openFormHelper[0]}
    ${refreshHelpersBlock[0]}
    ${returnListenersBlock[0]}
    ${modalDirtyAndCloseBlock[0]}
    globalThis.hasOpenShoppingForm = hasOpenShoppingForm;
    globalThis.refreshActiveHouseholdViewOnReturn = refreshActiveHouseholdViewOnReturn;
    globalThis.refreshShoppingIfSafe = refreshShoppingIfSafe;
    globalThis.refreshWeekIfSafe = refreshWeekIfSafe;
    `,
    harnessContext,
  );

  async function flushScheduled() {
    while (scheduled.length) {
      const fn = scheduled.shift();
      fn();
      await new Promise((resolve) => setImmediate(resolve));
    }
  }

  return {
    state,
    modal,
    notices,
    get refreshCount() {
      return refreshCount;
    },
    refreshViews,
    setNow(value) {
      currentNow = value;
    },
    setVisibility(value) {
      visibilityState = value;
    },
    setMainFormOpen(value) {
      mainForm = value;
    },
    async triggerWindow(type) {
      for (const fn of windowListeners.get(type) || []) fn({ type });
      await new Promise((resolve) => setImmediate(resolve));
    },
    async triggerDocument(type) {
      for (const fn of documentListeners.get(type) || []) fn({ type });
      await new Promise((resolve) => setImmediate(resolve));
    },
    flushScheduled,
  };
}

test("refreshes only the visible shopping view when no edit is open", () => {
  assert.equal(context.shouldRefreshShopping("shopping", "visible", false, false), true);
  assert.equal(context.shouldRefreshShopping("recipes", "visible", false, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "hidden", false, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "visible", true, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "visible", false, true), false);
});

test("refreshes only the visible week meal plan when no edit dialog, form, or dirty review is open", () => {
  assert.equal(context.shouldRefreshWeek("week", "visible", false, false, false), true);
  assert.equal(context.shouldRefreshWeek("recipes", "visible", false, false, false), false);
  assert.equal(context.shouldRefreshWeek("week", "hidden", false, false, false), false);
  assert.equal(context.shouldRefreshWeek("week", "visible", true, false, false), false);
  assert.equal(context.shouldRefreshWeek("week", "visible", false, true, false), false);
  assert.equal(context.shouldRefreshWeek("week", "visible", false, false, true), false);
});

test("drops results from an older request or a changed shopping or week period", () => {
  assert.equal(context.isCurrentShoppingLoad("shopping", 3, 3, "2026-11-16/2026-11-22", "2026-11-16/2026-11-22"), true);
  assert.equal(context.isCurrentShoppingLoad("shopping", 4, 3, "2026-11-23/2026-11-29", "2026-11-16/2026-11-22"), false);
  assert.equal(context.isCurrentShoppingLoad("recipes", 3, 3, "2026-11-16/2026-11-22", "2026-11-16/2026-11-22"), false);

  assert.equal(context.isCurrentWeekLoad("week", 2, 2, "2026-11-16/2026-11-22", "2026-11-16/2026-11-22"), true);
  assert.equal(context.isCurrentWeekLoad("week", 3, 2, "2026-11-23/2026-11-29", "2026-11-16/2026-11-22"), false);
  assert.equal(context.isCurrentWeekLoad("shopping", 2, 2, "2026-11-16/2026-11-22", "2026-11-16/2026-11-22"), false);
});

test("returning to visible week view refreshes meal plan and debounces immediate duplicate events", async () => {
  const app = createReturnHarness({ view: "week", visibilityState: "hidden", now: 5_000 });
  await app.triggerDocument("visibilitychange");
  assert.equal(app.refreshCount, 0);

  app.setVisibility("visible");
  await app.triggerDocument("visibilitychange");
  assert.equal(app.refreshCount, 1);
  assert.deepEqual(app.refreshViews, ["week"]);

  // Immediate focus/pageshow within 1500ms is debounced
  app.setNow(5_500);
  await app.triggerWindow("focus");
  await app.triggerWindow("pageshow");
  assert.equal(app.refreshCount, 1);

  // After debounce window, focus refreshes again
  app.setNow(7_100);
  await app.triggerWindow("focus");
  assert.equal(app.refreshCount, 2);
});

test("defers week meal-plan refresh while an edit dialog, AI proposal review, or form is open/dirty, then flushes on close", async () => {
  const app = createReturnHarness({ view: "week", modalOpen: true, now: 10_000 });
  // Simulate user editing a meal or toggling an AI proposal checkbox inside #modal
  app.modal.dispatch("change");
  assert.equal(app.state.mealPlanEditorDirty, true);

  await app.triggerWindow("focus");
  await app.triggerWindow("online");
  assert.equal(app.refreshCount, 0);
  assert.equal(app.state.weekRefreshPending, true);
  assert.equal(app.notices[0]?.msg, "Tilkoblingen er gjenopprettet.");

  // Closing the modal clears dirty flag and flushes the deferred week refresh
  app.modal.close();
  assert.equal(app.state.mealPlanEditorDirty, false);
  await app.flushScheduled();
  assert.equal(app.refreshCount, 1);
  assert.equal(app.state.weekRefreshPending, false);
  assert.deepEqual(app.refreshViews, ["week"]);

  // Inline form or in-flight meal write also defers refresh
  app.setNow(12_000);
  app.setMainFormOpen(true);
  await app.triggerWindow("pageshow");
  assert.equal(app.refreshCount, 1);
  assert.equal(app.state.weekRefreshPending, true);

  app.setMainFormOpen(false);
  app.state.weekWriteInFlight = 1;
  await app.triggerWindow("online");
  assert.equal(app.refreshCount, 1);
  assert.equal(app.state.weekRefreshPending, true);

  app.state.weekWriteInFlight = 0;
  await app.triggerWindow("online");
  assert.equal(app.refreshCount, 2);
});

test("defers shopping refresh while a modal/form/write is active and flushes when modal closes", async () => {
  const app = createReturnHarness({ view: "shopping", modalOpen: true, now: 20_000 });
  await app.triggerWindow("focus");
  await app.triggerWindow("online");
  assert.equal(app.refreshCount, 0);
  assert.equal(app.state.shoppingRefreshPending, true);

  app.modal.close();
  await app.flushScheduled();
  assert.equal(app.refreshCount, 1);
  assert.equal(app.state.shoppingRefreshPending, false);
  assert.deepEqual(app.refreshViews, ["shopping"]);
});

test("uses concise accessible quantities while preserving tiny positive amounts", () => {
  assert.equal(quantityContext.formatUiAccessibleQuantity("233.3333333333333333333333334"), "233,33");
  assert.equal(quantityContext.formatUiAccessibleQuantity("0.0001"), "<0,01");
});

test("mounted basket-match-refine forms do not block shopping refresh unless focused or dirty", () => {
  function makeForm(className, { focused = false, dirtyAttr = false, defaultValue = "Helmelk", value = "Helmelk" } = {}) {
    const input = { defaultValue, value };
    return {
      className,
      dataset: dirtyAttr ? { dirty: "true" } : {},
      contains: (el) => focused && el === input,
      querySelector: () => input,
      _input: input,
    };
  }

  const idleRefine = makeForm("basket-match-refine");
  const mainWithIdleRefine = { querySelectorAll: () => [idleRefine] };
  assert.equal(context.hasOpenShoppingForm(mainWithIdleRefine, null), false);

  const focusedRefine = makeForm("basket-match-refine", { focused: true });
  assert.equal(
    context.hasOpenShoppingForm({ querySelectorAll: () => [focusedRefine] }, focusedRefine._input),
    true,
  );

  const dirtyValueRefine = makeForm("basket-match-refine", {
    defaultValue: "Helmelk",
    value: "Tine Helmelk",
  });
  assert.equal(
    context.hasOpenShoppingForm({ querySelectorAll: () => [dirtyValueRefine] }, null),
    true,
  );

  const dirtyFlagRefine = makeForm("basket-match-refine", { dirtyAttr: true });
  assert.equal(
    context.hasOpenShoppingForm({ querySelectorAll: () => [dirtyFlagRefine] }, null),
    true,
  );

  const otherForm = makeForm("oda-search-form");
  assert.equal(
    context.hasOpenShoppingForm({ querySelectorAll: () => [idleRefine, otherForm] }, null),
    true,
  );

  assert.equal((appSource.match(/refineInput\.defaultValue\s*=/gu) || []).length, 2);
  assert.match(appSource, /hasOpenShoppingForm\(\$\("#main"\),\s*document\.activeElement\)/u);
});
