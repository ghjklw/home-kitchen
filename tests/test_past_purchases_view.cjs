"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const pastPurchasesView = require("../src/home_kitchen/static/past-purchases-view.js");

class MockNode {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this._textContent = "";
    this.className = "";
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = new Map();
    this.dataset = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.hidden = false;
    this.type = "text";
  }

  get id() {
    return this.attributes.id || "";
  }

  set id(val) {
    if (val) this.attributes.id = String(val);
    else delete this.attributes.id;
  }

  get textContent() {
    return (this._textContent || "") + this.children.map((c) => c.textContent || "").join("");
  }

  set textContent(val) {
    this._textContent = String(val ?? "");
    this.children = [];
  }

  append(...children) {
    for (const child of children) {
      if (!child) continue;
      if (typeof child === "string") {
        const span = new MockNode("#TEXT");
        span._textContent = child;
        span.parentNode = this;
        this.children.push(span);
      } else {
        child.parentNode = this;
        this.children.push(child);
      }
    }
  }

  replaceChildren(...children) {
    this._textContent = "";
    this.children = [];
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, handler) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(handler);
  }

  async dispatchEvent(event) {
    const evt = { target: this, preventDefault() {}, ...event };
    for (const handler of this.listeners.get(evt.type) || []) {
      await handler(evt);
    }
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const selectors = selector.split(",").map((s) => s.trim()).filter(Boolean);
    const results = [];
    const matchesPart = (node, sel) => {
      if (!node || !node.tagName || node.tagName === "#TEXT") return false;
      let rest = sel;
      const tagMatch = rest.match(/^[a-zA-Z0-9]+/);
      if (tagMatch) {
        if (node.tagName !== tagMatch[0].toUpperCase()) return false;
        rest = rest.slice(tagMatch[0].length);
      }
      while (rest.length > 0) {
        if (rest.startsWith(".")) {
          const m = rest.match(/^\.([a-zA-Z0-9_-]+)/);
          if (!m) return false;
          if (!node.className.split(/\s+/).includes(m[1])) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith("[")) {
          const m = rest.match(/^\[([a-zA-Z0-9_-]+)(?:=(["']?)(.*?)\2)?\]/);
          if (!m) return false;
          const actual = node.getAttribute(m[1]);
          if (actual === null) return false;
          if (m[3] !== undefined && actual !== m[3]) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith("#")) {
          const m = rest.match(/^#([a-zA-Z0-9_-]+)/);
          if (!m || node.id !== m[1]) return false;
          rest = rest.slice(m[0].length);
        } else {
          return false;
        }
      }
      return true;
    };
    const matchesSelector = (node, sel, rootNode) => {
      const parts = sel.trim().split(/\s+/);
      if (!matchesPart(node, parts[parts.length - 1])) return false;
      let cur = node.parentNode;
      for (let i = parts.length - 2; i >= 0; i--) {
        while (cur && cur !== rootNode && !matchesPart(cur, parts[i])) {
          cur = cur.parentNode;
        }
        if (!cur || !matchesPart(cur, parts[i])) return false;
        cur = cur.parentNode;
      }
      return true;
    };
    const walk = (node) => {
      for (const child of node.children) {
        if (selectors.some((sel) => matchesSelector(child, sel, this))) results.push(child);
        walk(child);
      }
    };
    walk(this);
    return results;
  }
}

function createMockDocument() {
  return {
    createElement: (tag) => new MockNode(tag),
  };
}

function sampleBrowsePayload() {
  return {
    baskets: [
      {
        id: "trumf:t-1",
        provider: "trumf",
        provider_label: "Trumf",
        store_name: "MENY Majorstuen",
        purchased_at: "2026-11-18T14:30:00Z",
        total_amount: "324.50",
        currency: "NOK",
        item_count: 2,
        provenance: "provider_verified",
        is_correction: false,
        revision_count: 1,
        evidence_ref: "t-1",
        lines: [
          {
            id: "trumf:t-1:line:0",
            name: "Tine Helmelk 1L",
            raw_name: "TINE HELMELK 1L",
            canonical_product_id: "pid-melk",
            canonical_product_name: "Helmelk",
            category: "Dairy",
            category_label: "Meieri",
            quantity: "2",
            unit: "l",
            amount: "45.80",
            ean: "7038010001000",
          },
          {
            id: "trumf:t-1:line:1",
            name: "Norske Gulrøtter 1kg",
            category: "Produce",
            category_label: "Frukt og grønt",
            quantity: "1",
            unit: "kg",
            amount: "29.90",
          },
        ],
      },
      {
        id: "oda:o-1",
        provider: "oda",
        provider_label: "Oda",
        store_name: "Oda",
        purchased_at: "2026-11-04T10:00:00Z",
        total_amount: "199.00",
        currency: "NOK",
        item_count: 1,
        provenance: "provider_verified",
        is_correction: false,
        revision_count: 1,
        lines: [
          {
            id: "oda:o-1:line:0",
            name: "Helmelk",
            canonical_product_id: "pid-melk",
            canonical_product_name: "Helmelk",
            category: "Dairy",
            category_label: "Meieri",
            quantity: "2",
            unit: "l",
            amount: "45.80",
          },
        ],
      },
    ],
    products: [
      {
        key: "pid:pid-melk",
        product_name: "Helmelk",
        canonical_product_id: "pid-melk",
        category: "Dairy",
        category_label: "Meieri",
        last_purchased_at: "2026-11-18T14:30:00Z",
        first_purchased_at: "2026-11-04T10:00:00Z",
        purchase_count: 2,
        total_quantity: "4",
        unit: "l",
        total_spent: "91.60",
        providers: ["trumf", "oda"],
        provider_labels: ["Trumf", "Oda"],
        stores: ["MENY Majorstuen", "Oda"],
        frequency: {
          status: "known",
          average_interval_days: 14.0,
          recency_weighted_interval_days: 14.0,
          weighted_purchases_per_30_days: 2.14,
          recency_score: 1.9,
          sample_size: 2,
          coverage_days: 180,
          is_lower_bound: true,
          confidence: "low",
          frequency_display: "every 14 days",
        },
        recent_items: [
          {
            purchased_at: "2026-11-18T14:30:00Z",
            provider: "trumf",
            provider_label: "Trumf",
            store_name: "MENY Majorstuen",
            product_name: "TINE HELMELK 1L",
            quantity: "2",
            unit: "l",
            amount: "45.80",
          },
        ],
      },
      {
        key: "name:norske gulrøtter 1kg",
        product_name: "Norske Gulrøtter 1kg",
        category: "Produce",
        category_label: "Frukt og grønt",
        last_purchased_at: "2026-11-18T14:30:00Z",
        purchase_count: 1,
        total_quantity: "1",
        unit: "kg",
        total_spent: "29.90",
        providers: ["trumf"],
        provider_labels: ["Trumf"],
        stores: ["MENY Majorstuen"],
        frequency: {
          status: "unknown",
          sample_size: 1,
          is_lower_bound: true,
          frequency_display: "unknown",
        },
        recent_items: [],
      },
    ],
    rows: [
      {
        id: "trumf:t-1:line:0",
        basket_id: "trumf:t-1",
        purchased_at: "2026-11-18T14:30:00Z",
        provider: "trumf",
        provider_label: "Trumf",
        store_name: "MENY Majorstuen",
        product_name: "Helmelk",
        raw_name: "TINE HELMELK 1L",
        canonical_product_id: "pid-melk",
        category: "Dairy",
        category_label: "Meieri",
        quantity: "2",
        unit: "l",
        amount: "45.80",
        ean: "7038010001000",
        provenance: "provider_verified",
      },
      {
        id: "trumf:t-1:line:1",
        basket_id: "trumf:t-1",
        purchased_at: "2026-11-18T14:30:00Z",
        provider: "trumf",
        provider_label: "Trumf",
        store_name: "MENY Majorstuen",
        product_name: "Norske Gulrøtter 1kg",
        category: "Produce",
        category_label: "Frukt og grønt",
        quantity: "1",
        unit: "kg",
        amount: "29.90",
        provenance: "provider_verified",
      },
      {
        id: "oda:o-1:line:0",
        basket_id: "oda:o-1",
        purchased_at: "2026-11-04T10:00:00Z",
        provider: "oda",
        provider_label: "Oda",
        store_name: "Oda",
        product_name: "Helmelk",
        canonical_product_id: "pid-melk",
        category: "Dairy",
        category_label: "Meieri",
        quantity: "2",
        unit: "l",
        amount: "45.80",
        provenance: "provider_verified",
      },
    ],
    providers: [
      { key: "oda", label: "Oda" },
      { key: "trumf", label: "Trumf" },
    ],
    stores: ["MENY Majorstuen", "Oda"],
    categories: [
      { key: "Produce", label: "Frukt og grønt" },
      { key: "Dairy", label: "Meieri" },
    ],
    provider_windows: {
      oda: { provider: "oda", observation_days: 180, total_records: 12, retained_months: null },
      trumf: { provider: "trumf", observation_days: 365, total_records: 24, retained_months: 12 },
    },
  };
}

test("pure formatting, filtering, grouping, and sorting helpers work in Bokmål", () => {
  assert.equal(pastPurchasesView.formatProviderLabel("rema1000"), "REMA 1000");
  assert.equal(pastPurchasesView.formatProviderLabel("trumf"), "Trumf");
  assert.match(pastPurchasesView.formatCurrencyNb("149.8"), /149,80 kr/);
  assert.equal(pastPurchasesView.formatCurrencyNb(null), "Sum ikke oppgitt");
  assert.equal(pastPurchasesView.formatFrequencyBadgeText({ status: "known", average_interval_days: 7.5 }), "ca. hver 7,5. dag");
  assert.equal(pastPurchasesView.formatFrequencyBadgeText({ status: "unknown" }), "Ukjent frekvens");
  assert.match(
    pastPurchasesView.formatFrequencyDetailText({
      status: "known",
      sample_size: 8,
      weighted_purchases_per_30_days: 4.1,
      coverage_days: 180,
    }),
    /ca\. 4,1 per 30 dager.*nedre grense over 180 dager/,
  );

  const payload = sampleBrowsePayload();
  const filtered = pastPurchasesView.filterPurchaseData(payload, {
    provider: "trumf",
    category: "Produce",
    dateFrom: "2026-11-10",
  });
  assert.equal(filtered.baskets.length, 1);
  assert.equal(filtered.baskets[0].lines.length, 1);
  assert.equal(filtered.rows.length, 1);
  assert.equal(filtered.products.length, 1);

  const grouped = pastPurchasesView.groupProductsByCategory(payload.products);
  assert.equal(grouped.length, 2);
  assert.equal(grouped[0].category_label, "Meieri");

  const sortedByName = pastPurchasesView.sortPurchaseRows(payload.rows, "product_name", "asc");
  assert.equal(sortedByName[0].product_name, "Helmelk");
  assert.equal(sortedByName[2].product_name, "Norske Gulrøtter 1kg");
});

test("renderPastPurchasesView supports all 3 Bokmål modes, filtering, sorting, and accessible labels", async () => {
  globalThis.document = createMockDocument();
  const container = document.createElement("div");
  const payload = sampleBrowsePayload();
  payload.baskets[0].lines[1].unit = null;
  Object.assign(payload.baskets[0].lines[0], {
    row_id: "row-milk-1",
    base_product_id: "pid-melk",
    base_product_name: "Helmelk",
    package_size: "1000",
    package_unit: "ml",
    package_inference_source: "description_rule",
    package_confidence: 0.42,
    mapping_source: "description",
    mapping_confidence: 0.48,
    mapping_review_status: "pending",
    mapping_match: "similar",
    mapping_match_source: "opted_in_exact_description",
    suggested_category: "Meieri",
    category_source: "ai",
    category_confidence: 0.61,
    category_review_status: "pending",
  });

  const view = pastPurchasesView.renderPastPurchasesView(container, {
    api: async (endpoint) => {
      assert.equal(endpoint, "/purchase-history");
      return payload;
    },
  });
  await view.ready;

  // Header, lower-bound note, and provider window chips
  assert.match(container.querySelector("h1").textContent, /Tidligere kjøp/);
  assert.match(container.querySelector(".purchase-coverage-note").textContent, /nedre grense/);
  const chips = container.querySelectorAll(".purchase-window-chip");
  assert.equal(chips.length, 2);
  assert.match(chips[1].textContent, /Trumf: 24 kjøp over 365 dager · maks 12 mnd/);

  // Explicit <label for> on all filter controls
  const labels = container.querySelectorAll("label");
  const inputs = [
    container.querySelector(".purchase-filter-search"),
    container.querySelector(".purchase-filter-provider"),
    container.querySelector(".purchase-filter-store"),
    container.querySelector(".purchase-filter-category"),
    container.querySelector(".purchase-filter-date-from"),
    container.querySelector(".purchase-filter-date-to"),
  ];
  assert.equal(inputs.length, 6);
  for (const inp of inputs) {
    assert.ok(inp.id, "every filter input/select should have an id");
    assert.ok(
      labels.some((lbl) => lbl.getAttribute("for") === inp.id),
      `missing <label for="${inp.id}">`,
    );
  }

  // Mode 1 (default): Kvitteringer og handlekurver
  const modeTabs = container.querySelectorAll(".purchase-mode-tab");
  assert.equal(modeTabs.length, 3);
  assert.equal(modeTabs[0].textContent, "Kvitteringer og handlekurver");
  assert.equal(modeTabs[1].textContent, "Produkter og kategorier");
  assert.equal(modeTabs[2].textContent, "Tabell med filtre");

  const basketCards = container.querySelectorAll(".purchase-basket-card");
  assert.equal(basketCards.length, 2);
  assert.match(basketCards[0].textContent, /MENY Majorstuen/);
  assert.match(basketCards[0].textContent, /324,50 kr/);
  assert.match(basketCards[0].textContent, /Vis varelinjer \(2\)/);
  const importedQuantities = basketCards[0].querySelectorAll(".purchase-line-qty");
  assert.equal(importedQuantities[0].textContent, "2 l");
  assert.equal(importedQuantities[0].getAttribute("aria-label"), "Kjøpt mengde: 2 l");
  const importedLines = basketCards[0].querySelectorAll(".purchase-basket-line");
  assert.equal(importedLines[0].querySelector(".purchase-line-missing-unit"), null);
  assert.equal(importedLines[1].querySelector(".purchase-line-qty").textContent, "1");
  assert.equal(importedLines[1].querySelector(".purchase-line-missing-unit").textContent, "Enhet ikke oppgitt");
  const firstLineDetails = basketCards[0].querySelector(".purchase-line-details");
  assert.equal(firstLineDetails.querySelector("summary").textContent, "Flere detaljer");
  assert.match(firstLineDetails.textContent, /Linjesum45,80 kr/);
  assert.match(firstLineDetails.textContent, /EAN7038010001000/);
  assert.match(firstLineDetails.textContent, /Tekst på kvitteringenTINE HELMELK 1L/);
  const mappingPanels = basketCards[0].querySelectorAll(".purchase-line-mapping");
  assert.equal(mappingPanels.length, 2);
  assert.match(mappingPanels[0].textContent, /GrunnproduktHelmelk/);
  assert.match(mappingPanels[0].textContent, /Pakningsstørrelse1\s000 ml/);
  assert.match(importedLines[0].textContent, /Kjøpt2 l/);
  assert.match(mappingPanels[0].textContent, /Lav sikkerhet/);
  assert.match(mappingPanels[0].textContent, /Identisk varetekst gjenbrukt etter bekreftelse/);
  assert.match(mappingPanels[0].textContent, /KategoriMeieri/);
  assert.match(mappingPanels[0].textContent, /Må kontrolleres/);
  assert.match(mappingPanels[1].textContent, /GrunnproduktIkke koblet/);
  assert.match(mappingPanels[1].textContent, /PakningsstørrelseIkke oppgitt/);

  // Switch to Mode 2: Produkter og kategorier
  await modeTabs[1].dispatchEvent({ type: "click" });
  const catGroups = container.querySelectorAll(".purchase-category-group");
  assert.equal(catGroups.length, 2);
  assert.match(catGroups[0].textContent, /Meieri/);
  assert.match(catGroups[0].textContent, /Sist kjøpt:/);
  assert.match(catGroups[0].textContent, /ca\. hver 14\. dag/);
  assert.match(catGroups[0].textContent, /ca\. 2,14 per 30 dager/);

  // Switch to Mode 3: Tabell med filtre
  await modeTabs[2].dispatchEvent({ type: "click" });
  const tableRows = container.querySelectorAll("tbody tr");
  assert.equal(tableRows.length, 3);
  assert.match(container.querySelector(".purchase-table-footer").textContent, /Viser 3 varelinjer/);

  // Sort by Vare ascending
  const sortButtons = container.querySelectorAll(".purchase-sort-btn");
  const productSortBtn = sortButtons.find((b) => b.dataset.sortKey === "product_name");
  await productSortBtn.dispatchEvent({ type: "click" });
  const firstRowAfterSort = container.querySelectorAll("tbody tr")[0];
  assert.match(firstRowAfterSort.textContent, /TINE HELMELK/);

  // Filter by provider = "oda"
  const provSelect = container.querySelector(".purchase-filter-provider");
  provSelect.value = "oda";
  await provSelect.dispatchEvent({ type: "change" });
  assert.equal(container.querySelectorAll("tbody tr").length, 1);

  // Filter to empty & reset
  const catSelect = container.querySelector(".purchase-filter-category");
  catSelect.value = "Produce";
  await catSelect.dispatchEvent({ type: "change" });
  assert.match(container.querySelector(".purchase-empty-state").textContent, /Ingen kjøp matcher filtrene/);

  const resetBtn = container.querySelector(".purchase-filter-reset");
  await resetBtn.dispatchEvent({ type: "click" });
  assert.equal(container.querySelectorAll("tbody tr").length, 3);
});

test("renderPastPurchasesView shows Bokmål empty and error states", async () => {
  globalThis.document = createMockDocument();
  const emptyContainer = document.createElement("div");
  const emptyView = pastPurchasesView.renderPastPurchasesView(emptyContainer, {
    api: async () => ({ baskets: [], products: [], rows: [], providers: [], stores: [], categories: [] }),
  });
  await emptyView.ready;
  assert.match(emptyContainer.querySelector(".purchase-empty-state").textContent, /Ingen kjøpshistorikk funnet ennå/);

  const errContainer = document.createElement("div");
  const errView = pastPurchasesView.renderPastPurchasesView(errContainer, {
    api: async () => {
      throw new Error("Nettverksfeil");
    },
  });
  await errView.ready;
  const alertBox = errContainer.querySelector('[role="alert"]');
  assert.ok(alertBox);
  assert.match(alertBox.textContent, /Kunne ikke laste kjøpshistorikk: Nettverksfeil/);
});

test("product mapping review requires confirmation and saves a remap for similar purchases", async () => {
  globalThis.document = createMockDocument();
  const container = document.createElement("div");
  const payload = sampleBrowsePayload();
  const calls = [];
  const notices = [];
  const view = pastPurchasesView.renderPastPurchasesView(container, {
    notify: (message) => notices.push(message),
    api: async (endpoint, method = "GET", body) => {
      calls.push({ endpoint, method, body });
      if (endpoint === "/purchase-history") return payload;
      if (endpoint.startsWith("/catalogue/identities/products?")) {
        return [{ id: "pid-carrot", canonical_name: "Gulrot", variant: null }];
      }
      if (endpoint === "/purchase-history/product-mappings") {
        return { mapping: {}, affected_rows: 3, similar_rows: 2 };
      }
      throw new Error(`Unexpected API call ${endpoint}`);
    },
  });
  await view.ready;

  const unmappedPanel = container.querySelectorAll(".purchase-line-mapping")[1];
  const searchInput = unmappedPanel.querySelector(".purchase-mapping-product-search");
  searchInput.value = "gulrot";
  await unmappedPanel.querySelector(".purchase-mapping-search").dispatchEvent({ type: "click" });
  const candidate = unmappedPanel.querySelector(".purchase-mapping-candidate");
  assert.ok(candidate);
  await candidate.dispatchEvent({ type: "click" });

  const confirmation = unmappedPanel.querySelector(".purchase-mapping-confirm-label input");
  const similar = unmappedPanel.querySelector(".purchase-mapping-similar-label input");
  unmappedPanel.querySelector(".purchase-mapping-package-size").value = "2";
  unmappedPanel.querySelector(".purchase-mapping-package-unit").value = "kg";
  similar.checked = false;
  const saveButton = unmappedPanel.querySelector(".purchase-mapping-actions button");
  await saveButton.dispatchEvent({ type: "click" });
  assert.equal(calls.some((call) => call.endpoint === "/purchase-history/product-mappings"), false);
  assert.match(unmappedPanel.querySelector('[role="alert"]').textContent, /Bekreft/);

  confirmation.checked = true;
  await saveButton.dispatchEvent({ type: "click" });
  const post = calls.find((call) => call.endpoint === "/purchase-history/product-mappings");
  assert.equal(post.method, "POST");
  assert.deepEqual(post.body, {
    row_id: "trumf:t-1:line:1",
    action: "remap",
    apply_to_similar: false,
    canonical_product_id: "pid-carrot",
    package_size: 2,
    package_unit: "kg",
  });
  assert.ok(notices.some((message) => /2 kjøp med identisk varetekst/.test(message)));
});

test("AI package inference can start without confirming a pending suggestion", async () => {
  globalThis.document = createMockDocument();
  const container = document.createElement("div");
  const calls = [];
  const payload = sampleBrowsePayload();
  const view = pastPurchasesView.renderPastPurchasesView(container, {
    api: async (endpoint, method = "GET", body) => {
      calls.push({ endpoint, method, body });
      if (endpoint === "/purchase-history") return payload;
      if (endpoint === "/purchase-history/product-mappings") return { affected_rows: 1, similar_rows: 0 };
      throw new Error(`Unexpected API call ${endpoint}`);
    },
  });
  await view.ready;

  const panel = container.querySelectorAll(".purchase-line-mapping")[0];
  const confirmation = panel.querySelector(".purchase-mapping-confirm-label input");
  assert.equal(confirmation.checked, false);
  const inferButton = panel.querySelectorAll(".purchase-mapping-actions button").find(
    (button) => button.textContent === "Foreslå pakning med KI",
  );
  assert.ok(inferButton);
  await inferButton.dispatchEvent({ type: "click" });

  const post = calls.find((call) => call.endpoint === "/purchase-history/product-mappings");
  assert.deepEqual(post.body, {
    row_id: "trumf:t-1:line:0",
    action: "infer",
    apply_to_similar: false,
  });
});

test("manual shopping trips show mapping context without mutation controls", async () => {
  globalThis.document = createMockDocument();
  const container = document.createElement("div");
  const payload = sampleBrowsePayload();
  payload.baskets.push({
    id: "trip:manual-1",
    provider: "store",
    provider_label: "Manuell handletur",
    store_name: "Lokal butikk",
    purchased_at: "2026-11-20T12:00:00Z",
    total_amount: "49.00",
    currency: "NOK",
    item_count: 1,
    provenance: "user_confirmed",
    lines: [{ id: "trip:manual-1:line:0", name: "Bleier", raw_name: "Bleier", quantity: "1", unit: "pk" }],
  });
  const view = pastPurchasesView.renderPastPurchasesView(container, {
    api: async () => payload,
  });
  await view.ready;

  const panels = container.querySelectorAll(".purchase-line-mapping");
  const manualPanel = panels[panels.length - 1];
  assert.match(manualPanel.textContent, /Ikke koblet/);
  assert.match(manualPanel.textContent, /registrert manuelt/);
  assert.equal(manualPanel.querySelector(".purchase-mapping-review"), null);
  assert.equal(pastPurchasesView.supportsProductMapping("store"), false);
  assert.equal(pastPurchasesView.supportsProductMapping("coop"), true);
});

test("AI product and category suggestions stay unconfirmed until the user accepts them", async () => {
  globalThis.document = createMockDocument();
  const container = document.createElement("div");
  const payload = sampleBrowsePayload();
  Object.assign(payload.baskets[0].lines[0], {
    row_id: "row-ai-1",
    base_product_id: null,
    base_product_name: "Helmelk",
    suggested_product_name: "Helmelk",
    mapping_source: "ai",
    mapping_confidence: 0.41,
    mapping_review_status: "pending",
    suggested_category: "Meieri",
    category_source: "ai",
    category_confidence: 0.53,
    category_review_status: "pending",
  });
  const calls = [];
  const view = pastPurchasesView.renderPastPurchasesView(container, {
    api: async (endpoint, method = "GET", body) => {
      calls.push({ endpoint, method, body });
      return endpoint === "/purchase-history" ? payload : { affected_rows: 1, similar_rows: 0 };
    },
  });
  await view.ready;

  const panel = container.querySelectorAll(".purchase-line-mapping")[0];
  assert.match(panel.textContent, /GrunnproduktIkke koblet/);
  assert.match(panel.textContent, /Forslag: Helmelk/);
  assert.match(panel.textContent, /KI-forslag · må kontrolleres/);
  assert.match(panel.textContent, /Lav sikkerhet/);
  assert.match(panel.textContent, /KategoriMeieri/);
  assert.equal(panel.querySelector(".purchase-line-base-name"), null);

  const confirmButton = panel.querySelectorAll(".purchase-mapping-actions button").find((button) => button.textContent === "Bekreft forslag");
  assert.ok(confirmButton);
  const acknowledgement = panel.querySelector(".purchase-mapping-confirm-label input");
  const confirmOptions = panel.querySelectorAll(".purchase-mapping-accept-label input");
  assert.equal(confirmOptions.length, 2);
  await confirmButton.dispatchEvent({ type: "click" });
  assert.equal(calls.some((call) => call.endpoint === "/purchase-history/product-mappings"), false);
  acknowledgement.checked = true;
  await confirmButton.dispatchEvent({ type: "click" });

  const post = calls.find((call) => call.endpoint === "/purchase-history/product-mappings");
  assert.deepEqual(post.body, {
    row_id: "row-ai-1",
    action: "confirm",
    apply_to_similar: true,
    confirm_product: true,
    confirm_category: true,
  });
});

test("mapping provenance and confidence helpers preserve source distinctions", () => {
  assert.equal(pastPurchasesView.formatMappingSource("retailer_identifier"), "Butikkens vare-ID");
  assert.equal(pastPurchasesView.formatMappingSource("description_rule"), "Avledet fra varetekst");
  assert.equal(pastPurchasesView.formatConfidence(0.42), "42 % sikkerhet");
  assert.equal(pastPurchasesView.isLowConfidence(0.42), true);
  assert.equal(pastPurchasesView.isLowConfidence(0.9), false);
  assert.equal(pastPurchasesView.formatMappingMatchLabel("retailer_product_id", "exact"), "Treff på butikkens vare-ID");
  assert.equal(pastPurchasesView.formatMappingMatchLabel("ean", "exact"), "Eksakt EAN-treff");
  assert.equal(pastPurchasesView.formatMappingMatchLabel("exact_description", "exact"), "Eksakt varetekst i samme butikk");
  assert.equal(
    pastPurchasesView.formatMappingMatchLabel("opted_in_exact_description", "similar"),
    "Identisk varetekst gjenbrukt etter bekreftelse",
  );
  assert.equal(pastPurchasesView.formatMappingMatchLabel(null, "exact"), "Eksakt treff");
  assert.equal(pastPurchasesView.formatCategoryDisplayLabel("non_food"), "Ikke-mat");
  assert.equal(pastPurchasesView.formatCategoryDisplayLabel("Household"), "Husholdning");
  assert.equal(pastPurchasesView.formatCategoryDisplayLabel("Bakery"), "Bakeri");
});

test("index.html and app.js wire Tidligere kjøp navigation view", () => {
  const staticDir = path.join(__dirname, "../src/home_kitchen/static");
  const indexHtml = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
  const appJs = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
  assert.match(indexHtml, /<script defer src="\/static\/past-purchases-view\.js"><\/script>/);
  assert.match(indexHtml, /<button data-view="purchases">Tidligere kjøp<\/button\s*>/);
  assert.match(appJs, /purchases:\s*renderPurchases/);
  assert.match(appJs, /function renderPurchases\(main\)/);
});
