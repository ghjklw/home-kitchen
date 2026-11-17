"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const unmappedView = require("../src/home_kitchen/static/unmapped-ingredients-view.js");

class MockNode {
  constructor(tag, text = "", className = "") {
    this.tagName = tag.toUpperCase();
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.hidden = false;
    this.open = false;
  }

  append(...children) {
    for (const child of children) {
      if (child) this.children.push(child);
    }
  }

  replaceChildren(...children) {
    this.textContent = "";
    this.children = children.filter(Boolean);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "type") this.type = String(value);
    if (name === "value") this.value = String(value);
  }

  getAttribute(name) {
    if (name === "type" && this.type !== undefined) return this.type;
    if (name === "value" && this.value !== undefined) return this.value;
    return this.attributes[name] ?? null;
  }

  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }

  async click() {
    if (!this.disabled && this.listeners.click) {
      await this.listeners.click({ preventDefault: () => {} });
    }
  }

  async change() {
    if (this.listeners.change) {
      await this.listeners.change({ target: this });
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
          if (!node.className || !node.className.split(/\s+/).includes(m[1])) return false;
          rest = rest.slice(m[0].length);
        } else {
          break;
        }
      }
      return true;
    }

    function search(node) {
      if (matchesSelector(node, selector)) results.push(node);
      if (Array.isArray(node?.children)) {
        for (const child of node.children) search(child);
      }
    }

    for (const child of this.children) search(child);
    return results;
  }

  get text() {
    return (
      (this.textContent || "") +
      " " +
      this.children.map((c) => (c.text !== undefined ? c.text : c.textContent || "")).join(" ")
    ).trim();
  }
}

class MockDocument {
  createElement(tag) {
    return new MockNode(tag);
  }
  createTextNode(text) {
    return { textContent: text, text };
  }
}

test("Kartlegg automatisk med KI button calls auto-map endpoint, shows Bokmål summary, and refreshes list", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  let items = [
    {
      recipe_id: "rec-1",
      recipe_title: "Fiskegrateng",
      ingredient_index: 0,
      name: "garlic",
      quantity: 2,
      unit: "cloves",
      canonical_product_id: null,
      canonical_unit_id: null,
    },
  ];

  const apiCalls = [];
  const notifications = [];
  let mappedCalls = 0;

  const api = async (endpoint, method = "GET", body = null) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/catalogue/unmapped-ingredients/auto-map") {
      items = [];
      return {
        processed_unique: 1,
        mapped_occurrences: 1,
        created_products: 1,
        created_aliases: 6,
        remaining_unmapped: 0,
      };
    }
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      return items;
    }
    return {};
  };

  const view = unmappedView.renderReviewDialog(root, {
    document: doc,
    api,
    notify: (msg) => notifications.push(msg),
    onMapped: () => {
      mappedCalls += 1;
    },
  });
  await view.ready;

  const autoMapBtn = root.querySelector(".unmapped-automap-btn");
  assert.ok(autoMapBtn);
  assert.equal(autoMapBtn.textContent, "Kartlegg automatisk med KI");
  assert.equal(autoMapBtn.disabled, false);

  await autoMapBtn.click();

  const autoMapCall = apiCalls.find((c) => c.endpoint === "/catalogue/unmapped-ingredients/auto-map");
  assert.ok(autoMapCall);
  assert.equal(autoMapCall.method, "POST");
  assert.deepEqual(autoMapCall.body, { batch_size: 30, max_batches: 3 });

  // Refreshed list after auto-map
  const getCalls = apiCalls.filter((c) => c.endpoint === "/catalogue/unmapped-ingredients");
  assert.equal(getCalls.length, 2);

  // Shows Bokmål summary and disables button when 0 unmapped remain
  const statusNode = root.querySelector(".unmapped-status");
  assert.ok(statusNode.textContent.includes("KI-kartlegging fullført: 1 forekomster koblet (1 unike)"));
  assert.ok(statusNode.textContent.includes("1 nye råvarer, 6 nye synonymer, 0 gjenstår."));
  assert.equal(notifications.length, 1);
  assert.equal(mappedCalls, 1);
  assert.ok(root.querySelector(".unmapped-empty"));
  assert.equal(autoMapBtn.disabled, true);
});

test("Kartlegg automatisk med KI passes recipe filter and handles errors in Bokmål", async () => {
  const doc = new MockDocument();
  const root = doc.createElement("div");

  const apiCalls = [];
  const api = async (endpoint, method = "GET", body = null) => {
    apiCalls.push({ endpoint, method, body });
    if (endpoint === "/catalogue/unmapped-ingredients/auto-map") {
      throw new Error("Tjenesten svarte ikke");
    }
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) {
      return [
        {
          recipe_id: "rec-42",
          recipe_title: "Suppe",
          ingredient_index: 0,
          name: "løk",
          quantity: 1,
          unit: "stk",
          canonical_product_id: null,
          canonical_unit_id: null,
        },
      ];
    }
    return {};
  };

  const view = unmappedView.renderReviewDialog(root, {
    document: doc,
    api,
    recipeId: "rec-42",
  });
  await view.ready;

  const autoMapBtn = root.querySelector(".unmapped-automap-btn");
  await autoMapBtn.click();

  const autoMapCall = apiCalls.find((c) => c.endpoint === "/catalogue/unmapped-ingredients/auto-map");
  assert.deepEqual(autoMapCall.body, {
    batch_size: 30,
    max_batches: 3,
    recipe_id: "rec-42",
  });

  const errorNode = root.querySelector(".unmapped-error");
  assert.equal(errorNode.hidden, false);
  assert.ok(errorNode.textContent.includes("KI-kartlegging feilet: Tjenesten svarte ikke"));
  assert.equal(autoMapBtn.disabled, false);
  assert.equal(autoMapBtn.textContent, "Kartlegg automatisk med KI");
});
