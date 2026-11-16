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
const helper = appSource.match(
  /function shouldRefreshShopping\([\s\S]*?\n\}/,
);
assert.ok(helper, "the shopping refresh safety guard is present");
const context = {};
vm.runInNewContext(
  helper[0] + "\nglobalThis.shouldRefreshShopping = shouldRefreshShopping;",
  context,
);

test("refreshes only the visible shopping view when no edit is open", () => {
  assert.equal(context.shouldRefreshShopping("shopping", "visible", false, false), true);
  assert.equal(context.shouldRefreshShopping("recipes", "visible", false, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "hidden", false, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "visible", true, false), false);
  assert.equal(context.shouldRefreshShopping("shopping", "visible", false, true), false);
});

test("uses concise accessible quantities while preserving tiny positive amounts", () => {
  assert.equal(quantityContext.formatUiAccessibleQuantity("233.3333333333333333333333334"), "233,33");
  assert.equal(quantityContext.formatUiAccessibleQuantity("0.0001"), "<0,01");
});
