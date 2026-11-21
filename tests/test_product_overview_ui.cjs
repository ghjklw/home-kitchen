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
    this.checked = false;
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
    if (this.disabled) return;
    if (this.type === "checkbox") {
      this.checked = !this.checked;
      if (this.listeners.change) {
        await this.listeners.change({ target: this });
      }
    }
    if (this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {} });
    }
  }

  async input(val) {
    this.value = val;
    if (this.listeners.input) {
      await this.listeners.input({ target: this });
    }
  }

  async change(val) {
    this.value = val;
    if (this.listeners.change) {
      await this.listeners.change({ target: this });
    }
  }

  focus() {}

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
  assert.equal(unknown.details, "ukjent");

  const everyFive = overviewView.formatFrequencySummary({
    status: "known",
    frequency_display: "every 5 days",
    average_interval_days: 5,
    sample_size: 4,
    date_range_days: 15,
  });
  assert.equal(everyFive.label, "hver 5 dager");

  const everyOne = overviewView.formatFrequencySummary({
    status: "known",
    frequency_display: "every 1 day",
    average_interval_days: 1,
    sample_size: 3,
    date_range_days: 2,
  });
  assert.equal(everyOne.label, "hver dag");

  const everyDecimal = overviewView.formatFrequencySummary({
    status: "known",
    frequency_display: "every 3.5 days",
    average_interval_days: 3.5,
    sample_size: 3,
    date_range_days: 7,
  });
  assert.equal(everyDecimal.label, "hver 3,5 dager");

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

test("renderProductCard shows alias certainty and confirms unreviewed aliases via PUT /review", async () => {
  const calls = [];
  const product = {
    id: "prod-alias-1",
    canonical_name: "carrot",
    display_name: "gulrot",
    aliases: [
      { alias: "gulrot", locale: "nb", context: "recipe", reviewed: true },
      { alias: "carotte", locale: "fr", context: "recipe", reviewed: false },
    ],
    translations_by_locale: { nb: ["gulrot"], fr: ["carotte"] },
    stock: { in_stock: false, total_quantity: "0", unit: "", provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    purchase_evidence_counts: { provider_verified: 0, user_confirmed: 0, uncertain: 0 },
  };

  const mockApi = async (path, method = "GET", body = null) => {
    calls.push({ path, method, body });
    return {};
  };

  const card = overviewView.renderProductCard(product, { api: mockApi, notify: () => {} });
  assert.match(card.textContent, /Bekreftet/);
  assert.match(card.textContent, /Ubekreftet \/ forslag/);

  const confirmBtn = card.querySelector(".confirm-alias-btn");
  assert.ok(confirmBtn);
  await confirmBtn.click();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "PUT");
  assert.equal(calls[0].path, "/catalogue/aliases/product/prod-alias-1/review");
  assert.deepEqual(calls[0].body, { alias: "carotte", locale: "fr", context: "recipe" });
  assert.equal(card.querySelector(".confirm-alias-btn"), null);
});

test("renderProductCard supports inline stock create and update via PUT /stock", async () => {
  const calls = [];
  const notifications = [];

  const initialProduct = {
    id: "prod-stock-1",
    canonical_name: "carrot",
    display_name: "gulrot",
    stock: {
      in_stock: false,
      total_quantity: "0",
      unit: null,
      items: [],
      provenance: "kitchen_inventory",
    },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  const updatedProduct = {
    ...initialProduct,
    stock: {
      in_stock: true,
      total_quantity: "3",
      unit: "kg",
      items: [
        {
          inventory_id: "inv-1",
          name: "gulrot",
          quantity: "3",
          unit: "kg",
          category: "Other",
          note: "Kjøleskap",
          provenance: "kitchen_inventory",
        },
      ],
      provenance: "kitchen_inventory",
    },
  };

  const mockApi = async (path, method, body) => {
    calls.push({ path, method, body });
    return updatedProduct;
  };

  const card = overviewView.renderProductCard(initialProduct, {
    api: mockApi,
    notify: (msg) => notifications.push(msg),
  });

  const stockForm = card.querySelector(".stock-edit-form");
  assert.ok(stockForm);
  stockForm.querySelector(".stock-qty-input").value = "3";
  stockForm.querySelector(".stock-unit-input").value = "kg";
  stockForm.querySelector(".stock-note-input").value = "Kjøleskap";
  await stockForm.submit();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "PUT");
  assert.equal(calls[0].path, "/catalogue/products/prod-stock-1/stock");
  assert.equal(calls[0].body.quantity, "3");
  assert.equal(calls[0].body.unit, "kg");
  assert.equal(calls[0].body.note, "Kjøleskap");

  assert.equal(card.querySelector(".stock-badge").textContent, "På lager: 3 kg");
  assert.match(card.querySelector(".stock-items-list").textContent, /gulrot: 3 kg \(Kjøleskap\)/);
  assert.match(notifications[0], /Lagerbeholdning for «gulrot» er lagret/);
});

test("renderProductCard renders remembered Oda SKUs distinctly from previously purchased SKUs and allows promote/exclude", async () => {
  const calls = [];
  const product = {
    id: "prod-skus-1",
    canonical_name: "milk",
    display_name: "melk",
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "known", frequency_display: "every 5 days", sample_size: 4, date_range_days: 15 },
    preferred_oda_skus: [{ oda_product_id: "oda-pref", rank: 1, name: "Tine Helmelk" }],
    excluded_oda_skus: [{ oda_product_id: "oda-excl", name: "Skummet melk", reason: "Feil fettprosent" }],
    remembered_oda_skus: [
      {
        oda_product_id: "oda-rem-2",
        provider: "oda",
        name: "Rørosmeieriet Helmelk 1L",
        brand: "Rørosmeieriet",
        price: "32.90",
        selection_count: 2,
      },
    ],
    previously_purchased_skus: [
      {
        sku_id: "7032069704018",
        provider: "rema1000",
        name: "REMA 1000 Helmelk 1L",
        store_name: "REMA 1000 Vulkan",
        ean: "7032069704018",
        purchase_count: 3,
        provenance: "provider_verified",
      },
      {
        sku_id: "oda-hist-9",
        provider: "oda",
        name: "Q-Melk Hel 1L",
        store_name: "Oda",
        purchase_count: 1,
        provenance: "provider_verified",
      },
    ],
  };

  const mockApi = async (path, method, body) => {
    calls.push({ path, method, body });
    return {
      ...product,
      preferred_oda_skus: body.preferred_oda_skus,
      excluded_oda_skus: body.excluded_oda_skus,
    };
  };

  const card = overviewView.renderProductCard(product, { api: mockApi });

  const remContainer = card.querySelector(".remembered-skus-container");
  assert.ok(remContainer);
  const remRows = remContainer.querySelectorAll(".remembered-sku-row");
  assert.equal(remRows.length, 1);
  assert.match(remRows[0].textContent, /Rørosmeieriet Helmelk 1L/);
  assert.match(remRows[0].textContent, /Valgt 2 ganger/);
  assert.match(remRows[0].textContent, /Husket valg/);

  const purContainer = card.querySelector(".purchased-skus-container");
  assert.ok(purContainer);
  const purRows = purContainer.querySelectorAll(".purchased-sku-row");
  assert.equal(purRows.length, 2);
  assert.match(purRows[0].textContent, /REMA 1000 Vulkan/);
  assert.match(purRows[0].textContent, /REMA 1000 Helmelk 1L/);
  assert.match(purRows[0].textContent, /7032069704018/);
  assert.match(purRows[0].textContent, /Kjøpt 3 ganger/);
  assert.match(purRows[0].textContent, /Verifisert kvittering/);
  assert.match(purRows[1].textContent, /Q-Melk Hel 1L/);

  // Promote remembered Oda SKU to preferred
  const promoteBtn = remContainer.querySelector(".promote-remembered-btn");
  assert.ok(promoteBtn);
  await promoteBtn.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "PUT");
  assert.equal(calls[0].path, "/catalogue/products/prod-skus-1/preferences");
  assert.deepEqual(
    calls[0].body.preferred_oda_skus.map((s) => s.oda_product_id),
    ["oda-pref", "oda-rem-2"],
  );
});

test("renderProductOverview renders merge controls, fetches merge preview, and confirms before POST /merge", async () => {
  const container = new MockNode("div");
  const calls = [];
  const notifications = [];
  let mergedDone = false;

  const targetProd = {
    id: "prod-target",
    canonical_name: "carrot",
    variant: "",
    display_name: "gulrot",
    aliases: [{ alias: "gulrot", locale: "nb", context: "recipe", reviewed: true }],
    stock: { in_stock: true, total_quantity: "2", unit: "kg", items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };
  const dupProd = {
    id: "prod-dup",
    canonical_name: "gulrøtter",
    variant: "",
    display_name: "gulrøtter",
    aliases: [{ alias: "gulrøtter", locale: "nb", context: "recipe", reviewed: true }],
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  const mockApi = async (path, method = "GET", body = null) => {
    calls.push({ path, method, body });
    if (method === "GET" && path.startsWith("/catalogue/overview")) {
      return mergedDone ? [targetProd] : [targetProd, dupProd];
    }
    if (method === "GET" && path === "/catalogue/products/prod-target/merge-preview?source_id=prod-dup") {
      return {
        target_product_id: "prod-target",
        source_product_id: "prod-dup",
        target_canonical_name: "carrot",
        source_canonical_name: "gulrøtter",
        target_variant: "",
        source_variant: "",
        target_display_name: "gulrot",
        source_display_name: "gulrøtter",
        variant_mismatch: false,
        aliases_to_move: 2,
        stock_rows_to_move: 1,
        recipe_links_to_move: 3,
        preferences_to_move: 1,
        exclusions_to_move: 0,
        purchase_history_rows_to_move: 4,
      };
    }
    if (method === "POST" && path === "/catalogue/products/prod-target/merge") {
      mergedDone = true;
      return targetProd;
    }
    throw new Error(`Unexpected call: ${method} ${path}`);
  };

  const view = overviewView.renderProductOverview(container, {
    api: mockApi,
    notify: (msg) => notifications.push(msg),
    locale: "nb",
  });
  await view.ready;

  const cards = container.querySelectorAll(".product-overview-card");
  assert.equal(cards.length, 2);

  const dupCard = cards[1];
  const mergeSelect = dupCard.querySelector(".merge-target-select");
  assert.ok(mergeSelect);
  mergeSelect.value = "prod-target";

  const mergeForm = dupCard.querySelector(".merge-product-form");
  await mergeForm.submit();
  await new Promise((r) => setTimeout(r, 10));

  // Preview fetched first; POST merge NOT executed yet
  assert.ok(
    calls.some(
      (c) =>
        c.method === "GET" &&
        c.path === "/catalogue/products/prod-target/merge-preview?source_id=prod-dup",
    ),
  );
  assert.equal(calls.some((c) => c.method === "POST"), false);

  const previewBox = dupCard.querySelector(".merge-preview-box");
  assert.ok(previewBox);
  assert.match(previewBox.textContent, /Kallenavn som flyttes: 2/);
  assert.match(previewBox.textContent, /Lagerlinjer som flyttes: 1/);
  assert.match(previewBox.textContent, /Oppskriftskoblinger som flyttes: 3/);
  assert.match(previewBox.textContent, /Oda-preferanser som flyttes: 1/);
  assert.match(previewBox.textContent, /Oda-ekskluderinger som flyttes: 0/);
  assert.match(previewBox.textContent, /Kjøpshistorikkrader som flyttes: 4/);
  assert.equal(dupCard.querySelector(".merge-variant-warning"), null);

  const confirmBtn = dupCard.querySelector(".confirm-merge-btn");
  assert.ok(confirmBtn);
  assert.equal(confirmBtn.disabled, false);
  await confirmBtn.click();
  await new Promise((r) => setTimeout(r, 10));

  const mergeCall = calls.find((c) => c.method === "POST");
  assert.ok(mergeCall);
  assert.equal(mergeCall.path, "/catalogue/products/prod-target/merge");
  assert.deepEqual(mergeCall.body, { source_product_id: "prod-dup" });
  assert.match(notifications[0], /slått sammen inn i «gulrot»/);
  assert.equal(container.querySelectorAll(".product-overview-card").length, 1);
});

test("renderProductOverview shows Bokmål variant mismatch warning and blocks merge until explicitly confirmed", async () => {
  const container = new MockNode("div");
  const calls = [];
  let mergedDone = false;

  const targetProd = {
    id: "prod-fresh",
    canonical_name: "tomato",
    variant: "fersk",
    display_name: "tomat (fersk)",
    aliases: [{ alias: "tomat", locale: "nb", context: "recipe", reviewed: true }],
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };
  const dupProd = {
    id: "prod-tinned",
    canonical_name: "tomato",
    variant: "hermetisk",
    display_name: "tomat (hermetisk)",
    aliases: [{ alias: "hermetisk tomat", locale: "nb", context: "recipe", reviewed: true }],
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  const mockApi = async (path, method = "GET", body = null) => {
    calls.push({ path, method, body });
    if (method === "GET" && path.startsWith("/catalogue/overview")) {
      return mergedDone ? [targetProd] : [targetProd, dupProd];
    }
    if (method === "GET" && path === "/catalogue/products/prod-fresh/merge-preview?source_id=prod-tinned") {
      return {
        target_product_id: "prod-fresh",
        source_product_id: "prod-tinned",
        target_canonical_name: "tomato",
        source_canonical_name: "tomato",
        target_variant: "fersk",
        source_variant: "hermetisk",
        target_display_name: "tomat (fersk)",
        source_display_name: "tomat (hermetisk)",
        variant_mismatch: true,
        aliases_to_move: 1,
        stock_rows_to_move: 0,
        recipe_links_to_move: 1,
        preferences_to_move: 0,
        exclusions_to_move: 0,
        purchase_history_rows_to_move: 0,
      };
    }
    if (method === "POST" && path === "/catalogue/products/prod-fresh/merge") {
      mergedDone = true;
      return targetProd;
    }
    throw new Error(`Unexpected call: ${method} ${path}`);
  };

  const view = overviewView.renderProductOverview(container, {
    api: mockApi,
    notify: () => {},
    locale: "nb",
  });
  await view.ready;

  const tinnedCard = container.querySelectorAll(".product-overview-card")[1];
  tinnedCard.querySelector(".merge-target-select").value = "prod-fresh";
  await tinnedCard.querySelector(".merge-product-form").submit();
  await new Promise((r) => setTimeout(r, 10));

  const warning = tinnedCard.querySelector(".merge-variant-warning");
  assert.ok(warning);
  assert.match(warning.textContent, /Advarsel: Variantene er ulike/);
  assert.match(warning.textContent, /hermetisk/);
  assert.match(warning.textContent, /fersk/);

  const confirmBtn = tinnedCard.querySelector(".confirm-merge-btn");
  assert.equal(confirmBtn.disabled, true);
  await confirmBtn.click();
  assert.equal(calls.some((c) => c.method === "POST"), false);

  // Cancel dismisses preview without merging
  await tinnedCard.querySelector(".cancel-merge-btn").click();
  assert.equal(tinnedCard.querySelector(".merge-preview-box"), null);

  // Re-open preview, explicitly confirm variant mismatch, then merge
  await tinnedCard.querySelector(".merge-product-form").submit();
  await new Promise((r) => setTimeout(r, 10));
  const checkbox = tinnedCard.querySelector(".merge-variant-confirm-checkbox");
  assert.ok(checkbox);
  await checkbox.click();
  assert.equal(checkbox.checked, true);
  assert.equal(tinnedCard.querySelector(".confirm-merge-btn").disabled, false);

  await tinnedCard.querySelector(".confirm-merge-btn").click();
  await new Promise((r) => setTimeout(r, 10));

  const mergeCall = calls.find((c) => c.method === "POST");
  assert.ok(mergeCall);
  assert.equal(mergeCall.path, "/catalogue/products/prod-fresh/merge");
  assert.deepEqual(mergeCall.body, { source_product_id: "prod-tinned" });
});

test("renderProductCard translates unknown and every X days badges to Bokmål", () => {
  const unknownCard = overviewView.renderProductCard({
    id: "prod-unknown-1",
    canonical_name: "salt",
    display_name: "salt",
    aliases: [{ alias: "salt", locale: "unknown", reviewed: false }],
    stock: {
      in_stock: true,
      total_quantity: "2",
      unit: "unknown",
      items: [{ name: "salt", quantity: "2", unit: "unknown" }],
    },
    purchase_frequency: { status: "unknown", frequency_display: "unknown", sample_size: 0 },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
  });
  assert.equal(unknownCard.querySelector(".locale-badge").textContent, "UKJENT");
  assert.equal(unknownCard.querySelector(".stock-badge").textContent, "På lager: 2 ukjent");
  assert.equal(unknownCard.querySelector(".frequency-details").textContent, "ukjent");

  const knownCard = overviewView.renderProductCard({
    id: "prod-known-1",
    canonical_name: "milk",
    display_name: "melk",
    aliases: [{ alias: "melk", locale: "nb", reviewed: true }],
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [] },
    purchase_frequency: {
      status: "known",
      frequency_display: "every 2.5 days",
      sample_size: 3,
      date_range_days: 5,
    },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
  });
  assert.equal(knownCard.querySelector(".frequency-badge").textContent, "hver 2,5 dager");
});

test("renderCuratedFoodsSection translates food categories to Bokmål, browses with empty query, and links products via POST", async () => {
  const shoppingView = require("../src/home_kitchen/static/shopping-view.js");
  const prevWindow = globalThis.window;
  globalThis.window = { KitchenShoppingView: shoppingView };

  try {
    const container = new MockNode("div");
    const calls = [];
    const notifications = [];
    let linkedOptions = [];

    const mockApi = async (path, method = "GET", body = null) => {
      calls.push({ path, method, body });
      if (path === "/catalogue/foods?q=&limit=37") {
        return [
          {
            id: "11111111-1111-1111-1111-111111111111",
            source_item_id: "01.001",
            source_name: "Melk, hel",
            display_name: "Helmelk",
            category: "Dairy",
            default_unit: "l",
          },
          {
            id: "33333333-3333-3333-3333-333333333333",
            source_item_id: "06.036",
            source_name: "Gulrot, norsk, rå",
            display_name: "Gulrot",
            category: "Produce",
            default_unit: "kg",
          },
        ];
      }
      if (path === "/catalogue/foods/11111111-1111-1111-1111-111111111111/products" && method === "GET") {
        return linkedOptions;
      }
      if (path === "/catalogue/foods/11111111-1111-1111-1111-111111111111/products" && method === "POST") {
        const created = {
          id: "opt-new-1",
          provider: body.provider,
          product_id: body.product_id,
          preferred: false,
          snapshot: { name: body.name },
        };
        linkedOptions = [created];
        return created;
      }
      throw new Error(`Unexpected API call: ${method} ${path}`);
    };

    const section = overviewView.renderCuratedFoodsSection(container, {
      api: mockApi,
      notify: (msg) => notifications.push(msg),
      products: [
        {
          id: "prod-milk",
          canonical_name: "whole milk",
          display_name: "helmelk",
          preferred_oda_skus: [{ oda_product_id: "oda-milk-1", name: "Tine Helmelk 1L" }],
        },
      ],
    });

    await section.loadFoods("");
    assert.ok(calls.some((c) => c.method === "GET" && c.path === "/catalogue/foods?q=&limit=37"));
    assert.match(container.textContent, /Meieri/);
    assert.match(container.textContent, /Frukt og grønt/);
    assert.doesNotMatch(container.textContent, /\bDairy\b/);
    assert.doesNotMatch(container.textContent, /\bProduce\b/);

    const firstCard = container.querySelectorAll(".curated-food-card")[0];
    const linkForm = firstCard.querySelector(".curated-food-link-form");
    assert.ok(linkForm);
    const linkSelect = linkForm.querySelector(".curated-food-link-select");
    await linkSelect.change("oda:oda-milk-1");
    await linkForm.submit();
    await new Promise((r) => setTimeout(r, 10));

    const postCall = calls.find((c) => c.method === "POST");
    assert.ok(postCall);
    assert.equal(postCall.path, "/catalogue/foods/11111111-1111-1111-1111-111111111111/products");
    assert.equal(postCall.body.provider, "oda");
    assert.equal(postCall.body.product_id, "oda-milk-1");
    assert.equal(postCall.body.name, "Tine Helmelk 1L");
    assert.match(firstCard.textContent, /Tine Helmelk 1L \(Oda\)/);
    assert.match(notifications[0], /Koblet «Tine Helmelk 1L»/);
  } finally {
    if (prevWindow === undefined) delete globalThis.window;
    else globalThis.window = prevWindow;
  }
});

test("renderProductOverview supports inline alias reassign (Endre kobling) via POST /catalogue/aliases/correct", async () => {
  const container = new MockNode("div");
  const calls = [];
  const notifications = [];
  let reassigned = false;

  const prodA = {
    id: "prod-butter",
    canonical_name: "butter",
    display_name: "smør",
    aliases: [
      { alias: "margarin", locale: "nb", context: "", reviewed: true },
    ],
    translations_by_locale: { nb: ["margarin"] },
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  const prodB = {
    id: "prod-margarine",
    canonical_name: "margarine",
    display_name: "margarin",
    aliases: [],
    translations_by_locale: {},
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  const mockApi = async (path, method = "GET", body = null) => {
    calls.push({ path, method, body });
    if (method === "GET" && path.startsWith("/catalogue/overview")) {
      if (!reassigned) return [prodA, prodB];
      return [
        { ...prodA, aliases: [], translations_by_locale: {} },
        {
          ...prodB,
          aliases: [{ alias: "margarin", locale: "nb", context: "", reviewed: true }],
          translations_by_locale: { nb: ["margarin"] },
        },
      ];
    }
    if (method === "POST" && path === "/catalogue/aliases/correct") {
      reassigned = true;
      return { status: "updated" };
    }
    throw new Error(`Unexpected API call: ${method} ${path}`);
  };

  const view = overviewView.renderProductOverview(container, {
    api: mockApi,
    notify: (msg) => notifications.push(msg),
    locale: "nb",
  });
  await view.ready;

  const cards = container.querySelectorAll(".product-overview-card");
  assert.equal(cards.length, 2);

  const butterCard = cards[0];
  const reassignBtn = butterCard.querySelector(".reassign-alias-btn");
  assert.ok(reassignBtn);
  assert.equal(reassignBtn.textContent, "Endre kobling");
  assert.equal(reassignBtn.getAttribute("aria-expanded"), "false");

  const reassignForm = butterCard.querySelector(".alias-reassign-form");
  assert.equal(reassignForm.hidden, true);
  await reassignBtn.click();
  assert.equal(reassignForm.hidden, false);
  assert.equal(reassignBtn.getAttribute("aria-expanded"), "true");

  const reassignSelect = reassignForm.querySelector(".alias-reassign-select");
  reassignSelect.value = "prod-margarine";
  await reassignForm.submit();
  await new Promise((r) => setTimeout(r, 10));

  const correctCall = calls.find((c) => c.method === "POST" && c.path === "/catalogue/aliases/correct");
  assert.ok(correctCall);
  assert.deepEqual(correctCall.body, {
    kind: "product",
    identity_id: "prod-margarine",
    alias: "margarin",
    locale: "nb",
    context: "",
    reviewed: true,
  });
  assert.match(notifications[0], /Kallenavnet «margarin» ble flyttet til «margarin»/);

  // Overview reloaded: alias moved from butter card to margarine card
  const refreshedCards = container.querySelectorAll(".product-overview-card");
  assert.equal(refreshedCards[0].querySelectorAll(".alias-review-row").length, 0);
  assert.equal(refreshedCards[1].querySelectorAll(".alias-review-row").length, 1);
  assert.match(refreshedCards[1].querySelector(".alias-review-row").textContent, /margarin/);
});
