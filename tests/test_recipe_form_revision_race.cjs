"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");
const KitchenRecipeEditor = require("../src/home_kitchen/static/recipe-editor.js");

const appSource = fs.readFileSync("src/home_kitchen/static/app.js", "utf8");
const functionStart = appSource.indexOf("function recipeForm(");
const functionEnd = appSource.indexOf("\n}\nfunction recipeImageCredit", functionStart) + 2;
assert.ok(functionStart >= 0 && functionEnd > functionStart, "recipeForm source is located");
const recipeFormSource = appSource.slice(functionStart, functionEnd);

class Node {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
    this.attributes = {};
    this.value = "";
    this.disabled = false;
    this.parentNode = null;
  }
  append(child) {
    child.parentNode = this;
    this.children.push(child);
  }
  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    children.forEach((child) => this.append(child));
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  focus() { this.focused = true; }
  get isConnected() {
    if (this.parentNode) return this.parentNode.isConnected;
    return this.connected === true;
  }
  get text() {
    return this.textContent + " " + this.children.map((child) => child.text || "").join(" ");
  }
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) {
      const found = child.find?.(predicate);
      if (found) return found;
    }
    return null;
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function fixture() {
  const modal = {
    open: false,
    dataset: {},
    showModal() { this.open = true; },
    close() { this.open = false; },
  };
  const content = new Node("dialog-content");
  content.connected = true;
  const state = { household: { default_servings: 2 }, recipes: [] };
  const fields = [];
  const submitActions = [];
  const requests = [];
  const notifications = [];
  const context = {
    state,
    window: { KitchenRecipeEditor, KitchenShoppingView: {} },
    $(selector) {
      if (selector === "#modal") return modal;
      if (selector === "#modal-content") return content;
      throw new Error(`Unexpected selector ${selector}`);
    },
    openDialog() {
      if (modal.open) modal.close();
      content.replaceChildren();
      modal.showModal();
      return content;
    },
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add(parent, ...children) {
      children.filter(Boolean).forEach((child) => parent.append(child));
      return parent;
    },
    button(text, action, className = "") {
      const node = new Node("button", text, className);
      node.action = action;
      return node;
    },
    field(label, type = "text", value = "", help = "") {
      const wrap = new Node("label", label);
      const input = new Node(type === "textarea" ? "textarea" : "input");
      input.value = value == null ? "" : String(value);
      input.fieldLabel = label;
      wrap.append(input);
      if (help) wrap.append(new Node("small", help));
      fields.push(input);
      return { input, wrap };
    },
    formSubmit(form, label, action) {
      form.submitAction = action;
      form.submitLabel = label;
      submitActions.push(action);
    },
    recipeAssistantConfigured: () => true,
    connectionSettingsButton: () => new Node("button", "Settings"),
    recipeImageCredit: () => null,
    purchaseEstimateText: () => "",
    formatUiCount: String,
    httpsUrl: () => true,
    api: async (...args) => {
      requests.push(args);
      if (args[0] === "/recipe-drafts/ai") return context.aiResponse.promise;
      if (args[0] === "/recipes") return [];
      return {};
    },
    aiResponse: deferred(),
    notify: (message) => notifications.push(message),
    navigate: () => {},
  };
  vm.runInNewContext(recipeFormSource, context);
  return { context, content, fields, modal, notifications, requests, state, submitActions };
}

function openEditor(value, draft) {
  value.context.recipeForm(draft, "manual");
  const revisionButton = value.content.find(
    (node) => node.tagName === "button" && node.textContent === "Se over KI-forslaget",
  );
  assert.ok(revisionButton, "AI revision button is rendered");
  return revisionButton;
}

function input(value, label, occurrence = -1) {
  const matches = value.fields.filter((field) => field.fieldLabel === label);
  const selected = occurrence < 0 ? matches.at(occurrence) : matches[occurrence];
  assert.ok(selected, `field ${label} exists`);
  return selected;
}

test("actual recipe modal keeps edited draft B when a deferred response for closed A resolves", async () => {
  const value = fixture();
  const reviseA = openEditor(value, {
    id: "recipe-a",
    title: "Recipe A",
    servings: 2,
    ingredients: [{ name: "salt", quantity: "1", unit: "tsp" }],
    instructions: ["Cook A"],
  });
  input(value, "Hva vil du endre?").value = "Make it simpler";
  const pendingA = reviseA.action();
  assert.equal(reviseA.disabled, true);
  assert.equal(input(value, "Oppskriftsnavn").disabled, false, "editor fields remain editable while AI is pending");

  value.modal.close();
  openEditor(value, {
    id: "recipe-b",
    title: "Recipe B",
    servings: 2,
    ingredients: [{ name: "tomato", quantity: "2", unit: "stk" }],
    instructions: ["Cook B"],
  });
  input(value, "Oppskriftsnavn").value = "Recipe B unsaved edit";
  input(value, "Mengde").value = "3";

  value.context.aiResponse.resolve({ adapted: { title: "Stale AI Recipe A" } });
  await pendingA;

  assert.equal(value.modal.open, true);
  assert.doesNotMatch(value.content.text, /Stale AI Recipe A/u);
  assert.equal(input(value, "Oppskriftsnavn").value, "Recipe B unsaved edit");
  assert.equal(input(value, "Mengde").value, "3");
});

test("actual recipe modal keeps manual edits made in A while AI is pending", async () => {
  const value = fixture();
  const reviseA = openEditor(value, {
    id: "recipe-a",
    title: "Recipe A",
    servings: 2,
    ingredients: [{ name: "salt", quantity: "1", unit: "tsp" }],
    instructions: ["Cook A"],
  });
  input(value, "Hva vil du endre?").value = "Make it simpler";
  const pendingA = reviseA.action();
  input(value, "Oppskriftsnavn").value = "Manual title retained";
  input(value, "Mengde").value = "4";
  value.context.aiResponse.resolve({ adapted: { title: "Stale AI A" } });
  await pendingA;

  assert.equal(value.modal.open, true);
  assert.equal(input(value, "Oppskriftsnavn").value, "Manual title retained");
  assert.equal(input(value, "Mengde").value, "4");
  assert.match(value.content.text, /Oppskriften ble endret mens KI-forslaget ble laget/u);
});

test("failed actual recipe revision keeps form edits and never saves the original", async () => {
  const value = fixture();
  const revise = openEditor(value, {
    id: "recipe-a",
    title: "Saved recipe title",
    servings: 2,
    ingredients: [{ name: "salt", quantity: "1", unit: "tsp" }],
    instructions: ["Cook A"],
  });
  input(value, "Hva vil du endre?").value = "Make it simpler";
  const pending = revise.action();
  input(value, "Oppskriftsnavn").value = "Unsaved manual title";
  input(value, "Mengde").value = "5";

  value.context.aiResponse.reject(new Error("AI request failed"));
  await pending;

  assert.equal(value.modal.open, true);
  assert.equal(input(value, "Oppskriftsnavn").value, "Unsaved manual title");
  assert.equal(input(value, "Mengde").value, "5");
  assert.match(value.content.text, /AI request failed/u);
  assert.equal(value.requests.some((request) => request[0] === "/recipes/recipe-a"), false);
});

test("recipe validation associates each field error, focuses the first invalid field, and preserves unknown amounts", async () => {
  const value = fixture();
  value.context.recipeForm({
    title: "Soup",
    servings: 2,
    ingredients: [{ name: "salt", quantity: "1", unit: "" }],
    instructions: ["Cook"],
  }, "manual");
  const name = input(value, "Navn");
  const quantity = input(value, "Mengde");
  const unit = input(value, "Enhet");
  const save = value.submitActions.at(-1);

  await assert.rejects(save(), /Rett ingrediensfeltene/u);
  assert.equal(unit.attributes["aria-invalid"], "true");
  assert.equal(unit.focused, true);
  const unitError = value.content.find((node) =>
    node.tagName === "span" && node.textContent === "Oppgi enheten når du har oppgitt en mengde.",
  );
  assert.ok(unitError);
  assert.equal(unit.attributes["aria-describedby"], unitError.id);

  name.value = "";
  quantity.value = "bad";
  await assert.rejects(save(), /Rett ingrediensfeltene/u);
  assert.equal(name.attributes["aria-invalid"], "true");
  assert.equal(name.focused, true);
  const nameError = value.content.find((node) =>
    node.tagName === "span" && node.textContent === "Oppgi navnet på ingrediensen.",
  );
  assert.ok(nameError);
  assert.equal(name.attributes["aria-describedby"], nameError.id);
  assert.equal(quantity.attributes["aria-invalid"], "true");
  const quantityError = value.content.find((node) =>
    node.tagName === "span" && node.textContent.startsWith("Oppgi en gyldig positiv mengde"),
  );
  assert.ok(quantityError);
  assert.equal(quantity.attributes["aria-describedby"], quantityError.id);

  name.value = "salt";
  quantity.value = "";
  unit.value = "";
  await save();
  assert.equal(name.attributes["aria-invalid"], "false");
  assert.equal(name.attributes["aria-describedby"], undefined);
  assert.equal(quantity.attributes["aria-invalid"], "false");
  assert.equal(quantity.attributes["aria-describedby"], undefined);
  assert.equal(unit.attributes["aria-invalid"], "false");
  assert.equal(unit.attributes["aria-describedby"], undefined);
  assert.equal(value.requests.some((request) => request[0] === "/recipes"), true);
});

test("active actual recipe modal applies AI review and retains metadata semantics on explicit save", async () => {
  const value = fixture();
  const revise = openEditor(value, {
    id: "mapped-recipe",
    title: "Mapped soup",
    servings: 1,
    ingredients: [{
      name: "Old unmatched phrase",
      quantity: "1.25",
      unit: "handfuls",
      canonical_product_id: "product-id",
      canonical_unit_id: "unit-id",
      source_unit_expression: "Handfuls",
      source_indices: [0],
    }],
    instructions: ["Cook"],
    source_ingredients: [{ index: 0, text: "Handfuls Old unmatched phrase" }],
  });
  input(value, "Hva vil du endre?").value = "Make it simpler";
  const pending = revise.action();
  value.context.aiResponse.resolve({
    adapted: {
      title: "Mapped soup revised",
      servings: 1,
      ingredients: [{ name: "Old unmatched phrase", quantity: "2.5", unit: "handfuls", source_indices: [0] }],
      instructions: ["Cook"],
    },
  });
  await pending;

  assert.equal(input(value, "Oppskriftsnavn").value, "Mapped soup revised");
  const aiEditorSave = value.submitActions.at(-1);
  await aiEditorSave();
  const save = value.requests.find((request) => request[0] === "/recipes/mapped-recipe");
  assert.ok(save);
  assert.deepEqual(JSON.parse(JSON.stringify(save[2].ingredients[0])), {
    name: "Old unmatched phrase",
    quantity: "2.5",
    unit: "handfuls",
    category: "Other",
    source_indices: [0],
    canonical_product_id: null,
    canonical_unit_id: null,
    source_unit_expression: "Handfuls",
  });
  assert.equal(value.modal.open, false);
});
