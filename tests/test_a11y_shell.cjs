"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticDir = path.join(__dirname, "../src/home_kitchen/static");
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const css = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");
const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");

const productOverviewView = require("../src/home_kitchen/static/product-overview-view.js");
const recipeTranslationsView = require("../src/home_kitchen/static/recipe-translations-view.js");
const recipeQuickFeedback = require("../src/home_kitchen/static/recipe-feedback-quick.js");
const unmappedIngredientsView = require("../src/home_kitchen/static/unmapped-ingredients-view.js");

function luminance(hex) {
  const channels = hex.match(/[0-9a-f]{2}/gi).map((value) => parseInt(value, 16) / 255);
  const linear = channels.map((value) =>
    value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(first, second) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

class MockNode {
  constructor(tag, doc, isText = false, text = "") {
    this.ownerDocument = doc;
    this.isTextNode = isText;
    this.tagName = isText ? "#TEXT" : String(tag).toUpperCase();
    this._textContent = text;
    this.className = "";
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = new Map();
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.open = false;
    this.required = false;
    this.focusCalls = [];
    if (this.tagName === "INPUT") {
      this.attributes.type = "text";
    }
    this.dataset = new Proxy(
      {},
      {
        get: (_target, prop) => {
          if (typeof prop !== "string") return undefined;
          const attr = "data-" + prop.replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
          return this.attributes[attr];
        },
        set: (_target, prop, val) => {
          if (typeof prop !== "string") return false;
          const attr = "data-" + prop.replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
          this.attributes[attr] = String(val);
          return true;
        },
        deleteProperty: (_target, prop) => {
          if (typeof prop !== "string") return false;
          const attr = "data-" + prop.replace(/[A-Z]/g, (ch) => "-" + ch.toLowerCase());
          delete this.attributes[attr];
          return true;
        },
      },
    );
  }

  get type() {
    return this.attributes.type || "";
  }

  set type(val) {
    if (val == null || val === "") {
      delete this.attributes.type;
    } else {
      this.attributes.type = String(val);
    }
  }

  get id() {
    return this.attributes.id || "";
  }

  set id(val) {
    if (val == null || val === "") {
      delete this.attributes.id;
    } else {
      this.attributes.id = String(val);
    }
  }

  get isConnected() {
    let cur = this;
    while (cur) {
      if (cur === this.ownerDocument.body || cur === this.ownerDocument.documentElement) {
        return true;
      }
      cur = cur.parentNode;
    }
    return false;
  }

  get textContent() {
    if (this.isTextNode) return this._textContent;
    return (this._textContent || "") + this.children.map((c) => c.textContent || "").join("");
  }

  set textContent(val) {
    this._textContent = String(val ?? "");
    this.children = [];
  }

  set innerHTML(val) {
    this._textContent = String(val ?? "");
    this.children = [];
  }

  append(...children) {
    for (const child of children) {
      if (!child) continue;
      if (typeof child === "string") {
        const txt = this.ownerDocument.createTextNode(child);
        txt.parentNode = this;
        this.children.push(txt);
      } else {
        if (child.parentNode && child.parentNode !== this) {
          child.parentNode.children = child.parentNode.children.filter((n) => n !== child);
        }
        child.parentNode = this;
        this.children.push(child);
      }
    }
  }

  replaceChildren(...children) {
    for (const prev of this.children) {
      prev.parentNode = null;
    }
    this._textContent = "";
    this.children = [];
    this.append(...children);
  }

  contains(target) {
    let cur = target;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
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

  removeEventListener(name, handler) {
    const list = this.listeners.get(name);
    if (!list) return;
    this.listeners.set(
      name,
      list.filter((fn) => fn !== handler),
    );
  }

  async dispatchEvent(event) {
    const evt = {
      defaultPrevented: false,
      propagationStopped: false,
      target: this,
      currentTarget: this,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {
        this.propagationStopped = true;
      },
      ...event,
    };
    const handlers = [...(this.listeners.get(evt.type) || [])];
    for (const handler of handlers) {
      await handler(evt);
    }
    if (!evt.propagationStopped && this.parentNode && evt.bubbles) {
      await this.parentNode.dispatchEvent(evt);
    }
    return evt;
  }

  focus(options) {
    this.focusCalls.push(options || {});
    this.ownerDocument.activeElement = this;
  }

  showModal() {
    this.open = true;
  }

  async close() {
    if (!this.open) return;
    this.open = false;
    await this.dispatchEvent({ type: "close", bubbles: false });
  }

  async pressEscape() {
    if (!this.open) return;
    const cancelEvt = await this.dispatchEvent({ type: "cancel", bubbles: false });
    if (!cancelEvt.defaultPrevented) {
      await this.close();
    }
  }

  async click() {
    if (this.disabled) return;
    await this.dispatchEvent({ type: "click", bubbles: true });
  }

  async keydown(key, extra = {}) {
    return this.dispatchEvent({ type: "keydown", key, bubbles: true, ...extra });
  }

  async input(val) {
    this.value = val;
    await this.dispatchEvent({ type: "input", bubbles: true });
  }

  async change(val) {
    if (val !== undefined) this.value = val;
    await this.dispatchEvent({ type: "change", bubbles: true });
  }

  async submit() {
    await this.dispatchEvent({ type: "submit", bubbles: true });
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    const selectors = selector.split(",").map((s) => s.trim()).filter(Boolean);
    const results = [];

    function matchesCompound(node, sel) {
      if (!node || node.isTextNode || !node.tagName) return false;
      let rest = sel;
      const tagMatch = rest.match(/^[a-zA-Z0-9]+/);
      if (tagMatch) {
        if (node.tagName !== tagMatch[0].toUpperCase()) return false;
        rest = rest.slice(tagMatch[0].length);
      }
      while (rest.length > 0) {
        if (rest.startsWith("#")) {
          const m = rest.match(/^#([a-zA-Z0-9_-]+)/);
          if (!m) return false;
          if (node.id !== m[1]) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith(".")) {
          const m = rest.match(/^\.([a-zA-Z0-9_-]+)/);
          if (!m) return false;
          const cls = m[1];
          if (!node.className || !node.className.split(/\s+/).includes(cls)) return false;
          rest = rest.slice(m[0].length);
        } else if (rest.startsWith("[")) {
          const m = rest.match(/^\[([a-zA-Z0-9_-]+)(?:([*^$]?=)(["']?)(.*?)\3)?\]/);
          if (!m) return false;
          const [, attr, op, , val] = m;
          const actual = node.getAttribute(attr);
          if (actual === null || actual === undefined) return false;
          if (op === "=" && actual !== val) return false;
          if (op === "*=" && !actual.includes(val)) return false;
          rest = rest.slice(m[0].length);
        } else {
          return false;
        }
      }
      return true;
    }

    function matchesSelector(node, sel, root) {
      const tokens = [];
      let remaining = sel.trim();
      while (remaining.length > 0) {
        const combMatch = remaining.match(/^\s*(>|\s+)\s*/);
        if (combMatch && tokens.length > 0) {
          tokens.push(combMatch[1] === ">" ? ">" : " ");
          remaining = remaining.slice(combMatch[0].length);
          continue;
        }
        const partMatch = remaining.match(/^(?:[^\s>"'\[]|\[[^\]]*\])+/);
        if (!partMatch) return false;
        tokens.push(partMatch[0]);
        remaining = remaining.slice(partMatch[0].length);
      }
      if (!tokens.length || !matchesCompound(node, tokens[tokens.length - 1])) return false;
      let idx = tokens.length - 2;
      let cur = node;
      while (idx >= 0) {
        const comb = tokens[idx];
        const parentSel = tokens[idx - 1];
        if (comb === ">") {
          cur = cur.parentNode;
          if (!cur || !matchesCompound(cur, parentSel)) return false;
        } else {
          cur = cur.parentNode;
          while (cur && cur !== root && !matchesCompound(cur, parentSel)) {
            cur = cur.parentNode;
          }
          if (!cur || (!matchesCompound(cur, parentSel))) return false;
        }
        idx -= 2;
      }
      return true;
    }

    function walk(node) {
      for (const child of node.children) {
        if (selectors.some((sel) => matchesSelector(child, sel, this))) {
          results.push(child);
        }
        walk.call(this, child);
      }
    }

    walk.call(this, this);
    return results;
  }
}

function createMockDocument() {
  const doc = {
    activeElement: null,
    documentElement: null,
    body: null,
    createElement(tag) {
      return new MockNode(tag, doc);
    },
    createTextNode(text) {
      return new MockNode("#text", doc, true, text);
    },
    querySelector(selector) {
      return doc.body.querySelector(selector);
    },
    querySelectorAll(selector) {
      return doc.body.querySelectorAll(selector);
    },
    getElementById(id) {
      return doc.body.querySelector("#" + id);
    },
  };
  doc.documentElement = new MockNode("html", doc);
  doc.body = new MockNode("body", doc);
  doc.documentElement.append(doc.body);
  doc.activeElement = doc.body;
  return doc;
}

function assertExplicitOrWrappingLabel(root, inputNode) {
  const id = inputNode.id;
  const matchingForLabel = id
    ? root.querySelectorAll("label").find((lbl) => lbl.getAttribute("for") === id)
    : null;
  let ancestor = inputNode.parentNode;
  let wrappingLabel = null;
  while (ancestor) {
    if (ancestor.tagName === "LABEL") {
      wrappingLabel = ancestor;
      break;
    }
    ancestor = ancestor.parentNode;
  }
  assert.ok(
    matchingForLabel || wrappingLabel,
    `Expected input (${inputNode.className || inputNode.tagName}) to have a <label for> or wrapping <label>`,
  );
  if (matchingForLabel) {
    assert.ok(matchingForLabel.textContent.trim().length > 0, "Label text should be non-empty");
  }
}

// ---------------------------------------------------------------------------
// 1. Shell landmarks, skip link, focus contrast, and ARIA regions
// ---------------------------------------------------------------------------

test("keyboard users can skip the repeated navigation to main content", () => {
  assert.match(html, /<a class="skip-link" href="#main">Hopp til hovedinnhold<\/a>/);
  assert.match(html, /<main id="main" tabindex="-1">/);
  assert.match(css, /\.skip-link\s*\{[^}]*transform:\s*translateY\(calc\(-100% - 16px\)\)/s);
  assert.match(css, /\.skip-link:focus\s*\{[^}]*transform:\s*translateY\(0\)/s);
});

test("keyboard focus indicator contrasts with the app's light and dark surfaces", () => {
  assert.match(css, /:focus-visible\s*\{[^}]*outline:\s*2px solid #fffef9/s);
  assert.match(css, /:focus-visible\s*\{[^}]*box-shadow:\s*0 0 0 5px #244c3e/s);
  for (const background of ["#fffef9", "#f8f6ef", "#f0f0e5"])
    assert.ok(contrast("#244c3e", background) >= 3, `${background} needs a contrasting dark ring`);
  assert.ok(contrast("#fffef9", "#244c3e") >= 3, "the light ring needs contrast on dark controls");
});

test("index.html exposes accessible landmarks, live status regions, and dialog containers", () => {
  assert.match(html, /<nav aria-label="Hovedmeny">/);
  assert.match(html, /data-view="settings" aria-label="Husholdningsinnstillinger"/);
  assert.match(html, /<div id="notice" role="status" aria-live="polite" hidden><\/div>/);
  assert.match(html, /<dialog id="modal"><div id="modal-content"><\/div><\/dialog>/);
  assert.match(
    html,
    /<dialog id="private-photo-modal" aria-labelledby="private-photo-title"><div id="private-photo-content"><\/div><\/dialog>/,
  );
});

// ---------------------------------------------------------------------------
// 2. Modal / dialog focus-return and Escape / close behavior in app.js
// ---------------------------------------------------------------------------

function extractAppModalHarness(doc) {
  const modalBlockMatch = appSource.match(
    /const modalReturnTargets = new WeakMap\(\);[\s\S]*?\nfunction openDialog\(heading\) \{[\s\S]*?\n\}(?=\nfunction field)/u,
  );
  assert.ok(modalBlockMatch, "app.js should define modal return-focus helpers and openDialog");

  const viewPhotoMatch = appSource.match(
    /async function viewRecipePhoto\(photoSource\) \{[\s\S]*?\n\}(?=\nfunction unmappedIngredientsDialog)/u,
  );
  assert.ok(viewPhotoMatch, "app.js should define viewRecipePhoto");

  const fieldMatch = appSource.match(
    /function field\(label[\s\S]*?\n\}(?=\nfunction formSubmit)/u,
  );
  assert.ok(fieldMatch, "app.js should define field()");

  const feedbackFormMatch = appSource.match(
    /function feedbackForm\(recipe\) \{[\s\S]*?\n\}(?=\nfunction itemForm)/u,
  );
  assert.ok(feedbackFormMatch, "app.js should define feedbackForm()");

  const formSubmitMatch = appSource.match(
    /function formSubmit\(form, text, action[\s\S]*?\n\}(?=\nasync function mutate)/u,
  );
  assert.ok(formSubmitMatch, "app.js should define formSubmit()");

  const modalCancelCloseMatch = appSource.match(
    /\$\("#modal"\)\.addEventListener\("cancel",[\s\S]*?\n\}\);/u,
  );
  assert.ok(modalCancelCloseMatch, "app.js should attach cancel handler to #modal");

  const restoreListenersMatch = appSource.match(
    /\$\("#modal"\)\.addEventListener\("close", \(\) => restoreModalReturnFocus\(\$\("#modal"\)\)\);\n\$\("#private-photo-modal"\)\?\.addEventListener\("close", \(\) =>\n  restoreModalReturnFocus\(\$\("#private-photo-modal"\)\),\n\);/u,
  );
  assert.ok(restoreListenersMatch, "app.js should register close focus-return listeners on both dialogs");

  const state = { cartConfirming: false };
  const context = {
    document: doc,
    state,
    fetch: async () => ({ ok: false }),
    URL: { revokeObjectURL: () => {} },
    api: async () => [],
    notify: () => {},
    recentFeedbackPanel: () => ({ node: doc.createElement("div"), refresh: async () => {} }),
    connectionSettingsButton: () => doc.createElement("button"),
    $: (selector) => doc.querySelector(selector),
    el(tag, text, className) {
      const node = doc.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = text;
      if (className) node.className = className;
      return node;
    },
    add(parent, ...children) {
      children.filter(Boolean).forEach((child) => parent.append(child));
      return parent;
    },
    button(text, action, className = "") {
      const node = context.el("button", text, className);
      node.type = "button";
      node.addEventListener("click", action);
      return node;
    },
    WeakMap,
  };

  vm.runInNewContext(
    [
      modalBlockMatch[0],
      viewPhotoMatch[0],
      fieldMatch[0],
      formSubmitMatch[0],
      feedbackFormMatch[0],
      modalCancelCloseMatch[0],
      restoreListenersMatch[0],
    ].join("\n"),
    context,
  );

  return { context, state };
}

test("modal open/close via close button and Escape restores focus to triggering element", async () => {
  const doc = createMockDocument();
  const main = doc.createElement("main");
  main.id = "main";
  main.setAttribute("tabindex", "-1");
  const triggerBtn = doc.createElement("button");
  triggerBtn.textContent = "Legg til oppskrift";
  main.append(triggerBtn);

  const modal = doc.createElement("dialog");
  modal.id = "modal";
  const modalContent = doc.createElement("div");
  modalContent.id = "modal-content";
  modal.append(modalContent);

  const photoModal = doc.createElement("dialog");
  photoModal.id = "private-photo-modal";
  const photoContent = doc.createElement("div");
  photoContent.id = "private-photo-content";
  photoModal.append(photoContent);

  doc.body.append(main, modal, photoModal);

  const { context, state } = extractAppModalHarness(doc);

  // 1. Focus trigger, open dialog, close via close button
  triggerBtn.focus();
  assert.equal(doc.activeElement, triggerBtn);

  const root = context.openDialog("Ny oppskrift");
  assert.equal(modal.open, true);
  assert.equal(modal.getAttribute("aria-labelledby"), "dialog-title");
  const closeBtn = root.querySelector("button");
  assert.equal(closeBtn.getAttribute("aria-label"), "Lukk dialogvindu");

  closeBtn.focus();
  assert.equal(doc.activeElement, closeBtn);
  await closeBtn.click();
  assert.equal(modal.open, false);
  assert.equal(doc.activeElement, triggerBtn, "closing via × button returns focus to trigger");

  // 2. Re-open dialog, close via Escape (cancel -> close)
  context.openDialog("Bekreft");
  assert.equal(modal.open, true);
  closeBtn.focus();
  await modal.pressEscape();
  assert.equal(modal.open, false);
  assert.equal(doc.activeElement, triggerBtn, "closing via Escape returns focus to trigger");

  // 3. When state.cartConfirming is true, Escape cancel is prevented and modal stays open
  context.openDialog("Bekreft handlekurv");
  state.cartConfirming = true;
  await modal.pressEscape();
  assert.equal(modal.open, true, "Escape should not close modal while cartConfirming is active");
  state.cartConfirming = false;
  await modal.pressEscape();
  assert.equal(modal.open, false);
  assert.equal(doc.activeElement, triggerBtn);
});

test("modal-to-modal transition preserves original external trigger and handles disconnected trigger fallback", async () => {
  const doc = createMockDocument();
  const main = doc.createElement("main");
  main.id = "main";
  main.setAttribute("tabindex", "-1");

  const outerTrigger = doc.createElement("button");
  outerTrigger.textContent = "Åpne meny";
  outerTrigger.dataset.focusKey = "planner-add-btn";
  main.append(outerTrigger);

  const modal = doc.createElement("dialog");
  modal.id = "modal";
  const modalContent = doc.createElement("div");
  modalContent.id = "modal-content";
  modal.append(modalContent);

  const photoModal = doc.createElement("dialog");
  photoModal.id = "private-photo-modal";
  const photoContent = doc.createElement("div");
  photoContent.id = "private-photo-content";
  photoModal.append(photoContent);

  doc.body.append(main, modal, photoModal);
  const { context } = extractAppModalHarness(doc);

  // Open first modal from outerTrigger, focus an inner button, transition to second modal
  outerTrigger.focus();
  const firstRoot = context.openDialog("Steg 1");
  const innerBtn = doc.createElement("button");
  innerBtn.textContent = "Neste";
  firstRoot.append(innerBtn);
  innerBtn.focus();

  const secondRoot = context.openDialog("Steg 2");
  assert.equal(modal.open, true);
  const photoTrigger = doc.createElement("button");
  photoTrigger.textContent = "Vis originalbildet";
  secondRoot.append(photoTrigger);
  photoTrigger.focus();

  // Open secondary #private-photo-modal from inside #modal, close it -> returns to photoTrigger
  await context.viewRecipePhoto({ id: "photo-1" });
  assert.equal(photoModal.open, true);
  const photoCloseBtn = photoContent.querySelector("button");
  assert.equal(photoCloseBtn.getAttribute("aria-label"), "Lukk originalbildet");
  await photoCloseBtn.click();
  assert.equal(photoModal.open, false);
  assert.equal(doc.activeElement, photoTrigger, "closing private-photo-modal restores focus to inner trigger");

  // Close #modal -> returns focus to original outerTrigger, NOT removed innerBtn
  await modal.pressEscape();
  assert.equal(modal.open, false);
  assert.equal(doc.activeElement, outerTrigger, "closing chained modal returns focus to original external trigger");

  // Disconnected trigger with matching [data-focus-key] replacement
  outerTrigger.focus();
  context.openDialog("Rediger");
  main.replaceChildren();
  const replacementBtn = doc.createElement("button");
  replacementBtn.dataset.focusKey = "planner-add-btn";
  main.append(replacementBtn);
  await modal.close();
  assert.equal(doc.activeElement, replacementBtn, "falls back to matching data-focus-key element when trigger is replaced");

  // Disconnected trigger without replacement falls back to #main
  const ephemeralBtn = doc.createElement("button");
  main.append(ephemeralBtn);
  ephemeralBtn.focus();
  context.openDialog("Slett");
  main.replaceChildren();
  await modal.close();
  assert.equal(doc.activeElement, main, "falls back to #main when trigger is removed without replacement");
});

test("app.js feedbackForm notes and inputs use <label for> associations and role=alert error container", () => {
  const doc = createMockDocument();
  const main = doc.createElement("main");
  main.id = "main";
  const modal = doc.createElement("dialog");
  modal.id = "modal";
  const modalContent = doc.createElement("div");
  modalContent.id = "modal-content";
  modal.append(modalContent);
  doc.body.append(main, modal);

  const { context } = extractAppModalHarness(doc);
  context.feedbackForm({ id: "rec-1", title: "Suppe" });

  const inputs = modalContent.querySelectorAll("input, select, textarea");
  assert.equal(inputs.length, 3);
  for (const input of inputs) {
    assertExplicitOrWrappingLabel(modalContent, input);
  }
  const errNode = modalContent.querySelector(".form-error");
  assert.ok(errNode);
  assert.equal(errNode.getAttribute("role"), "alert");
});

// ---------------------------------------------------------------------------
// 3. product-overview-view.js ARIA, labels, keyboard, SKU toggles, merge & stock
// ---------------------------------------------------------------------------

test("product-overview-view exposes accessible labels, keyboard alias submit, live status, SKU toggles, merge, and stock edit", async () => {
  const doc = createMockDocument();
  globalThis.document = doc;
  const container = doc.createElement("div");
  doc.body.append(container);

  const calls = [];
  const productA = {
    id: "prod-a",
    canonical_name: "carrot",
    display_name: "gulrot",
    aliases: [
      { alias: "gulrot", locale: "nb", context: "recipe", reviewed: true },
      { alias: "carotte", locale: "fr", context: "recipe", reviewed: false },
    ],
    stock: { in_stock: true, total_quantity: "2", unit: "kg", items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [
      { oda_product_id: "sku-1", rank: 1, name: "Norske gulrøtter" },
      { oda_product_id: "sku-2", rank: 2, name: "Øko gulrøtter" },
    ],
    excluded_oda_skus: [{ oda_product_id: "sku-bad", name: "Dårlige gulrøtter", reason: "Smak" }],
    remembered_oda_skus: [{ oda_product_id: "sku-rem", name: "Minigulrøtter", selection_count: 1 }],
    previously_purchased_skus: [
      { sku_id: "sku-pur", provider: "oda", name: "Vaskede gulrøtter", purchase_count: 2, provenance: "provider_verified" },
    ],
  };
  const productB = {
    id: "prod-b",
    canonical_name: "carrots",
    display_name: "gulrøtter",
    aliases: [],
    stock: { in_stock: false, total_quantity: "0", unit: null, items: [], provenance: "kitchen_inventory" },
    purchase_frequency: { status: "unknown", frequency_display: "unknown" },
    preferred_oda_skus: [],
    excluded_oda_skus: [],
    remembered_oda_skus: [],
    previously_purchased_skus: [],
  };

  let foodPreferred = false;
  const mockApi = async (endpoint, method = "GET", body = null) => {
    calls.push({ endpoint, method, body });
    if (endpoint.startsWith("/catalogue/overview")) return [productA, productB];
    if (endpoint.startsWith("/catalogue/foods?")) {
      return [
        {
          id: "food-1",
          source_item_id: "06.001",
          source_name: "Gulrot, rå",
          display_name: "Gulrot",
          category: "Vegetables",
          default_unit: "kg",
        },
      ];
    }
    if (endpoint === "/catalogue/foods/food-1/products") {
      return [
        {
          id: "store-prod-1",
          provider: "oda",
          product_id: "oda-10",
          preferred: foodPreferred,
          snapshot: { name: "Norsk Gulrot 1kg" },
        },
      ];
    }
    if (endpoint === "/catalogue/products/store-prod-1/preferred") {
      foodPreferred = method === "PUT";
      return {};
    }
    if (endpoint.endsWith("/review")) return {};
    if (endpoint.endsWith("/preferences")) {
      return {
        ...productA,
        preferred_oda_skus: body.preferred_oda_skus,
        excluded_oda_skus: body.excluded_oda_skus,
      };
    }
    if (endpoint.endsWith("/stock")) {
      return {
        ...productA,
        stock: { in_stock: true, total_quantity: body.quantity, unit: body.unit, items: [], provenance: "kitchen_inventory" },
      };
    }
    if (endpoint.includes("/merge-preview")) {
      return {
        target_product_id: "prod-b",
        source_product_id: "prod-a",
        target_canonical_name: "carrots",
        source_canonical_name: "carrot",
        target_variant: "",
        source_variant: "",
        target_display_name: "gulrøtter",
        source_display_name: "gulrot",
        variant_mismatch: false,
        aliases_to_move: 2,
        stock_rows_to_move: 1,
        recipe_links_to_move: 0,
        preferences_to_move: 2,
        exclusions_to_move: 1,
        purchase_history_rows_to_move: 0,
      };
    }
    if (endpoint.endsWith("/merge")) return productB;
    throw new Error(`Unexpected ${method} ${endpoint}`);
  };

  const view = productOverviewView.renderProductOverview(container, { api: mockApi, locale: "nb" });
  await view.ready;

  // Search and live status region
  const searchInput = container.querySelector(".catalogue-search-input");
  assert.equal(searchInput.getAttribute("aria-label"), "Søk etter råvare");
  const overviewRoot = container.querySelector(".product-overview-view");
  const overviewStatus = overviewRoot.querySelector('.visually-hidden[role="status"]');
  assert.ok(overviewStatus);
  assert.equal(overviewStatus.getAttribute("aria-live"), "polite");
  assert.equal(overviewStatus.textContent, "Fant 2 råvarer.");

  const cardA = container.querySelectorAll(".product-overview-card")[0];
  const sections = cardA.querySelectorAll("section");
  assert.equal(sections.length, 5);
  for (const sec of sections) {
    assert.ok(sec.getAttribute("aria-label"), "Every product card section requires aria-label");
  }

  // Alias confirm button has descriptive aria-label naming alias + locale
  const confirmAliasBtn = cardA.querySelector(".confirm-alias-btn");
  assert.ok(confirmAliasBtn);
  assert.match(confirmAliasBtn.getAttribute("aria-label"), /carotte/i);
  assert.match(confirmAliasBtn.getAttribute("aria-label"), /FR/);

  // Keyboard Enter on alias input triggers PUT /review
  const aliasInput = cardA.querySelector(".alias-add-input");
  assert.equal(aliasInput.getAttribute("aria-label"), "Nytt kallenavn");
  assert.equal(cardA.querySelector(".alias-locale-select").getAttribute("aria-label"), "Språk for kallenavn");
  aliasInput.value = "morkov";
  await aliasInput.keydown("Enter");
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(calls.some((c) => c.method === "PUT" && c.endpoint.includes("/review") && c.body.alias === "morkov"));

  // Inline stock edit form inputs have aria-labels
  const stockQty = cardA.querySelector(".stock-qty-input");
  const stockUnit = cardA.querySelector(".stock-unit-input");
  const stockNote = cardA.querySelector(".stock-note-input");
  assert.equal(stockQty.getAttribute("aria-label"), "Mengde på lager");
  assert.equal(stockUnit.getAttribute("aria-label"), "Enhet for lagerbeholdning");
  assert.equal(stockNote.getAttribute("aria-label"), "Notat for lagerbeholdning");
  stockQty.value = "5";
  await cardA.querySelector(".stock-edit-form").submit();
  assert.ok(calls.some((c) => c.method === "PUT" && c.endpoint === "/catalogue/products/prod-a/stock"));

  // SKU priority/exclude controls have item-specific aria-labels and status live region updates
  const moveDownBtn = cardA.querySelector(".move-down-btn");
  assert.match(moveDownBtn.getAttribute("aria-label"), /Norske gulrøtter/);
  await moveDownBtn.click();
  const prefStatus = cardA.querySelector(".preferences-status-box");
  assert.equal(prefStatus.getAttribute("role"), "status");
  assert.equal(prefStatus.getAttribute("aria-live"), "polite");
  assert.equal(prefStatus.hidden, false);
  assert.match(prefStatus.textContent, /Oda-preferanser for «gulrot» er lagret/);

  // Merge select has aria-label and previews impact before confirming
  const mergeSelect = cardA.querySelector(".merge-target-select");
  assert.equal(mergeSelect.getAttribute("aria-label"), "Velg målråvare å slå sammen inn i");
  mergeSelect.value = "prod-b";
  await cardA.querySelector(".merge-product-form").submit();
  assert.ok(calls.some((c) => c.method === "GET" && c.endpoint.includes("/catalogue/products/prod-b/merge-preview")));
  const mergePreviewBox = cardA.querySelector(".merge-preview-box");
  assert.ok(mergePreviewBox);
  assert.equal(mergePreviewBox.getAttribute("role"), "region");
  await cardA.querySelector(".confirm-merge-btn").click();
  assert.ok(calls.some((c) => c.method === "POST" && c.endpoint === "/catalogue/products/prod-b/merge"));

  // Curated foods section: aria-expanded on product links button, aria-pressed on SKU toggle
  const curatedHost = container.querySelector(".curated-catalogue-host");
  const details = curatedHost.querySelector("details");
  details.open = true;
  await details.dispatchEvent({ type: "toggle" });
  await new Promise((r) => setTimeout(r, 10));

  const curatedSearch = curatedHost.querySelector('input[type="search"]');
  assert.equal(curatedSearch.getAttribute("aria-label"), "Søk i Matvaretabellen");

  const loadOptsBtn = curatedHost.querySelector(".curated-food-card button");
  assert.equal(loadOptsBtn.getAttribute("aria-expanded"), "false");
  assert.match(loadOptsBtn.getAttribute("aria-label"), /Gulrot/);
  await loadOptsBtn.click();
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(loadOptsBtn.getAttribute("aria-expanded"), "true");

  const skuToggleBtn = curatedHost.querySelector(".curated-food-options button");
  assert.equal(skuToggleBtn.getAttribute("aria-pressed"), "false");
  assert.match(skuToggleBtn.getAttribute("aria-label"), /Norsk Gulrot 1kg/);
  await skuToggleBtn.click();
  await new Promise((r) => setTimeout(r, 10));
  const updatedSkuToggleBtn = curatedHost.querySelector(".curated-food-options button");
  assert.equal(updatedSkuToggleBtn.getAttribute("aria-pressed"), "true");
});

test("product-overview-view renders role=alert when loading products or curated foods fails", async () => {
  const doc = createMockDocument();
  globalThis.document = doc;
  const container = doc.createElement("div");

  const failingApi = async () => {
    throw new Error("Server utilgjengelig");
  };
  const view = productOverviewView.renderProductOverview(container, { api: failingApi, locale: "nb" });
  await view.ready;

  const errorCard = container.querySelector('.callout.error[role="alert"]');
  assert.ok(errorCard, "Overview load failure should expose role=alert");
  assert.match(errorCard.textContent, /Kunne ikke laste råvareoversikt/);

  const curated = productOverviewView.renderCuratedFoodsSection(doc.createElement("div"), {
    api: failingApi,
  });
  await curated.loadFoods("gulrot");
  const curatedErr = curated.details.querySelector('.callout.error[role="alert"]');
  assert.ok(curatedErr, "Curated foods failure should expose role=alert");
});

// ---------------------------------------------------------------------------
// 4. recipe-translations-view.js locale select, edit form labels, Escape, alerts
// ---------------------------------------------------------------------------

test("recipe-translations-view provides accessible locale controls, <label for> fields, Escape cancel with focus return, and status/alert live regions", async () => {
  const doc = createMockDocument();
  globalThis.document = doc;
  const container = doc.createElement("div");
  doc.body.append(container);

  const recipe = {
    id: "rec-a11y",
    title: "Tomato Soup",
    source_language: "en",
    ingredients: [{ name: "tomatoes", quantity: 500, unit: "g", category: "Produce" }],
    instructions: ["Boil tomatoes", "Blend smooth"],
  };

  const mockApi = async (endpoint, method = "GET", body = null) => {
    if (endpoint.endsWith("/translations") && method === "GET") return [];
    if (endpoint.endsWith("/translations/nb") && method === "PUT") {
      return {
        id: "tr-1",
        recipe_id: "rec-a11y",
        target_locale: "nb",
        ...body,
        provider: "manual",
        model: "manual",
        is_stale: false,
      };
    }
    return {};
  };

  recipeTranslationsView.attachRecipeTranslation(container, recipe, { api: mockApi });

  const panel = container.querySelector(".recipe-translation-panel");
  assert.equal(panel.getAttribute("aria-label"), "Oppskriftsspråk og oversettelse");

  const langBadge = container.querySelector(".recipe-language-badge");
  assert.match(langBadge.getAttribute("aria-label"), /Engelsk/);

  const localeSelect = container.querySelector(".translation-locale-select");
  assert.equal(localeSelect.getAttribute("aria-label"), "Velg målspråk for oversettelse");
  await localeSelect.change("fr");
  assert.match(container.querySelector(".translate-btn").textContent, /Fransk/);
  await localeSelect.change("nb");

  const editBtn = container.querySelector(".edit-translation-btn");
  assert.equal(editBtn.getAttribute("aria-expanded"), "false");

  await editBtn.click();
  assert.equal(editBtn.getAttribute("aria-expanded"), "true");

  const editForm = container.querySelector(".translation-edit-form");
  assert.equal(editForm.hidden, false);
  assert.equal(editForm.getAttribute("aria-label"), "Rediger oversettelse manuelt");

  const titleInput = container.querySelector(".translation-edit-title");
  assert.equal(doc.activeElement, titleInput, "opening Rediger oversettelse moves focus to title input");

  // Every input/textarea in the translation edit form has <label for> + id + aria-label
  const formFields = editForm.querySelectorAll("input, textarea");
  assert.ok(formFields.length >= 5);
  for (const fieldNode of formFields) {
    assertExplicitOrWrappingLabel(editForm, fieldNode);
    assert.ok(fieldNode.getAttribute("aria-label"), "Translation form input requires aria-label");
  }

  const removeIngBtn = editForm.querySelector(".remove-translation-ingredient-btn");
  assert.equal(removeIngBtn.getAttribute("aria-label"), "Fjern ingrediens 1");

  // Pressing Escape inside the edit form cancels and returns focus to editBtn
  await titleInput.keydown("Escape");
  assert.equal(editForm.hidden, true);
  assert.equal(editBtn.getAttribute("aria-expanded"), "false");
  assert.equal(doc.activeElement, editBtn, "Escape in translation edit form returns focus to editBtn");

  // Re-open, trigger validation error -> role="alert", then save -> role="status"
  await editBtn.click();
  container.querySelector(".translation-edit-title").value = "";
  await container.querySelector(".save-translation-btn").click();
  const errorBox = container.querySelector(".translation-error-message");
  assert.equal(errorBox.hidden, false);
  assert.equal(errorBox.getAttribute("role"), "alert");
  assert.match(errorBox.textContent, /Oppgi en tittel/);

  container.querySelector(".translation-edit-title").value = "Tomatsuppe";
  await container.querySelector(".save-translation-btn").click();
  const statusBox = container.querySelector(".translation-status-message");
  assert.equal(statusBox.hidden, false);
  assert.equal(statusBox.getAttribute("role"), "status");
  assert.equal(statusBox.getAttribute("aria-live"), "polite");
  assert.match(statusBox.textContent, /Oversettelsen er lagret/);
});

// ---------------------------------------------------------------------------
// 5. recipe-feedback-quick.js toggles, aria-pressed, role=status & role=alert
// ---------------------------------------------------------------------------

test("recipe-feedback-quick exposes group roles, aria-pressed toggles, aria-label, role=status, and role=alert", async () => {
  const doc = createMockDocument();
  const bar = recipeQuickFeedback.renderQuickFeedbackBar(
    { id: "rec-qf-1", activity_stats: { latest_rating: 3, would_make_again: null } },
    null,
    {
      document: doc,
      api: async (_endpoint, _method, payload) => {
        if (payload.would_make_again === false) {
          throw new Error("Nettverksfeil");
        }
        return { ok: true };
      },
    },
  );

  assert.equal(bar.getAttribute("role"), "group");
  assert.equal(bar.getAttribute("aria-label"), "Hurtigvurdering for oppskrift");

  const groups = bar.querySelectorAll('[role="group"]');
  assert.equal(groups.length, 2);
  assert.equal(groups[0].getAttribute("aria-label"), "Gi terningkast eller stjernevurdering fra 1 til 5");
  assert.equal(groups[1].getAttribute("aria-label"), "Anbefal eller fraråd oppskrift");

  assert.equal(bar.starButtons.length, 5);
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "true");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[0].getAttribute("aria-label"), "1 stjerne av 5");
  assert.equal(bar.starButtons[4].getAttribute("aria-label"), "5 stjerner av 5");

  assert.equal(bar.statusElement.getAttribute("role"), "status");
  assert.equal(bar.statusElement.getAttribute("aria-live"), "polite");
  assert.equal(bar.errorElement.getAttribute("role"), "alert");
  assert.equal(bar.errorElement.hidden, true);

  await bar.starButtons[4].click();
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "true");
  assert.match(bar.statusElement.textContent, /5 av 5 stjerner/);

  await bar.thumbUpButton.click();
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");
  assert.match(bar.statusElement.textContent, /Tommel opp/);

  await bar.thumbDownButton.click();
  assert.equal(bar.errorElement.hidden, false);
  assert.match(bar.errorElement.textContent, /Kunne ikke lagre anbefaling/);
});

// ---------------------------------------------------------------------------
// 6. unmapped-ingredients-view.js keyboard search, aria-pressed candidates, live regions
// ---------------------------------------------------------------------------

test("unmapped-ingredients-view supports keyboard Enter search, aria-pressed candidate toggles, labels, and live regions", async () => {
  const doc = createMockDocument();
  const root = doc.createElement("div");
  doc.body.append(root);

  const item = {
    recipe_id: "rec-unm-1",
    recipe_title: "Fiskegryte",
    ingredient_index: 0,
    name: "torsk",
    quantity: 400,
    unit: "g",
    canonical_product_id: null,
    canonical_unit_id: null,
  };

  const api = async (endpoint) => {
    if (endpoint.startsWith("/catalogue/unmapped-ingredients")) return [item];
    if (endpoint.startsWith("/catalogue/aliases/product")) {
      return {
        status: "ambiguous",
        candidates: [
          { id: "p-1", canonical_name: "Torsk", variant: "fersk" },
          { id: "p-2", canonical_name: "Torsk", variant: "saltet" },
        ],
      };
    }
    if (endpoint.startsWith("/catalogue/aliases/unit")) {
      return {
        status: "resolved",
        candidates: [{ id: "u-1", canonical_name: "gram" }],
      };
    }
    return {};
  };

  const view = unmappedIngredientsView.renderReviewDialog(root, { document: doc, api });
  await view.ready;

  assert.equal(root.querySelector(".unmapped-status").getAttribute("role"), "status");
  assert.equal(root.querySelector(".unmapped-status").getAttribute("aria-live"), "polite");
  assert.equal(root.querySelector(".unmapped-error").getAttribute("role"), "alert");
  assert.equal(root.querySelector(".unmapped-filter").getAttribute("aria-label"), "Filtrer etter oppskrift");
  assert.ok(root.querySelector(".unmapped-automap-btn").getAttribute("aria-label"));

  const card = root.querySelector(".unmapped-ingredient-card");
  const searchInputs = card.querySelectorAll('input[type="search"]');
  assert.equal(searchInputs.length, 2);

  // Trigger product search via keyboard Enter
  await searchInputs[0].keydown("Enter");
  await new Promise((r) => setTimeout(r, 10));

  const prodResultsRegion = card.querySelectorAll(".unmapped-search-results")[0];
  assert.equal(prodResultsRegion.getAttribute("role"), "region");
  assert.equal(prodResultsRegion.getAttribute("aria-live"), "polite");

  const candBtns = prodResultsRegion.querySelectorAll(".unmapped-candidate-btn");
  assert.equal(candBtns.length, 2);
  assert.equal(candBtns[0].getAttribute("aria-pressed"), "false");
  assert.equal(candBtns[1].getAttribute("aria-pressed"), "false");

  await candBtns[0].click();
  assert.equal(candBtns[0].getAttribute("aria-pressed"), "true");
  assert.equal(candBtns[1].getAttribute("aria-pressed"), "false");

  const selectionStatus = card.querySelector(".unmapped-selection-info");
  assert.equal(selectionStatus.getAttribute("role"), "status");
  assert.equal(selectionStatus.getAttribute("aria-live"), "polite");
  assert.match(selectionStatus.textContent, /Valgt råvare: Torsk \(fersk\)/);

  // Unit search via keyboard Enter
  await searchInputs[1].keydown("Enter");
  await new Promise((r) => setTimeout(r, 10));
  const unitCandBtn = card.querySelectorAll(".unmapped-search-results")[1].querySelector(".unmapped-candidate-btn");
  assert.equal(unitCandBtn.getAttribute("aria-pressed"), "false");
  await unitCandBtn.click();
  assert.equal(unitCandBtn.getAttribute("aria-pressed"), "true");

  // Alias checkbox is labeled and map button is enabled with aria-label
  const aliasCheckbox = card.querySelector('input[type="checkbox"]');
  assertExplicitOrWrappingLabel(card, aliasCheckbox);
  assert.ok(aliasCheckbox.getAttribute("aria-label"));

  const mapBtn = card.querySelector(".unmapped-map-btn");
  assert.equal(mapBtn.disabled, false);
  assert.match(mapBtn.getAttribute("aria-label"), /torsk/);
});
