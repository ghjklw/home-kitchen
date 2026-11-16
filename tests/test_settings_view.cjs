const assert = require("node:assert/strict");
const test = require("node:test");

const {
  DEFAULT_SHOPPING_CATEGORY_ORDER,
  integrationAuthLabel,
  integrationStatusLabel,
  moveShoppingCategory,
  normalizeShoppingCategoryOrder,
  normalizeIntegrationResponse,
  connectionStatusSnapshot,
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
  assert.equal(integrationStatusLabel("connected"), "Tilkoblet");
  assert.equal(integrationStatusLabel("not_configured"), "Oppsett kreves");
  assert.equal(integrationAuthLabel("federation"), "Føderert administrert identitet");
  assert.equal(integrationAuthLabel("api_key"), "API-nøkkel");
  assert.equal(integrationAuthLabel("api_key", true), "API-nøkkel er satt opp (hemmeligheten er skjult)");
  assert.equal(integrationAuthLabel("unknown"), "Ikke oppgitt");
  assert.equal(odaNeedsReconnect("Your Oda connection expired; connect again"), true);
  assert.equal(odaNeedsReconnect("This requires you to connect your Oda account first"), true);
  assert.equal(odaNeedsReconnect("Oda product search returned no matches"), false);
  assert.deepEqual(DEFAULT_SHOPPING_CATEGORY_ORDER.slice(0, 3), ["Produce", "Bakery", "Meat"]);
});

test("accepts typed integration rows and rejects a legacy summary payload", () => {
  const typed = [{ id: "anthropic", name: "Recipe assistant", status: "configured" }];
  assert.equal(normalizeIntegrationResponse(typed), typed);
  assert.equal(normalizeIntegrationResponse({ auth: { mode: "local" } }), null);
});

test("keeps integration and Oda status independently when one endpoint fails", () => {
  const integrationRows = [{ id: "anthropic", status: "configured" }];
  const partialOdaFailure = connectionStatusSnapshot(
    { status: "fulfilled", value: integrationRows },
    { status: "rejected", reason: new Error("offline") },
  );
  assert.equal(partialOdaFailure.integrations, integrationRows);
  assert.equal(partialOdaFailure.integrationsError, null);
  assert.equal(partialOdaFailure.oda, null);
  assert.match(partialOdaFailure.odaStatusError, /Oda-status er ikke tilgjengelig/);

  const partialIntegrationFailure = connectionStatusSnapshot(
    { status: "rejected", reason: new Error("offline") },
    { status: "fulfilled", value: { configured: true, connected: true, detail: "Connected" } },
  );
  assert.deepEqual(partialIntegrationFailure.integrations, []);
  assert.match(partialIntegrationFailure.integrationsError, /Tilkoblingsstatus er ikke tilgjengelig/);
  assert.equal(partialIntegrationFailure.oda.connected, true);
  assert.equal(partialIntegrationFailure.odaStatusError, null);
});

test("reports malformed successful endpoint payloads as unavailable", () => {
  const snapshot = connectionStatusSnapshot(
    { status: "fulfilled", value: { unexpected: true } },
    { status: "fulfilled", value: [] },
  );
  assert.match(snapshot.integrationsError, /Tilkoblingsstatus er ikke tilgjengelig/);
  assert.match(snapshot.odaStatusError, /Oda-status er ikke tilgjengelig/);

  const emptyOda = connectionStatusSnapshot(
    { status: "fulfilled", value: [] },
    { status: "fulfilled", value: {} },
  );
  assert.equal(emptyOda.integrationsError, null);
  assert.match(emptyOda.odaStatusError, /Oda-status er ikke tilgjengelig/);
});
