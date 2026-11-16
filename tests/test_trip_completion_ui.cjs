const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync("src/home_kitchen/static/app.js", "utf8");
const helperStart = source.indexOf("window.KitchenTripCompletion = {");
const helperEnd = source.indexOf("\n};", helperStart) + 3;
const sandbox = { window: {} };
vm.runInNewContext(source.slice(helperStart, helperEnd), sandbox);
const completion = sandbox.window.KitchenTripCompletion;

test("maps reviewed meal, capture, and regular sources to their matching purchase fields", () => {
  assert.equal(completion.purchaseSourceField("meal"), "close_meal_ids");
  assert.equal(completion.purchaseSourceField("capture"), "close_capture_ids");
  assert.equal(completion.purchaseSourceField("regular"), "regular_ids");
  assert.equal(completion.purchaseSourceField("unknown"), null);
});

test("exact product purchase requires a product from this reviewed line and explicit package count", () => {
  const line = { selected_products: [{ product: { provider: "oda", product_id: "p-1" } }] };
  assert.deepEqual({ ...completion.exactProductPurchase(line, 0, "3") }, {
    provider: "oda", product_id: "p-1", packages: 3,
  });
  assert.equal(completion.exactProductPurchase(line, "", "3"), null);
  assert.equal(completion.exactProductPurchase(line, 1, "3"), null);
  assert.equal(completion.exactProductPurchase(line, 0, ""), null);
  assert.equal(completion.exactProductPurchase(line, 0, "1.5"), null);
  assert.equal(completion.exactProductPurchase(line, 0, "100"), null);
});

test("completion UI uses reviewed endpoints and does not default planned need into actual purchase", () => {
  assert.match(source, /\/store-trip\/completion-review/);
  assert.match(source, /\/store-trip\/completions/);
  assert.match(source, /Planlagt behov: \$\{formatUiQuantity\(line\.to_buy_quantity\)\} \$\{line\.unit\} · kontekst, ikke faktisk kjøp/);
  assert.match(source, /quantity\.placeholder = "Ukjent"/);
  assert.match(source, /reviewed: true/);
  assert.match(source, /crypto\.randomUUID\(\)/);
});
