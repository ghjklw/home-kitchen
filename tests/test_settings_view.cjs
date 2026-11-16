const assert = require("node:assert/strict");
const test = require("node:test");

const {
  DEFAULT_SHOPPING_CATEGORY_ORDER,
  integrationAuthLabel,
  integrationStatusLabel,
  integrationExpiryNotice,
  moveShoppingCategory,
  normalizeShoppingCategoryOrder,
  createShoppingCategoryRow,
  editShoppingCategoryRow,
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

test("local category display labels preserve canonical values until the user renames them", () => {
  const label = (value) => ({ Produce: "Frukt og grønt", Dairy: "Meieri" })[value] || value;
  const row = createShoppingCategoryRow("Produce", label);
  assert.deepEqual(row, {
    value: "Produce",
    originalValue: "Produce",
    displayValue: "Frukt og grønt",
  });
  assert.deepEqual(editShoppingCategoryRow(row, "Frukt og grønt", label), row);
  assert.deepEqual(editShoppingCategoryRow(row, "Grønnsaker", label), {
    value: "Grønnsaker",
    originalValue: "Produce",
    displayValue: "Grønnsaker",
  });
  assert.deepEqual(editShoppingCategoryRow({ value: "Hverdagsmat", originalValue: "Hverdagsmat", displayValue: "Hverdagsmat" }, "Hverdagsmat", label), {
    value: "Hverdagsmat",
    originalValue: "Hverdagsmat",
    displayValue: "Hverdagsmat",
  });
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

test("provides status labels for expired connectors", () => {
  assert.equal(integrationStatusLabel("expired"), "Sesjon utløpt");
});

test("provides authentication labels for session token, cookie, and credentials without revealing secrets", () => {
  assert.equal(integrationAuthLabel("session_token"), "Sesjonstoken");
  assert.equal(
    integrationAuthLabel("session_token", true),
    "Sesjonstoken er lagret (hemmeligheten er skjult)",
  );
  assert.equal(integrationAuthLabel("session_cookie"), "Sesjonsinformasjonskapsel");
  assert.equal(
    integrationAuthLabel("session_cookie", true),
    "Sesjonsinformasjonskapsel er lagret (hemmeligheten er skjult)",
  );
  assert.equal(integrationAuthLabel("credentials"), "Brukernavn og passord");
  assert.equal(
    integrationAuthLabel("credentials", true),
    "Brukernavn og passord er lagret (hemmeligheten er skjult)",
  );
});

test("calculates expiry notices for expired and upcoming expiry states", () => {
  const expiredIntegration = {
    id: "trumf",
    status: "expired",
    expiry_prompt: "Sesjonen for Trumf har utløpt. Oppdater informasjonskapselen for å fortsette synkronisering.",
  };
  assert.equal(
    integrationExpiryNotice(expiredIntegration),
    "Sesjonen for Trumf har utløpt. Oppdater informasjonskapselen for å fortsette synkronisering.",
  );

  const nowMs = 1700000000000;
  // 3 days in the future
  const expiringSoon = {
    id: "rema1000",
    status: "connected",
    expires_at: new Date(nowMs + 3 * 86400000).toISOString(),
  };
  assert.equal(
    integrationExpiryNotice(expiringSoon, nowMs),
    "Sesjonen utløper om 3 dager. Vurder å fornye tilkoblingen snart.",
  );

  // 30 days in the future (no warning needed)
  const farFuture = {
    id: "rema1000",
    status: "connected",
    expires_at: new Date(nowMs + 30 * 86400000).toISOString(),
  };
  assert.equal(integrationExpiryNotice(farFuture, nowMs), null);
});

test("app.js source includes connector controls and password-masked inputs", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");

  // Verify connect and disconnect routes
  assert.match(appSource, /\/integrations\/\$\{providerId\}\/connection/);
  assert.match(appSource, /\/integrations\/\$\{integration\.id\}\/connect/);

  // Verify Bokmål controls
  assert.match(appSource, /Koble til/);
  assert.match(appSource, /Oppdater tilkobling/);
  assert.match(appSource, /Forny tilkobling/);
  assert.match(appSource, /Koble fra/);

  // Verify password masking for secret inputs in openConnectorModal
  assert.match(appSource, /field\(\s*"Sesjonstoken \(token\)",\s*"password"/);
  assert.match(appSource, /field\(\s*"Informasjonskapsel \(cookie\)",\s*"password"/);
  assert.match(appSource, /field\(\s*"Passord",\s*"password"/);
});
