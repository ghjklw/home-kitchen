"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const overviewView = require("../src/home_kitchen/static/product-overview-view.js");

class MockNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this._textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.disabled = false;
    this.value = "";
    this.hidden = false;
    this.type = "text";
    this.required = false;
  }

  get textContent() {
    return (this._textContent || "") + this.children.map((c) => c.textContent || "").join("");
  }

  set textContent(val) {
    this._textContent = String(val ?? "");
  }

  append(...children) {
    for (const child of children) {
      if (child) {
        this.children.push(child);
      }
    }
  }

  replaceChildren(...children) {
    this._textContent = "";
    this.children = children.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  async click() {
    if (!this.disabled && this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {} });
    }
  }

  async input(val) {
    this.value = val;
    if (this.listeners.input) {
      await this.listeners.input({ target: this });
    }
  }

  async submit() {
    if (this.listeners.submit) {
      await this.listeners.submit({ preventDefault: () => {} });
    }
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const results = [];

    function matchesSelector(node, sel) {
      if (!node || !node.tagName) return false;
      sel = sel.trim();
      let tag = null;
      let rest = sel;
      const tagMatch = sel.match(/^[a-zA-Z0-9]+/);
      if (tagMatch) {
        tag = tagMatch[0].toUpperCase();
        rest = sel.slice(tag.length);
      }
      if (tag && node.tagName !== tag) return false;

      while (rest.length > 0) {
        if (rest.startsWith(".")) {
          const m = rest.match(/^\.([a-zA-Z0-9_-]+)/);
          if (!m) return false;
          const cls = m[1];
          if (!node.className || !node.className.split(/\s+/).includes(cls)) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith("[")) {
          const m = rest.match(/^\[([a-zA-Z0-9_-]+)(?:([*^$]?=)(["']?)(.*?)\3)?\]/);
          if (!m) return false;
          const attr = m[1];
          const op = m[2];
          const val = m[4];
          const actual = node.getAttribute?.(attr);
          if (actual === null || actual === undefined) return false;
          if (op === "=" && actual !== val) return false;
          if (op === "*=" && !actual.includes(val)) return false;
          rest = rest.slice(m[0].length);
        } else {
          break;
        }
      }
      return true;
    }

    function search(node) {
      if (matchesSelector(node, selector)) {
        results.push(node);
      }
      if (Array.isArray(node?.children)) {
        for (const child of node.children) {
          search(child);
        }
      }
    }

    for (const child of this.children) {
      search(child);
    }
    return results;
  }
}

globalThis.document = {
  createElement: (tag) => new MockNode(tag),
};

test("formatStockSummary enforces kitchen_inventory provenance and distinguishes 0 stock", () => {
  const zeroStock = overviewView.formatStockSummary({
    in_stock: false,
    total_quantity: 0,
    unit: null,
    provenance: "kitchen_inventory",
  });
  assert.equal(zeroStock.inStock, false);
  assert.equal(zeroStock.label, "Ikke på lager (0)");
  assert.match(zeroStock.provenanceText, /Kjøkkenlager/);
  assert.match(zeroStock.provenanceText, /ikke estimert fra kjøp/);

  const activeStock = overviewView.formatStockSummary({
    in_stock: true,
    total_quantity: "3",
    unit: "boks",
    provenance: "kitchen_inventory",
  });
  assert.equal(activeStock.inStock, true);
  assert.equal(activeStock.label, "På lager: 3 boks");
});

test("formatFrequencySummary formats known and unknown purchase intervals without invented numbers", () => {
  const unknown = overviewView.formatFrequencySummary({
    status: "unknown",
    frequency_display: "unknown",
    sample_size: 0,
  });
  assert.equal(unknown.known, false);
  assert.match(unknown.label, /Ukjent frekvens/);

  const known = overviewView.formatFrequencySummary({
    status: "known",
    frequency_display: "ca. hver 7. dag",
    average_interval_days: 7.0,
    sample_size: 5,
    date_range_days: 35,
  });
  assert.equal(known.known, true);
  assert.equal(known.label, "ca. hver 7. dag");
  assert.match(known.details, /5 kjøp over 35 dager/);
});

test("formatProvenanceCounts formats provider-verified, user-confirmed and uncertain records", () => {
  const counts = overviewView.formatProvenanceCounts({
    provider_verified: 12,
    user_confirmed: 3,
    uncertain: 1,
  });
  assert.equal(counts.provider_verified, 12);
  assert.equal(counts.user_confirmed, 3);
  assert.equal(counts.uncertain, 1);
  assert.match(counts.summaryText, /Verifisert butikk\/kvittering: 12/);
  assert.match(counts.summaryText, /Brukerbekreftet: 3/);
  assert.match(counts.summaryText, /Usikker: 1/);
});

test("pure preference manipulation helpers (reorder, add, remove, build payload)", () => {
  const skus = [
    { oda_product_id: "sku-1", rank: 1, name: "Melk 1" },
    { oda_product_id: "sku-2", rank: 2, name: "Melk 2" },
    { oda_product_id: "sku-3", rank: 3, name: "Melk 3" },
  ];

  // Reorder index 2 (sku-3) to index 0
  const reordered = overviewView.reorderPreferredSkus(skus, 2, 0);
  assert.equal(reordered[0].oda_product_id, "sku-3");
  assert.equal(reordered[0].rank, 1);
  assert.equal(reordered[1].oda_product_id, "sku-1");
  assert.equal(reordered[1].rank, 2);
  assert.equal(reordered[2].oda_product_id, "sku-2");
  assert.equal(reordered[2].rank, 3);

  // Add preferred sku
  const added = overviewView.addPreferredSku(reordered, { oda_product_id: "sku-4", name: "Melk 4" });
  assert.equal(added.length, 4);
  assert.equal(added[3].oda_product_id, "sku-4");
  assert.equal(added[3].rank, 4);

  // Add duplicate sku is ignored
  const noDup = overviewView.addPreferredSku(added, "sku-4");
  assert.equal(noDup.length, 4);

  // Remove preferred sku
  const removed = overviewView.removePreferredSku(added, "sku-1");
  assert.equal(removed.length, 3);
  assert.deepEqual(
    removed.map((s) => s.rank),
    [1, 2, 3],
  );

  // Exclusions: add and remove
  const excl1 = overviewView.addExcludedSku([], { oda_product_id: "bad-sku-1", reason: "Liker ikke" });
  assert.equal(excl1.length, 1);
  assert.equal(excl1[0].oda_product_id, "bad-sku-1");
  assert.equal(excl1[0].reason, "Liker ikke");

  const excl2 = overviewView.removeExcludedSku(excl1, "bad-sku-1");
  assert.equal(excl2.length, 0);

  // Payload format
  const payload = overviewView.buildPreferencesPayload(removed, excl1);
  assert.ok(Array.isArray(payload.preferred_oda_skus));
  assert.ok(Array.isArray(payload.excluded_oda_skus));
  assert.equal(payload.preferred_oda_skus[0].rank, 1);
});

test("renderProductCard separates stock strictly from purchases and displays evidence counts", () => {
  const productData = {
    id: "prod-carrot",
    canonical_name: "carrot",
    variant: "",
    display_name: "gulrot",
    locale: "nb",
    translations_by_locale: {
      nb: ["gulrot", "gulrøtter"],
      en: ["carrot", "carrots"],
    },
    stock: {
      in_stock: false,
      total_quantity: "0",
      unit: "kg",
      items: [],
      provenance: "kitchen_inventory",
    },
    purchase_frequency: {
      status: "known",
      average_interval_days: 6.5,
      sample_size: 10,
      date_range_days: 65,
      frequency_display: "ca. hver 7. dag",
      counts_by_provenance: {
        provider_verified: 8,
        user_confirmed: 2,
        uncertain: 0,
      },
    },
    preferred_oda_skus: [
      { oda_product_id: "oda-1", rank: 1, name: "Norske gulrøtter", brand: "Bama", price: "22.90" },
      { oda_product_id: "oda-2", rank: 2, name: "Økologiske gulrøtter", brand: "Kolonihagen", price: "29.90" },
    ],
    excluded_oda_skus: [
      { oda_product_id: "oda-bad", name: "Importerte gulrøtter", reason: "Dårlig smak" },
    ],
  };

  const card = overviewView.renderProductCard(productData, {
    api: async () => {},
  });

  // Display name and canonical name
  assert.equal(card.querySelector(".product-display-name").textContent, "gulrot");
  assert.match(card.querySelector(".product-canonical-subtitle").textContent, /Kanonisk: carrot/);

  // Translations rendered
  const localeRows = card.querySelectorAll(".locale-translation-row");
  assert.equal(localeRows.length, 2);
  assert.match(localeRows[0].textContent, /NB/);
  assert.match(localeRows[0].textContent, /gulrot, gulrøtter/);

  // CRITICAL: Stock is 0 and explicitly marked as kitchen inventory
  const stockBadge = card.querySelector(".stock-badge");
  assert.equal(stockBadge.textContent, "Ikke på lager (0)");
  const stockProv = card.querySelector(".provenance-note");
  assert.match(stockProv.textContent, /Kjøkkenlager/);
  assert.match(stockProv.textContent, /ikke estimert fra kjøp/);

  // Purchases are displayed separately under frequency
  const freqBadge = card.querySelector(".frequency-badge");
  assert.equal(freqBadge.textContent, "ca. hver 7. dag");
  const provCounts = card.querySelector(".provenance-counts");
  assert.match(provCounts.textContent, /Verifisert butikk\/kvittering: 8/);
  assert.match(provCounts.textContent, /Brukerbekreftet: 2/);

  // Preferred Oda SKUs rendered in order
  const prefRows = card.querySelectorAll(".preferred-sku-row");
  assert.equal(prefRows.length, 2);
  assert.match(prefRows[0].textContent, /#1/);
  assert.match(prefRows[0].textContent, /Norske gulrøtter/);
  assert.match(prefRows[1].textContent, /#2/);
  assert.match(prefRows[1].textContent, /Økologiske gulrøtter/);

  // Excluded SKUs rendered
  const exclRows = card.querySelectorAll(".excluded-sku-row");
  assert.equal(exclRows.length, 1);
  assert.match(exclRows[0].textContent, /Importerte gulrøtter/);
  assert.match(exclRows[0].textContent, /Dårlig smak/);
});

test("reordering preferred Oda SKUs calls PUT preferences and updates card", async () => {
  let putUrl = null;
  let putPayload = null;

  const initialProduct = {
    id: "prod-tomato",
    canonical_name: "tomato",
    variant: "tinned",
    display_name: "hermetisk tomat",
    stock: { in_stock: true, total_quantity: "4", unit: "boks", provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [
      { oda_product_id: "tom-1", rank: 1, name: "Mutti Polpa" },
      { oda_product_id: "tom-2", rank: 2, name: "Eldorado Hakkede" },
    ],
    excluded_oda_skus: [],
  };

  const updatedProduct = {
    ...initialProduct,
    preferred_oda_skus: [
      { oda_product_id: "tom-2", rank: 1, name: "Eldorado Hakkede" },
      { oda_product_id: "tom-1", rank: 2, name: "Mutti Polpa" },
    ],
  };

  const mockApi = async (url, method, body) => {
    if (method === "PUT") {
      putUrl = url;
      putPayload = body;
      return updatedProduct;
    }
  };

  const card = overviewView.renderProductCard(initialProduct, {
    api: mockApi,
  });

  // Click "↓" on rank #1
  const downBtn = card.querySelectorAll(".preferred-sku-row")[0].querySelector(".move-down-btn");
  await downBtn.click();

  assert.equal(putUrl, "/catalogue/products/prod-tomato/preferences");
  assert.ok(putPayload);
  assert.equal(putPayload.preferred_oda_skus[0].oda_product_id, "tom-2");
  assert.equal(putPayload.preferred_oda_skus[0].rank, 1);
  assert.equal(putPayload.preferred_oda_skus[1].oda_product_id, "tom-1");
  assert.equal(putPayload.preferred_oda_skus[1].rank, 2);

  // Card is re-rendered with updated preference order
  const updatedRows = card.querySelectorAll(".preferred-sku-row");
  assert.match(updatedRows[0].textContent, /Eldorado Hakkede/);
  assert.match(updatedRows[1].textContent, /Mutti Polpa/);
});

test("adding and removing exclusions calls PUT preferences", async () => {
  let putPayload = null;

  const product = {
    id: "prod-barley",
    canonical_name: "barley",
    display_name: "byggryn",
    stock: { in_stock: false, total_quantity: "0", provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
  };

  const mockApi = async (url, method, body) => {
    putPayload = body;
    return {
      ...product,
      excluded_oda_skus: [{ oda_product_id: "barley-bad", reason: "Uønsket" }],
    };
  };

  const card = overviewView.renderProductCard(product, {
    api: mockApi,
  });

  const addForm = card.querySelector(".add-excluded-sku-form");
  addForm.querySelector(".excl-sku-input").value = "barley-bad";
  addForm.querySelector(".excl-reason-input").value = "Uønsket";
  await addForm.submit();

  assert.ok(putPayload);
  assert.equal(putPayload.excluded_oda_skus.length, 1);
  assert.equal(putPayload.excluded_oda_skus[0].oda_product_id, "barley-bad");
  assert.equal(putPayload.excluded_oda_skus[0].reason, "Uønsket");

  // Verify remove exclusion control
  const removeBtn = card.querySelector(".remove-excl-btn");
  assert.ok(removeBtn);
  mockApi.nextReturn = { ...product, excluded_oda_skus: [] };
  await removeBtn.click();
  assert.equal(putPayload.excluded_oda_skus.length, 0);
});

test("renderProductOverview queries /catalogue/overview with search filter and renders cards", async () => {
  const container = new MockNode("div");
  let queriedUrl = null;

  const mockApi = async (url) => {
    queriedUrl = url;
    return [
      {
        id: "prod-1",
        canonical_name: "potato",
        display_name: "potet",
        stock: { in_stock: true, total_quantity: "5", unit: "kg", provenance: "kitchen_inventory" },
        purchase_frequency: { status: "unknown", frequency_display: "unknown" },
        preferred_oda_skus: [],
        excluded_oda_skus: [],
      },
    ];
  };

  const view = overviewView.renderProductOverview(container, {
    api: mockApi,
    locale: "nb",
  });
  await view.ready;

  assert.ok(queriedUrl.includes("/catalogue/overview?locale=nb"));

  const cards = container.querySelectorAll(".product-overview-card");
  assert.equal(cards.length, 1);
  assert.equal(cards[0].querySelector(".product-display-name").textContent, "potet");

  // Search input triggers reload
  const searchInput = container.querySelector(".catalogue-search-input");
  assert.ok(searchInput);
  assert.equal(searchInput.getAttribute("aria-label"), "Søk etter råvare");
});

test("renderCuratedFoodsSection browses Matvaretabellen and loads linked products without faking activity", async () => {
  const container = new MockNode("div");
  const calls = [];

  const mockApi = async (path, method = "GET") => {
    calls.push(`${method} ${path}`);
    if (path.startsWith("/catalogue/foods?")) {
      return [
        {
          id: "11111111-1111-1111-1111-111111111111",
          source_item_id: "01.001",
          source_name: "Melk, hel, 3,5 % fett",
          display_name: "Helmelk",
          category: "Dairy",
          default_unit: "l",
        },
      ];
    }
    if (path === "/catalogue/foods/11111111-1111-1111-1111-111111111111/products") {
      return [
        {
          id: "22222222-2222-2222-2222-222222222222",
          provider: "oda",
          product_id: "oda-1",
          preferred: false,
          snapshot: { name: "Tine Helmelk 1L" },
        },
      ];
    }
    if (path === "/catalogue/products/22222222-2222-2222-2222-222222222222/preferred") {
      return {};
    }
    throw new Error(`Unexpected API call: ${method} ${path}`);
  };

  const section = overviewView.renderCuratedFoodsSection(container, {
    api: mockApi,
    notify: () => {},
  });
  await section.loadFoods("melk");

  assert.ok(calls.includes("GET /catalogue/foods?q=melk&limit=37"));
  assert.match(container.textContent, /Helmelk/);
  assert.match(container.textContent, /Kilde: Melk, hel, 3,5 % fett/);
  assert.match(container.textContent, /01\.001/);

  const optsBtn = container.querySelector(".curated-food-card").querySelector("button");
  assert.ok(optsBtn);
  await optsBtn.click();
  await new Promise((r) => setTimeout(r, 10));
  assert.ok(
    calls.includes("GET /catalogue/foods/11111111-1111-1111-1111-111111111111/products"),
  );
  assert.match(container.textContent, /Tine Helmelk 1L/);
});
