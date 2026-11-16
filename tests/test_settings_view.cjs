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
  parsePkceCompletionPayload,
  buildCookidooLoginPayload,
  buildTrumfVerifyPayload,
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

// --- Unit tests for connector payload helpers ---

test("parsePkceCompletionPayload parses URLs and raw codes cleanly", () => {
  assert.deepEqual(
    parsePkceCompletionPayload("https://login.rema.no/callback?code=abc12345&state=xyz"),
    { redirect_url: "https://login.rema.no/callback?code=abc12345&state=xyz" },
  );
  assert.deepEqual(
    parsePkceCompletionPayload("http://localhost:8000/auth/callback?code=coop-code"),
    { redirect_url: "http://localhost:8000/auth/callback?code=coop-code" },
  );
  assert.deepEqual(
    parsePkceCompletionPayload("  coop-auth-code-9988  "),
    { code: "coop-auth-code-9988" },
  );
  assert.equal(parsePkceCompletionPayload(""), null);
  assert.equal(parsePkceCompletionPayload("   "), null);
  assert.equal(parsePkceCompletionPayload(null), null);
});

test("buildCookidooLoginPayload handles credentials, default country, and whitespace", () => {
  assert.deepEqual(
    buildCookidooLoginPayload(" chef@example.com ", " my-password "),
    { email: "chef@example.com", password: "my-password", country: "no" },
  );
  assert.deepEqual(
    buildCookidooLoginPayload("chef@example.com", "pw", " DE "),
    { email: "chef@example.com", password: "pw", country: "de" },
  );
  assert.equal(buildCookidooLoginPayload("", "pw"), null);
  assert.equal(buildCookidooLoginPayload("chef@example.com", ""), null);
  assert.equal(buildCookidooLoginPayload(null, null), null);
});

test("buildTrumfVerifyPayload handles session_id and OTP code", () => {
  assert.deepEqual(
    buildTrumfVerifyPayload(" sess-trumf-101 ", " 654321 "),
    { session_id: "sess-trumf-101", otp: "654321" },
  );
  assert.equal(buildTrumfVerifyPayload("", "123456"), null);
  assert.equal(buildTrumfVerifyPayload("sess-1", ""), null);
  assert.equal(buildTrumfVerifyPayload(null, null), null);
});

// --- DOM Test Harness for openConnectorModal ---

class MockNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.className = className;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this.open = false;
    this.ownText = text ? String(text) : "";
  }
  set textContent(v) {
    this.ownText = String(v);
    this.children = [];
  }
  get textContent() {
    return this.ownText + this.children.map((c) => c.textContent).join("");
  }
  append(...children) {
    for (const c of children) {
      if (c) this.children.push(typeof c === "string" ? new MockNode("span", c) : c);
    }
  }
  replaceChildren(...children) {
    this.ownText = "";
    this.children = children.filter(Boolean);
  }
  setAttribute(n, v) {
    this.attributes[n] = String(v);
    if (n === "type") this.type = String(v);
  }
  getAttribute(n) {
    if (n === "type" && this.type !== undefined) return this.type;
    return this.attributes[n] ?? null;
  }
  addEventListener(n, h) {
    this.listeners[n] = h;
  }
  focus() {
    this.focused = true;
  }
  async click() {
    if (!this.disabled && this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {}, target: this });
    }
  }
  async submit() {
    if (this.listeners.submit) {
      await this.listeners.submit({ preventDefault: () => {}, target: this });
    }
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
  querySelectorAll(sel) {
    const res = [];
    const walk = (node) => {
      let match = false;
      if (sel.startsWith(".")) {
        match = node.className && node.className.split(/\s+/).includes(sel.slice(1));
      } else if (sel.startsWith("#")) {
        match = node.id === sel.slice(1);
      } else {
        match = node.tagName === sel.toUpperCase();
      }
      if (match) res.push(node);
      for (const c of node.children) walk(c);
    };
    for (const c of this.children) walk(c);
    return res;
  }
}

function extractFunction(source, name) {
  const match = source.match(
    new RegExp(
      `(?:async )?function ${name}\\([\\s\\S]*?\\n\\}(?=\\n(?:async )?function |\\n(?:const|let|var) |\\n\\/\\/|$)`,
      "u",
    ),
  );
  assert.ok(match, `app.js should define ${name}`);
  return match[0];
}

function setupModalHarness(apiHandler) {
  const fs = require("node:fs");
  const path = require("node:path");
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");

  const modal = new MockNode("dialog");
  modal.id = "modal";
  modal.open = false;
  modal.close = () => {
    modal.open = false;
  };
  modal.showModal = () => {
    modal.open = true;
  };
  const modalContent = new MockNode("div");
  modalContent.id = "modal-content";

  const calls = [];
  const openedWindows = [];
  const notices = [];

  const documentMock = {
    createElement: (tag) => new MockNode(tag),
    querySelector: (sel) => (sel === "#modal" ? modal : sel === "#modal-content" ? modalContent : null),
  };

  const windowMock = {
    KitchenSettingsView: require("../src/home_kitchen/static/settings-view.js"),
    open: (url, target, features) => {
      openedWindows.push({ url, target, features });
    },
  };

  const fns = ["el", "add", "button", "field", "formSubmit", "openConnectorModal"];
  const context = new Function(
    "document",
    "$",
    "api",
    "notify",
    "loadConnectionStatus",
    "refreshView",
    "window",
    "openDialog",
    `
    ${fns.map((f) => extractFunction(appSource, f)).join("\n")}
    return { openConnectorModal };
    `,
  )(
    documentMock,
    (sel) => documentMock.querySelector(sel),
    async (path, method = "GET", body) => {
      calls.push({ path, method, body });
      return apiHandler(path, method, body);
    },
    (msg) => notices.push(msg),
    async () => {},
    async () => {},
    windowMock,
    (heading) => {
      modal.open = true;
      modalContent.replaceChildren();
      return modalContent;
    },
  );

  function findInput(labelText) {
    const labels = modalContent.querySelectorAll("label");
    for (const l of labels) {
      const span = l.querySelectorAll("span")[0];
      if (span && span.textContent.trim() === labelText) {
        return l.querySelectorAll("input")[0] || null;
      }
    }
    return null;
  }

  function findButton(btnText) {
    const buttons = modalContent.querySelectorAll("button");
    return buttons.find((b) => b.textContent.trim() === btnText) || null;
  }

  function findDetails(summaryText) {
    const details = modalContent.querySelectorAll("details");
    return (
      details.find((d) => {
        const sum = d.querySelectorAll("summary")[0];
        return sum && sum.textContent.trim() === summaryText;
      }) || null
    );
  }

  function findError(parent = modalContent) {
    const errors = parent.querySelectorAll("p");
    return errors.find((p) => p.className && p.className.includes("form-error")) || null;
  }

  return {
    openConnectorModal: context.openConnectorModal,
    modal,
    modalContent,
    calls,
    openedWindows,
    notices,
    findInput,
    findButton,
    findDetails,
    findError,
  };
}

test("Rema 1000 guided PKCE flow opens login, reveals step 2, and completes with URL or code", async () => {
  const harness = setupModalHarness(async (path, method, body) => {
    if (path === "/integrations/rema1000/pkce/start") {
      return { authorize_url: "https://login.rema.no/oauth/authorize?scope=all" };
    }
    if (path === "/integrations/rema1000/pkce/complete") {
      return { status: "connected" };
    }
    throw new Error("Unexpected endpoint " + path);
  });

  harness.openConnectorModal({ id: "rema1000", name: "Rema 1000" });
  assert.equal(harness.modal.open, true);

  // Step 1: Open login button
  const openLoginBtn = harness.findButton("Åpne innlogging");
  assert.ok(openLoginBtn, "Step 1 has 'Åpne innlogging' button");

  const step2Forms = harness.modalContent.querySelectorAll("form");
  const step2 = step2Forms.find((f) => f.className && f.className.includes("connector-step-2"));
  assert.ok(step2, "Step 2 form exists");
  assert.equal(step2.hidden, true, "Step 2 is initially hidden");

  // Click Åpne innlogging
  await openLoginBtn.click();
  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/rema1000/pkce/start",
    method: "POST",
    body: undefined,
  });
  assert.deepEqual(harness.openedWindows, [
    {
      url: "https://login.rema.no/oauth/authorize?scope=all",
      target: "_blank",
      features: "noopener,noreferrer",
    },
  ]);
  assert.equal(step2.hidden, false, "Step 2 is revealed after opening login");

  // Step 2 input
  const pasteInput = harness.findInput("Lim inn nettadressen eller koden etter innlogging");
  assert.ok(pasteInput, "Step 2 has paste input");

  // Submit with full URL
  pasteInput.value = "https://rema.no/auth/callback?code=rema-auth-token-123";
  await step2.submit();

  assert.equal(harness.calls.length, 2);
  assert.deepEqual(harness.calls[1], {
    path: "/integrations/rema1000/pkce/complete",
    method: "POST",
    body: { redirect_url: "https://rema.no/auth/callback?code=rema-auth-token-123" },
  });
  assert.equal(harness.modal.open, false, "Modal closes on successful connection");
  assert.deepEqual(harness.notices, ["Rema 1000 er tilkoblet."]);

  // Also test submitting raw code (not a URL)
  const codeHarness = setupModalHarness(async (path) => {
    if (path === "/integrations/rema1000/pkce/start") return { authorize_url: "https://login.rema.no/auth" };
    return { status: "connected" };
  });
  codeHarness.openConnectorModal({ id: "rema1000", name: "Rema 1000" });
  await codeHarness.findButton("Åpne innlogging").click();
  const codeStep2 = codeHarness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  const codeInput = codeHarness.findInput("Lim inn nettadressen eller koden etter innlogging");
  codeInput.value = "raw-auth-code-789";
  await codeStep2.submit();

  assert.deepEqual(codeHarness.calls[1], {
    path: "/integrations/rema1000/pkce/complete",
    method: "POST",
    body: { code: "raw-auth-code-789" },
  });

  // Test submitting empty input shows Bokmål error
  const emptyHarness = setupModalHarness(async () => ({ authorize_url: "https://login.rema.no/auth" }));
  emptyHarness.openConnectorModal({ id: "rema1000", name: "Rema 1000" });
  await emptyHarness.findButton("Åpne innlogging").click();
  const emptyStep2 = emptyHarness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  await emptyStep2.submit();
  const err = emptyHarness.findError(emptyStep2);
  assert.ok(err);
  assert.match(err.textContent, /Vennligst lim inn nettadressen eller koden/);
  assert.equal(emptyHarness.calls.length, 1, "No complete API call when empty");
});

test("Coop guided PKCE flow executes against coop endpoints", async () => {
  const harness = setupModalHarness(async (path) => {
    if (path === "/integrations/coop/pkce/start") return { authorize_url: "https://medlem.coop.no/oidc" };
    return { status: "connected" };
  });

  harness.openConnectorModal({ id: "coop", name: "Coop" });
  await harness.findButton("Åpne innlogging").click();
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/coop/pkce/start",
    method: "POST",
    body: undefined,
  });

  const step2 = harness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  const pasteInput = harness.findInput("Lim inn nettadressen eller koden etter innlogging");
  pasteInput.value = "https://coop.no/callback?code=coop-code-456";
  await step2.submit();

  assert.deepEqual(harness.calls[1], {
    path: "/integrations/coop/pkce/complete",
    method: "POST",
    body: { redirect_url: "https://coop.no/callback?code=coop-code-456" },
  });
  assert.deepEqual(harness.notices, ["Coop er tilkoblet."]);
});

test("Rema 1000 and Coop expandable manual details fallback to connect endpoint", async () => {
  const harness = setupModalHarness(async () => ({ status: "connected" }));
  harness.openConnectorModal({ id: "rema1000", name: "Rema 1000" });

  const details = harness.findDetails("Avansert: lim inn sesjonstoken manuelt");
  assert.ok(details, "Details summary matches 'Avansert: lim inn sesjonstoken manuelt'");

  const tokenInput = harness.findInput("Sesjonstoken (token)");
  assert.ok(tokenInput, "Token input is present inside details");
  assert.equal(tokenInput.type, "password", "Token input is masked as password");

  const expiryInput = harness.findInput("Utløpsdato (valgfritt)");
  assert.ok(expiryInput, "Optional expiry date field is present");

  tokenInput.value = "manual-token-secret-123";
  expiryInput.value = "2026-12-31";

  const manualForm = details.querySelectorAll("form")[0];
  await manualForm.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/rema1000/connect",
    method: "POST",
    body: {
      auth_mode: "session_token",
      session_token: "manual-token-secret-123",
      expires_at: new Date("2026-12-31T23:59:59Z").toISOString(),
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Rema 1000 er tilkoblet."]);
});

test("Cookidoo interactive login flow sends credentials and displays clear security note", async () => {
  const harness = setupModalHarness(async (path, method, body) => ({ status: "connected" }));
  harness.openConnectorModal({ id: "cookidoo", name: "Cookidoo / Thermomix TM6" });

  // Verify clear Bokmål note that only the resulting OAuth token is saved encrypted
  assert.match(
    harness.modalContent.textContent,
    /Passordet lagres aldri – kun det resulterende OAuth-tokenet lagres trygt kryptert/,
  );

  const emailInput = harness.findInput("E-post");
  const passwordInput = harness.findInput("Passord");
  const countryInput = harness.findInput("Land");

  assert.ok(emailInput, "Email input is present");
  assert.ok(passwordInput, "Password input is present");
  assert.equal(passwordInput.type, "password", "Password field has type='password'");
  assert.ok(countryInput, "Country input is present");
  assert.equal(countryInput.value, "no", "Country defaults to 'no'");

  emailInput.value = "chef@homekitchen.no";
  passwordInput.value = "thermomix2026";
  countryInput.value = "no";

  const loginForm = harness.modalContent.querySelectorAll("form")[0];
  await loginForm.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/cookidoo/login",
    method: "POST",
    body: {
      email: "chef@homekitchen.no",
      password: "thermomix2026",
      country: "no",
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Cookidoo / Thermomix TM6 er tilkoblet."]);

  // Test validation error when password is empty
  const errHarness = setupModalHarness(async () => ({}));
  errHarness.openConnectorModal({ id: "cookidoo", name: "Cookidoo" });
  const errEmail = errHarness.findInput("E-post");
  errEmail.value = "chef@homekitchen.no";
  const errForm = errHarness.modalContent.querySelectorAll("form")[0];
  await errForm.submit();
  const errAlert = errHarness.findError(errForm);
  assert.ok(errAlert);
  assert.match(errAlert.textContent, /Vennligst oppgi både e-post og passord/);
  assert.equal(errHarness.calls.length, 0);
});

test("Cookidoo manual cookie fallback details form calls connect endpoint", async () => {
  const harness = setupModalHarness(async () => ({ status: "connected" }));
  harness.openConnectorModal({ id: "cookidoo", name: "Cookidoo / Thermomix TM6" });

  const details = harness.findDetails("Avansert: lim inn informasjonskapsel manuelt");
  assert.ok(details, "Details summary matches 'Avansert: lim inn informasjonskapsel manuelt'");

  const cookieInput = harness.findInput("Informasjonskapsel (cookie)");
  assert.ok(cookieInput);
  assert.equal(cookieInput.type, "password", "Cookie input has type='password'");

  cookieInput.value = "v_session=cookidoo_secret_cookie";
  const manualForm = details.querySelectorAll("form")[0];
  await manualForm.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/cookidoo/connect",
    method: "POST",
    body: {
      auth_mode: "session_cookie",
      session_cookie: "v_session=cookidoo_secret_cookie",
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Cookidoo / Thermomix TM6 er tilkoblet."]);
});

test("Trumf interactive SMS / OTP 2-step flow requests code, stores session_id, and verifies OTP", async () => {
  const harness = setupModalHarness(async (path, method, body) => {
    if (path === "/integrations/trumf/login/start") {
      return { session_id: "trumf-sess-abc-777" };
    }
    if (path === "/integrations/trumf/login/verify") {
      return { status: "connected" };
    }
    throw new Error("Unexpected endpoint " + path);
  });

  harness.openConnectorModal({ id: "trumf", name: "Trumf" });

  const phoneInput = harness.findInput("Mobilnummer");
  assert.ok(phoneInput, "Phone number input is present");

  const sendBtn = harness.findButton("Send engangskode");
  assert.ok(sendBtn, "Button 'Send engangskode' is present");

  const forms = harness.modalContent.querySelectorAll("form");
  const step2 = forms.find((f) => f.className && f.className.includes("connector-step-2"));
  assert.ok(step2, "Step 2 form is present");
  assert.equal(step2.hidden, true, "Step 2 is initially hidden");

  // Step 1: Submit phone number
  phoneInput.value = "91234567";
  const step1Form = forms.find((f) => f.className && f.className.includes("connector-step-1"));
  await step1Form.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/trumf/login/start",
    method: "POST",
    body: { phone: "91234567" },
  });
  assert.equal(step2.hidden, false, "Step 2 is revealed after sending code");

  // Step 2: OTP code input and Bekreft kode button
  const otpInput = harness.findInput("Engangskode");
  assert.ok(otpInput, "OTP input is present");

  const verifyBtn = harness.findButton("Bekreft kode");
  assert.ok(verifyBtn, "Button 'Bekreft kode' is present");

  otpInput.value = "456789";
  await step2.submit();

  assert.equal(harness.calls.length, 2);
  assert.deepEqual(harness.calls[1], {
    path: "/integrations/trumf/login/verify",
    method: "POST",
    body: {
      session_id: "trumf-sess-abc-777",
      otp: "456789",
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Trumf er tilkoblet."]);

  // Test validation error when OTP is empty
  const errHarness = setupModalHarness(async (path) => {
    if (path === "/integrations/trumf/login/start") return { session_id: "sess-1" };
    return {};
  });
  errHarness.openConnectorModal({ id: "trumf", name: "Trumf" });
  errHarness.findInput("Mobilnummer").value = "91234567";
  const errStep1 = errHarness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-1"));
  await errStep1.submit();
  const errStep2 = errHarness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  await errStep2.submit();
  const errAlert = errHarness.findError(errStep2);
  assert.ok(errAlert);
  assert.match(errAlert.textContent, /Vennligst oppgi engangskoden/);
  assert.equal(errHarness.calls.length, 1, "Verify endpoint not called without OTP");
});

test("Trumf manual cookie fallback details form calls connect endpoint", async () => {
  const harness = setupModalHarness(async () => ({ status: "connected" }));
  harness.openConnectorModal({ id: "trumf", name: "Trumf" });

  const details = harness.findDetails("Avansert: lim inn informasjonskapsel manuelt");
  assert.ok(details, "Details summary matches 'Avansert: lim inn informasjonskapsel manuelt'");

  const cookieInput = harness.findInput("Informasjonskapsel (cookie)");
  assert.ok(cookieInput);
  assert.equal(cookieInput.type, "password", "Cookie input has type='password'");

  cookieInput.value = "trumf_session=trumf_cookie_999";
  const manualForm = details.querySelectorAll("form")[0];
  await manualForm.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/trumf/connect",
    method: "POST",
    body: {
      auth_mode: "session_cookie",
      session_cookie: "trumf_session=trumf_cookie_999",
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Trumf er tilkoblet."]);
});

test("Bokmål labels and password masking across all connector modals", () => {
  const harness = setupModalHarness(async () => ({}));

  for (const provider of [
    { id: "rema1000", name: "Rema 1000" },
    { id: "coop", name: "Coop" },
    { id: "cookidoo", name: "Cookidoo / Thermomix TM6" },
    { id: "trumf", name: "Trumf" },
  ]) {
    harness.openConnectorModal(provider);
    const text = harness.modalContent.textContent;

    // Check Bokmål wording
    assert.match(text, /Koble til|Logg inn|Avansert|Fullfør/);
    // Ensure English words are not used in UI text
    assert.doesNotMatch(text, /\bConnect to\b|\bLog in with\b|\bPassword is not saved\b|\bSend code\b/i);

    // Verify password masking for sensitive inputs
    const passwordInputs = harness.modalContent.querySelectorAll("input").filter((i) => i.type === "password");
    assert.ok(
      passwordInputs.length >= 1,
      `Provider ${provider.id} must have at least one password-masked input`,
    );
  }
});
