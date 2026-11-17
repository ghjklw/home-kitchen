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
  buildTrumfSessionCookie,
  buildTrumfConnectPayload,
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

test("buildTrumfConnectPayload handles cookie and optional expiry date", () => {
  assert.deepEqual(
    buildTrumfConnectPayload("  trumf_session=test12345  "),
    { auth_mode: "session_cookie", session_cookie: "trumf_session=test12345" },
  );
  assert.deepEqual(
    buildTrumfConnectPayload("trumf_session=test12345", "2026-12-31"),
    {
      auth_mode: "session_cookie",
      session_cookie: "trumf_session=test12345",
      expires_at: "2026-12-31T23:59:59.000Z",
    },
  );
  assert.equal(buildTrumfConnectPayload("", null), null);
  assert.equal(buildTrumfConnectPayload(null, null), null);
});

test("buildTrumfSessionCookie and buildTrumfConnectPayload normalize Cookie headers cleanly", () => {
  assert.equal(
    buildTrumfSessionCookie("Cookie: __Secure-authjs.session-token.0=chunk0-val; __Secure-authjs.session-token.1=chunk1-val"),
    "__Secure-authjs.session-token.0=chunk0-val; __Secure-authjs.session-token.1=chunk1-val",
  );
  assert.equal(
    buildTrumfSessionCookie(
      "__Secure-authjs.session-token.0=chunk0-val\n__Secure-authjs.session-token.1=chunk1-val",
    ),
    "__Secure-authjs.session-token.0=chunk0-val; __Secure-authjs.session-token.1=chunk1-val",
  );
  assert.deepEqual(
    buildTrumfConnectPayload("Cookie: __Secure-authjs.session-token.0=part0; __Secure-authjs.session-token.1=part1", "2027-01-15"),
    {
      auth_mode: "session_cookie",
      session_cookie: "__Secure-authjs.session-token.0=part0; __Secure-authjs.session-token.1=part1",
      expires_at: "2027-01-15T23:59:59.000Z",
    },
  );
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

test("Trumf primary session cookie flow uses single Cookie-header field and optional SMS OTP", async () => {
  const harness = setupModalHarness(async (path, method, body) => {
    if (path === "/integrations/trumf/connect") {
      return { status: "connected" };
    }
    if (path === "/integrations/trumf/login/start") {
      return { session_id: "otp-sess-1", detail: "Engangskode er sendt på SMS." };
    }
    if (path === "/integrations/trumf/login/verify") {
      return { status: "connected" };
    }
    throw new Error("Unexpected endpoint " + path);
  });

  harness.openConnectorModal({
    id: "trumf",
    name: "Trumf",
    supported_auth_modes: ["session_cookie", "interactive_otp"],
  });

  // No OAuth PKCE dead-end button
  assert.equal(harness.findButton("Koble til med Trumf-konto (OAuth PKCE)"), null);

  // Primary session cookie section (not collapsed inside <details>)
  const primarySection = harness.modalContent.querySelectorAll(".connector-trumf-cookie")[0];
  assert.ok(primarySection, "Primary Trumf session cookie section is rendered");
  assert.match(primarySection.textContent, /Sesjonsinformasjonskapsel/);

  const openBtn = harness.findButton("Åpne Trumf-innlogging");
  assert.ok(openBtn, "Button 'Åpne Trumf-innlogging' is present");

  const cookieInput = harness.findInput("Informasjonskapsel (cookie)");
  assert.ok(cookieInput);
  assert.equal(cookieInput.type, "password");
  assert.equal(harness.findInput("__Secure-authjs.session-token.0 (valgfritt)"), null);
  assert.equal(harness.findInput("__Secure-authjs.session-token.1 (valgfritt)"), null);

  // 1. Submit using Cookie header (even with leading 'Cookie:' prefix)
  cookieInput.value =
    "Cookie: __Secure-authjs.session-token.0=tok-part-zero; __Secure-authjs.session-token.1=tok-part-one";
  const cookieForm = primarySection.querySelectorAll("form")[0];
  await cookieForm.submit();

  assert.equal(harness.calls.length, 1);
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/trumf/connect",
    method: "POST",
    body: {
      auth_mode: "session_cookie",
      session_cookie:
        "__Secure-authjs.session-token.0=tok-part-zero; __Secure-authjs.session-token.1=tok-part-one",
    },
  });
  assert.equal(harness.modal.open, false);
  assert.deepEqual(harness.notices, ["Trumf er tilkoblet."]);

  // 2. Submit using raw Cookie header value (without 'Cookie:' prefix)
  const fullHarness = setupModalHarness(async () => ({ status: "connected" }));
  fullHarness.openConnectorModal({ id: "trumf", name: "Trumf" });
  fullHarness.findInput("Informasjonskapsel (cookie)").value =
    "__Secure-authjs.session-token.0=a; __Secure-authjs.session-token.1=b";
  const fullPrimary = fullHarness.modalContent.querySelectorAll(".connector-trumf-cookie")[0];
  await fullPrimary.querySelectorAll("form")[0].submit();
  assert.deepEqual(fullHarness.calls[0], {
    path: "/integrations/trumf/connect",
    method: "POST",
    body: {
      auth_mode: "session_cookie",
      session_cookie: "__Secure-authjs.session-token.0=a; __Secure-authjs.session-token.1=b",
    },
  });

  // 3. Optional secondary SMS OTP flow
  const otpDetails = harness.findDetails("Bekreft med SMS-kode");
  assert.ok(otpDetails, "Optional secondary 'Bekreft med SMS-kode' details is present");
  harness.openConnectorModal({
    id: "trumf",
    name: "Trumf",
    supported_auth_modes: ["session_cookie", "interactive_otp"],
  });
  const phoneInput = harness.findInput("Telefonnummer");
  phoneInput.value = "+4790000000";
  const phoneForm = harness.modalContent
    .querySelectorAll("form")
    .find((f) => f.className.includes("connector-trumf-otp-start"));
  await phoneForm.submit();
  assert.deepEqual(harness.calls[1], {
    path: "/integrations/trumf/login/start",
    method: "POST",
    body: { phone: "+4790000000" },
  });
  const otpInput = harness.findInput("Engangskode fra SMS");
  otpInput.value = "123456";
  const verifyForm = harness.modalContent
    .querySelectorAll("form")
    .find((f) => f.className.includes("connector-trumf-otp-verify"));
  await verifyForm.submit();
  assert.deepEqual(harness.calls[2], {
    path: "/integrations/trumf/login/verify",
    method: "POST",
    body: { session_id: "otp-sess-1", otp: "123456" },
  });

  // 4. Test validation error when cookie field is empty
  const errHarness = setupModalHarness(async () => ({}));
  errHarness.openConnectorModal({ id: "trumf", name: "Trumf" });
  const errPrimary = errHarness.modalContent.querySelectorAll(".connector-trumf-cookie")[0];
  const errForm = errPrimary.querySelectorAll("form")[0];
  await errForm.submit();
  const errAlert = errHarness.findError(errForm);
  assert.ok(errAlert);
  assert.match(errAlert.textContent, /Vennligst oppgi informasjonskapsel/);
  assert.equal(errHarness.calls.length, 0);
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

test("api() error handling distinguishes Entra ID 401 from integration 401 provider detail", async () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");

  // Extract api function
  const fnMatch = appSource.match(/async function api\(path, method = "GET", body\) \{[\s\S]*?\n\}/);
  assert.ok(fnMatch, "api function found in app.js");

  let mockResponse;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => mockResponse;

  const apiFn = new Function(`return (${fnMatch[0]})`)();

  try {
    // 1. Regular endpoint 401: forward-auth Entra ID household error
    mockResponse = {
      ok: false,
      status: 401,
      json: async () => ({ detail: "Unauthorized" }),
    };
    await assert.rejects(
      async () => apiFn("/meals"),
      /Logg inn via husholdningens godkjente tilgangslenke, og prøv igjen\./,
    );

    // 2. Integration endpoint 401: uses actual server detail in Bokmål
    mockResponse = {
      ok: false,
      status: 401,
      json: async () => ({ detail: "Ugyldig e-postadresse eller passord for Cookidoo." }),
    };
    await assert.rejects(
      async () => apiFn("/integrations/cookidoo/login", "POST", {}),
      /Ugyldig e-postadresse eller passord for Cookidoo\./,
    );

    // 3. Integration endpoint 401 without detail: uses Bokmål fallback
    mockResponse = {
      ok: false,
      status: 401,
      json: async () => ({}),
    };
    await assert.rejects(
      async () => apiFn("/integrations/cookidoo/login", "POST", {}),
      /Ugyldig påloggingsinformasjon eller manglende tilgang\. Vennligst sjekk detaljene og prøv igjen\./,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Trumf login URL points to https://www.trumf.no/trumf-profil/kvitteringer without 404 /logg-inn path", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
  assert.doesNotMatch(appSource, /trumf\.no\/logg-inn/);

  const harness = setupModalHarness(async () => ({}));
  harness.openConnectorModal({ id: "trumf", name: "Trumf" });
  harness.findButton("Åpne Trumf-innlogging").click();
  assert.deepEqual(harness.openedWindows[0], {
    url: "https://www.trumf.no/trumf-profil/kvitteringer",
    target: "_blank",
    features: "noopener,noreferrer",
  });
});

test("guided modal supports optional account label and addAccount multi-account flow", async () => {
  const harness = setupModalHarness(async (path) => {
    if (path === "/integrations/rema1000/pkce/start") {
      return { authorize_url: "https://id.rema.no/authorization" };
    }
    return { status: "connected" };
  });

  harness.openConnectorModal({ id: "rema1000", name: "Rema 1000" }, { addAccount: true });
  const labelInput = harness.findInput("Kontoetikett (valgfritt, f.eks. Malo)");
  assert.ok(labelInput, "Account label input exists");
  labelInput.value = "Partner";

  await harness.findButton("Åpne innlogging").click();
  assert.deepEqual(harness.calls[0], {
    path: "/integrations/rema1000/pkce/start",
    method: "POST",
    body: { account_label: "Partner", add_account: true },
  });

  const step2 = harness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  const pasteInput = harness.findInput("Lim inn nettadressen eller koden etter innlogging");
  pasteInput.value = "code-partner-123";
  await step2.submit();

  assert.deepEqual(harness.calls[1], {
    path: "/integrations/rema1000/pkce/complete",
    method: "POST",
    body: { code: "code-partner-123", account_label: "Partner", add_account: true },
  });
});

test("settings view helpers format multi-account display names, auth labels, and expiry", () => {
  const {
    accountDisplayName,
    formatAccountExpiry,
  } = require("../src/home_kitchen/static/settings-view.js");

  assert.equal(accountDisplayName({ account_id: "default", account_label: "Malo" }), "Malo");
  assert.equal(accountDisplayName({ account_id: "default", account_label: null }), "Standardkonto");
  assert.equal(accountDisplayName({ account_id: "acc_12345678", account_label: "" }), "acc_12345678");
  assert.ok(formatAccountExpiry({ token_expires_at: "2026-12-31T12:00:00Z" }));
  assert.equal(formatAccountExpiry({}), null);
  assert.equal(integrationAuthLabel("oauth_pkce", true), "OAuth-innlogging er lagret (hemmeligheten er skjult)");
  assert.equal(integrationAuthLabel("interactive_otp", true), "SMS-innlogging er lagret (hemmeligheten er skjult)");
});

test("renderConnectionSettings renders per-account rows, actions, and add-another-account button", async () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const appSource = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");

  const calls = [];
  const modalCalls = [];
  const documentMock = {
    createElement: (tag) => new MockNode(tag),
  };
  const windowMock = {
    KitchenSettingsView: require("../src/home_kitchen/static/settings-view.js"),
  };
  const state = {
    settingsRefreshInFlight: false,
    oda: null,
    integrationsError: null,
    odaStatusError: null,
    integrations: [
      {
        id: "rema1000",
        name: "Rema 1000",
        status: "connected",
        auth_mode: "oauth_pkce",
        supported_auth_modes: ["oauth_pkce", "session_token"],
        detail: "Rema 1000",
        accounts: [
          {
            account_id: "default",
            account_label: "Malo",
            auth_mode: "oauth_pkce",
            auth_type: "oauth_pkce",
            status: "connected",
            connected_by_actor_id: "user-malo",
            token_expires_at: "2026-12-31T12:00:00Z",
            refresh_available: true,
          },
          {
            account_id: "acc_abc12345",
            account_label: "Partner",
            auth_mode: "session_token",
            auth_type: "session_token",
            status: "expired",
            connected_by_actor_id: "user-partner",
            token_expires_at: "2025-01-01T12:00:00Z",
            refresh_available: false,
          },
        ],
      },
    ],
  };

  const fns = [
    "el",
    "add",
    "button",
    "disconnectConnector",
    "disconnectConnectorAccount",
    "refreshConnectorAccount",
    "renderConnectionSettings",
  ];
  const { renderConnectionSettings } = new Function(
    "document",
    "state",
    "window",
    "api",
    "notify",
    "loadConnectionStatus",
    "refreshView",
    "refreshConnectionSettings",
    "connectOdaAccount",
    "disconnectOdaAccount",
    "openConnectorModal",
    `
    ${fns.map((f) => extractFunction(appSource, f)).join("\n")}
    return { renderConnectionSettings };
    `,
  )(
    documentMock,
    state,
    windowMock,
    async (reqPath, method = "GET", body) => {
      calls.push({ reqPath, method, body });
      return {};
    },
    () => {},
    async () => {},
    async () => {},
    async () => {},
    async () => {},
    async () => {},
    (integration, opts) => modalCalls.push({ id: integration.id, opts }),
  );

  const section = renderConnectionSettings();
  const rows = section.querySelectorAll(".connection-account-row");
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /Malo/);
  assert.match(rows[0].textContent, /Koblet til av user-malo/);
  assert.match(rows[1].textContent, /Partner/);
  assert.match(rows[1].textContent, /Koblet til av user-partner/);

  const addAnotherBtn = section.querySelectorAll("button").find((b) => b.textContent === "Koble til en konto til");
  assert.ok(addAnotherBtn, "Should render 'Koble til en konto til' button");
  await addAnotherBtn.click();
  assert.deepEqual(modalCalls[0], { id: "rema1000", opts: { addAccount: true } });

  // Clicking Forny on refreshable account calls refresh endpoint with account_id
  const firstRenewBtn = rows[0].querySelectorAll("button").find((b) => b.textContent === "Forny");
  await firstRenewBtn.click();
  assert.deepEqual(calls[0], {
    reqPath: "/integrations/rema1000/refresh?account_id=default",
    method: "POST",
    body: undefined,
  });

  // Clicking Koble fra on second account calls per-account DELETE endpoint
  const secondDisconnectBtn = rows[1].querySelectorAll("button").find((b) => b.textContent === "Koble fra");
  await secondDisconnectBtn.click();
  assert.deepEqual(calls[1], {
    reqPath: "/integrations/rema1000/accounts/acc_abc12345",
    method: "DELETE",
    body: undefined,
  });
});

test("Coop modal explains no.coop.members://auth/callback and Trumf modal explains full Cookie header", async () => {
  const harness = setupModalHarness(async (path) => {
    if (path === "/integrations/coop/pkce/start") {
      return { authorize_url: "https://login.coop.no/authorize?client_id=7WrQEdeXwUudArpQVjmZEvrTgVs1WkRr" };
    }
    return { status: "connected" };
  });

  harness.openConnectorModal({ id: "coop", name: "Coop" });
  const coopText = harness.modalContent.textContent;
  assert.match(coopText, /no\.coop\.members:\/\/auth\/callback/);
  assert.doesNotMatch(coopText, /minside\.coop\.no/);

  await harness.findButton("Åpne innlogging").click();
  const coopStep2 = harness.modalContent.querySelectorAll("form").find((f) => f.className.includes("connector-step-2"));
  const pasteInput = harness.findInput("Lim inn nettadressen eller koden etter innlogging");
  pasteInput.value = "no.coop.members://auth/callback?code=coop-app-code&state=state-1";
  await coopStep2.submit();
  assert.deepEqual(harness.calls[1], {
    path: "/integrations/coop/pkce/complete",
    method: "POST",
    body: { redirect_url: "no.coop.members://auth/callback?code=coop-app-code&state=state-1" },
  });

  harness.openConnectorModal({ id: "trumf", name: "Trumf" });
  const trumfText = harness.modalContent.textContent;
  assert.doesNotMatch(trumfText, /OAuth PKCE/);
  assert.match(trumfText, /Sesjonsinformasjonskapsel/);
  assert.match(trumfText, /https:\/\/www\.trumf\.no\/trumf-profil\/kvitteringer/);
  assert.match(trumfText, /F12.*Network.*F5/);
  assert.match(trumfText, /Headers → Request Headers/);
  assert.match(trumfText, /Cookie:/);
  assert.match(trumfText, /ikke Set-Cookie/);
  assert.match(trumfText, /Copy value/);
  assert.doesNotMatch(trumfText, /__Secure-authjs\.session-token\.0 \(valgfritt\)/);
  assert.doesNotMatch(trumfText, /__Secure-authjs\.session-token\.1 \(valgfritt\)/);
  assert.match(trumfText, /Bekreft med SMS-kode/);
});
