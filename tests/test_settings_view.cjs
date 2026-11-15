const assert = require("node:assert/strict");
const test = require("node:test");

const {
  DEFAULT_SHOPPING_CATEGORY_ORDER,
  integrationAuthLabel,
  integrationStatusLabel,
  moveShoppingCategory,
  normalizeShoppingCategoryOrder,
  odaNeedsReconnect,
} = require("../src/home_kitchen/static/settings-view.js");

test("moves a category without mutating the saved order", () => {
  const order = ["Produce", "Bakery", "Dairy"];
  assert.deepEqual(moveShoppingCategory(order, 2, 0), ["Dairy", "Produce", "Bakery"]);
  assert.deepEqual(order, ["Produce", "Bakery", "Dairy"]);
  assert.deepEqual(moveShoppingCategory(order, 0, -1), order);
});

test("normalizes category names and preserves an explicit alphabetical order", () => {
  assert.deepEqual(normalizeShoppingCategoryOrder([" Produce ", "Other"]), {
    value: ["Produce", "Other"],
    error: null,
  });
  assert.deepEqual(normalizeShoppingCategoryOrder([]), { value: [], error: null });
});

test("rejects duplicate, blank, too-long, and excessive category orders", () => {
  assert.equal(normalizeShoppingCategoryOrder(["Pantry", " pantry "]).error, "duplicate");
  assert.equal(normalizeShoppingCategoryOrder([" "]).error, "invalid_name");
  assert.equal(normalizeShoppingCategoryOrder(["x".repeat(101)]).error, "invalid_name");
  assert.equal(normalizeShoppingCategoryOrder(Array(65).fill("x")).error, "too_many");
});

test("provides connection status and authentication labels without credentials", () => {
  assert.equal(integrationStatusLabel("connected"), "Connected");
  assert.equal(integrationStatusLabel("not_configured"), "Setup required");
  assert.equal(integrationAuthLabel("federation"), "Managed identity federation");
  assert.equal(integrationAuthLabel("api_key"), "API key");
  assert.equal(integrationAuthLabel("api_key", true), "API key configured (secret hidden)");
  assert.equal(integrationAuthLabel("unknown"), "Not reported");
  assert.equal(odaNeedsReconnect("Your Oda connection expired; connect again"), true);
  assert.equal(odaNeedsReconnect("This requires you to connect your Oda account first"), true);
  assert.equal(odaNeedsReconnect("Oda product search returned no matches"), false);
  assert.deepEqual(DEFAULT_SHOPPING_CATEGORY_ORDER.slice(0, 3), ["Produce", "Bakery", "Meat"]);
});
