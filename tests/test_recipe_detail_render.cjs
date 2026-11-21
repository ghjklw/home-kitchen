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
  replaceChildren(...children) { this.children = children.filter(Boolean); }
  setAttribute() {}
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

test("recipe detail renders Bokmål TM6 guided-cooking badges and ingredient references", () => {
  const context = {
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
  };
  vm.runInNewContext(
    ["formatGuidedStepDuration", "guidedStepBadges", "renderInstructionStepNode"].map(functionSource).join("\n"),
    context,
  );

  const li = context.renderInstructionStepNode(
    "Kok opp 15 min/100°C/revers/hastighet 1.",
    {
      step_index: 0,
      text: "Kok opp 15 min/100°C/revers/hastighet 1.",
      time_seconds: 900,
      temperature: "100°C",
      speed: "1",
      direction: "reverse",
      reverse: true,
      accessory: "Simmering basket",
      ingredient_references: ["300 g risottoris", "8 dl kraft"],
    },
  );
  assert.match(li.text, /Tid: 15 min/u);
  assert.match(li.text, /Temp: 100°C/u);
  assert.match(li.text, /Hastighet: 1/u);
  assert.match(li.text, /Retning: Revers/u);
  assert.match(li.text, /Tilbehør: Simmering basket/u);
  assert.match(li.text, /Ingredienser: 300 g risottoris, 8 dl kraft/u);
});

test("viewRecipe renders provenance note for AI adaptation of a Cookidoo recipe", () => {
  const dialog = new Node("dialog");
  const context = {
    openDialog: () => dialog,
    el: (tag, text = "", className = "") => new Node(tag, text, className),
    add: (parent, ...children) => { children.filter(Boolean).forEach((child) => parent.append(child)); return parent; },
    recipeImage: () => new Node("img"),
    formatRecipeBadge: () => "",
    formatRecipeSourceBadge: () => "KI-idé",
    renderRecipeStatsBadges: () => null,
    renderRecipeQuickFeedbackWidget: undefined,
    recipeOriginLabel: undefined,
    formatUiCount: String,
    ingredientQuantityText: (item) => item.name,
    purchaseEstimateText: () => "",
    renderInstructionStepNode: (step) => new Node("li", step),
    button: (text, action, className = "") => new Node("button", text, className),
    window: {},
  };
  vm.runInNewContext(
    ["recipeOriginLabel", "viewRecipe"].map(functionSource).join("\n"),
    context,
  );
  context.viewRecipe({
    id: "ai-1",
    title: "Vegetarisk risotto (KI-tilpasset)",
    source: "ai",
    servings: 4,
    provenance: {
      kind: "ai_adaptation",
      original_source: "cookidoo",
      source_url: "https://cookidoo.international/recipes/recipe/nb-NO/r-tm6",
    },
    source_url: "https://cookidoo.international/recipes/recipe/nb-NO/r-tm6",
    ingredients: [{ name: "risottoris" }],
    instructions: ["Kok på komfyr"],
    guided_steps: [],
  });
  assert.match(dialog.text, /Tilpasset fra en Cookidoo-oppskrift\./u);
});
