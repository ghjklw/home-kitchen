"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync("src/home_kitchen/static/app.js", "utf8");
function functionSource(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?\\n\\}`, "u"));
  assert.ok(match, `${name} is defined`);
  return match[0];
}

class Node {
  constructor(tag, text = "", className = "") {
    this.tagName = tag;
    this.textContent = text;
    this.className = className;
    this.children = [];
  }
  append(child) { this.children.push(child); }
  get text() { return this.textContent + " " + this.children.map((child) => child.text || "").join(" "); }
}

test("recipe detail renders a localized source label for imported drafts", () => {
  const dialog = new Node("dialog");
  const context = {
    openDialog: () => dialog,
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    recipeImage: () => new Node("img"),
    formatUiCount: String,
    window: {
      KitchenPlannedCooking: {
        formatPlannedIngredient: (item) => ({ text: item.name, accessibleLabel: item.name, copyText: item.name }),
        formatPlannedIngredientList: () => "",
      },
      KitchenShoppingView: { formatQuantityForDisplay: String },
    },
    ingredientQuantityText: String,
    purchaseEstimateText: () => "",
    button: (text, action, className = "") => {
      const node = new Node("button", text, className);
      node.action = action;
      return node;
    },
  };
  vm.runInNewContext(
    ["recipeOriginLabel", "plannedMealView"].map(functionSource).join("\n"),
    context,
  );
  context.plannedMealView({
    meal: { id: "meal-1" },
    servings: 2,
    sourceRecipe: {
      id: "recipe-1",
      title: "Tomatsuppe",
      source: "ai",
      servings: 2,
      provenance: { original_source: "imported" },
      ingredients: [],
      instructions: [],
    },
    ingredients: [],
  });
  assert.match(dialog.text, /Tilpasset fra en importert oppskrift\./u);
  assert.doesNotMatch(dialog.text, /Tilpasset fra imported/u);
});
