"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const state = {
  view: "week",
  household: {},
  recipes: [],
  meals: [],
  shopping: { lines: [], suggestions: [] },
  purchasePlan: { lines: [], suggestions: [] },
  inventory: [],
  storeTrip: null,
  oda: null,
  odaSearch: {},
  odaMedia: {},
  cartProposalLoading: false,
  cartConfirming: false,
  cartProposalHandled: false,
  cartProposalBlocked: false,
  shoppingPanel: "needs",
  basket: null,
  items: [],
  regular: [],
  integrations: {},
  week: localDate(new Date()),
};
let noticeTimer;
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function add(parent, ...children) {
  children.filter(Boolean).forEach((child) => parent.append(child));
  return parent;
}
function button(text, action, className = "") {
  const node = el("button", text, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function notify(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.className = error ? "error" : "";
  node.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(
    () => {
      node.hidden = true;
    },
    error ? 10000 : 4500,
  );
}
async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch("/api" + path, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new Error(
      "Could not reach your kitchen. Check your connection and try again. Your changes have not been saved.",
    );
  }
  if (!response.ok) {
    let data;
    let detail;
    try {
      data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : data.message;
    } catch {}
    const error = new Error(
      response.status === 401 || response.status === 403
        ? "Sign in through your household’s trusted access link, then try again."
        : response.status === 409 && path === "/meals"
          ? "That meal time is already occupied. Choose another slot, or remove the existing meal first. Your form is still here."
          : detail ||
          (response.status === 503
            ? path === "/ai/plan"
              ? "AI meal planning needs a provider configured by your household administrator."
              : "This service needs setup before it can be used."
            : response.status === 422 && path === "/ai/plan"
              ? "No recipes are available for meal planning. Add a recipe, then try again."
              : "Could not complete that request. Please try again."),
    );
    if (Number.isFinite(data?.retry_after_seconds))
      error.retryAfterSeconds = Math.max(0, data.retry_after_seconds);
    throw error;
  }
  return response.status === 204 ? null : response.json();
}
function localDate(value) {
  const date = new Date(value);
  date.setHours(12, 0, 0, 0);
  return date;
}
function iso(date) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}
function day(offset) {
  const date = new Date(state.week);
  date.setDate(date.getDate() + offset);
  return date;
}
function range() {
  return "?start=" + iso(day(0)) + "&end=" + iso(day(6));
}
function parseDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/u.test(value || "")) return null;
  const [year, month, date] = value.split("-").map(Number);
  const parsed = new Date(year, month - 1, date, 12);
  return iso(parsed) === value ? parsed : null;
}
function syncPlannerUrl() {
  const url = new URL(location.href);
  url.searchParams.set("date", iso(state.week));
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}
function dateLabel(date, options) {
  return date.toLocaleDateString(undefined, options);
}
function httpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}
function recipeImage(recipe, className = "recipe-image") {
  const src = httpsUrl(recipe.image_url);
  if (!src) return null;
  const frame = el("div", null, "recipe-image-frame");
  const image = el("img", null, className);
  image.src = src;
  image.alt = recipe.title + " photo";
  image.loading = "lazy";
  image.decoding = "async";
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => {
    frame.replaceChildren(el("span", "Image unavailable", "image-fallback"));
  });
  frame.append(image);
  return frame;
}
function title(kicker, heading, description, actions = []) {
  const block = el("header", null, "header");
  const text = el("div");
  add(
    text,
    el("p", kicker, "eyebrow"),
    el("h1", heading),
    el("p", description, "subtitle"),
  );
  add(block, text);
  if (actions.length) add(block, add(el("div", null, "actions"), ...actions));
  return block;
}
function empty(heading, description, action) {
  return add(
    el("div", null, "empty"),
    el("div", "✳", "empty-symbol"),
    el("h2", heading),
    el("p", description),
    action,
  );
}
function weekControls() {
  const row = el("div", null, "toolbar period-toolbar");
  const change = async (amount) => {
    state.week.setDate(state.week.getDate() + amount);
    syncPlannerUrl();
    await refreshView();
  };
  const previous = button("←", () => change(-7));
  const next = button("→", () => change(7));
  const jump = el("label", null, "date-jump");
  jump.classList.add("period-jump");
  const jumpInput = el("input");
  jumpInput.type = "date";
  jumpInput.value = iso(state.week);
  jumpInput.setAttribute("aria-label", "Jump to date");
  jump.append(el("span", "Jump to date"), jumpInput);
  jumpInput.addEventListener("change", async () => {
    const date = parseDate(jumpInput.value);
    if (!date) return;
    state.week = date;
    syncPlannerUrl();
    await refreshView();
  });
  add(
    row,
    previous,
    el(
      "span",
      dateLabel(day(0), { month: "short", day: "numeric" }) +
        " – " +
        dateLabel(day(6), { month: "short", day: "numeric", year: "numeric" }),
      "week-range",
    ),
    next,
    button(
      "Today",
      async () => {
        state.week = localDate(new Date());
        syncPlannerUrl();
        await refreshView();
      },
      "quiet small today-period",
    ),
    jump,
  );
  previous.ariaLabel = "Previous 7 days";
  next.ariaLabel = "Next 7 days";
  return row;
}
function openDialog(heading) {
  const modal = $("#modal");
  if (modal.open) modal.close();
  const root = $("#modal-content");
  root.replaceChildren();
  const titleNode = el("h2", heading);
  titleNode.id = "dialog-title";
  modal.setAttribute("aria-labelledby", "dialog-title");
  add(
    root,
    add(
      el("div", null, "dialog-header"),
      titleNode,
      button("×", () => modal.close(), "quiet"),
    ),
  );
  root.querySelector("button").ariaLabel = "Close dialog";
  modal.showModal();
  return root;
}
function field(label, type = "text", value = "", help = "") {
  const wrap = el("label", null, "field");
  const input = el(
    type === "textarea" ? "textarea" : type === "select" ? "select" : "input",
  );
  if (input.tagName === "INPUT") input.type = type;
  input.value = value ?? "";
  add(wrap, el("span", label), input, help ? el("small", help) : null);
  return { wrap, input };
}
function formSubmit(form, text, action) {
  const error = el("p", "", "form-error");
  error.setAttribute("role", "alert");
  const submit = el("button", text, "primary");
  submit.type = "submit";
  add(form, error, add(el("div", null, "actions"), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    error.textContent = "";
    try {
      await action();
    } catch (err) {
      error.textContent = err.message;
    } finally {
      submit.disabled = false;
    }
  });
}
async function mutate(path, method, body, message) {
  await api(path, method, body);
  $("#modal").close();
  await refreshView();
  if (message) notify(message);
}
async function refreshView() {
  try {
    if (state.view === "week") state.meals = await api("/meals" + range());
    if (state.view === "shopping") {
      const results = await Promise.all([
        api("/shopping" + range()),
        api("/purchase-plan" + range()),
        api("/items"),
        api("/regular-items"),
        api("/inventory"),
      ]);
      [
        state.shopping,
        state.purchasePlan,
        state.items,
        state.regular,
        state.inventory,
      ] = results;
      if (state.shoppingPanel === "oda") {
        [state.basket, state.oda] = await Promise.all([
          api("/basket" + range()),
          api("/oda/status"),
        ]);
      } else if (state.shoppingPanel === "in_store") {
        state.storeTrip = await api("/store-trip" + range());
      }
    }
    render();
  } catch (error) {
    notify(error.message, true);
  }
}
function navigate(view) {
  if (view === "basket") {
    state.shoppingPanel = "oda";
    view = "shopping";
  }
  state.view = view;
  location.hash = view;
  refreshView();
}
function render() {
  const main = $("#main");
  main.replaceChildren();
  document.querySelectorAll("[data-view]").forEach((node) => {
    if (node.dataset.view === state.view)
      node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  if (!navigator.onLine)
    add(
      main,
      el(
        "p",
        "You’re offline. Reconnect to load or save household changes.",
        "callout offline",
      ),
    );
  (
    ({
      week: renderWeek,
      recipes: renderRecipes,
      shopping: renderShopping,
      settings: renderSettings,
    })[state.view] || renderWeek
  )(main);
}
function renderWeek(main) {
  add(
    main,
    title(
      "Make room for good food",
      "Your week, on a plate.",
      "Plan breakfast, lunch and dinner for the next seven days.",
      [
        button("Suggest my week", suggestWeek),
        button("Plan a meal", () => mealForm(), "primary"),
      ],
    ),
    weekControls(),
  );
  const grid = el("div", null, "week-grid");
  for (let i = 0; i < 7; i++) {
    const date = day(i);
    const meals = state.meals.filter((meal) => meal.date === iso(date));
    const card = el(
      "section",
      null,
      "day" + (iso(date) === iso(new Date()) ? " today" : ""),
    );
    add(
      card,
      el("h3", dateLabel(date, { weekday: "long" })),
      el(
        "p",
        dateLabel(date, { month: "short", day: "numeric" }) +
          (iso(date) === iso(new Date()) ? " · Today" : ""),
        "date",
      ),
    );
    const occupiedSlots = new Set(meals.map((meal) => meal.slot || "dinner"));
    ["breakfast", "lunch", "dinner"].forEach((slot) => {
      const slotSection = el("section", null, "meal-slot");
      const slotMeals = meals.filter((meal) => (meal.slot || "dinner") === slot);
      if (slotMeals.length) {
        add(slotSection, el("span", slot, "eyebrow"));
        slotMeals.forEach((meal) => {
          const recipe = state.recipes.find((item) => item.id === meal.recipe_id);
          add(
            slotSection,
            el("h3", recipe?.title || "Recipe unavailable", "meal-title"),
            el("p", meal.servings + " servings", "muted"),
            add(
              el("div", null, "meal-actions"),
              recipe
                ? button("View recipe", () => viewRecipe(recipe), "small")
                : null,
              button(
                "Cook",
                () => openPlannedMeal(meal),
                "small primary",
              ),
              button(
                "Remove",
                async () => {
                  try {
                    await mutate(
                      "/meals/" + encodeURIComponent(meal.id),
                      "DELETE",
                      undefined,
                      "Meal removed.",
                    );
                  } catch (err) {
                    notify(err.message, true);
                  }
                },
                "small quiet",
              ),
            ),
          );
        });
      }
      if (slotMeals.length) add(card, slotSection);
    });
    const openSlots = ["breakfast", "lunch", "dinner"].filter(
      (slot) => !occupiedSlots.has(slot),
    );
    if (openSlots.length) {
      add(
        card,
        el(
          "p",
          meals.length ? "Open: " + openSlots.join(" · ") : "Nothing planned yet",
          "open-slots",
        ),
        button(
          "+ Add meal",
          () => mealForm(iso(date), undefined, openSlots[0]),
          "quiet small",
        ),
      );
    }
    add(grid, card);
  }
  add(main, grid);
}
async function suggestWeek() {
  const root = openDialog("A week to review");
  add(
    root,
    el(
      "p",
      "Asking AI for dinner ideas. Nothing will be added until you save each meal.",
      "muted",
    ),
  );
  const result = el("div", null, "loading");
  result.setAttribute("aria-live", "polite");
  result.textContent = "Putting together your suggestions…";
  add(root, result);
  try {
    const draft = await api("/ai/plan", "POST", {
      start: iso(day(0)),
      days: 7,
    });
    result.className = "";
    result.replaceChildren();
    if (!Array.isArray(draft.meals) || !draft.meals.length) {
      add(
        result,
        el(
          "p",
          "No meal suggestions came back. Try again later or plan a dinner yourself.",
          "muted",
        ),
      );
      return;
    }
    if (Array.isArray(draft.notes) && draft.notes.length) {
      const notes = add(
        el("section", null, "callout"),
        el("strong", "Notes from the planner"),
      );
      const list = el("ul");
      draft.notes.forEach((note) => add(list, el("li", String(note))));
      add(notes, list);
      add(result, notes);
    }
    const list = el("div", null, "draft-meals");
    draft.meals.forEach((meal, index) => {
      const recipe = state.recipes.find((item) => item.id === meal.recipe_id);
      const date = new Date(meal.date + "T12:00:00");
      const row = el("article", null, "draft-meal");
      const info = el("div");
      add(
        info,
        el("h3", recipe?.title || "Recipe unavailable", "meal-title"),
        el(
          "p",
          (Number.isNaN(date.valueOf())
            ? meal.date
            : dateLabel(date, {
                weekday: "long",
                month: "short",
                day: "numeric",
              })) +
            " · " +
            (meal.slot || "dinner") +
            " · " +
            meal.servings +
            " servings",
          "muted",
        ),
      );
      const status = el("p", "", "draft-status");
      status.setAttribute("role", "status");
      const save = button("Add to my week", async () => {
        save.disabled = true;
        status.textContent = "Saving…";
        try {
          await api("/meals", "POST", meal);
          status.textContent = "Added to your week.";
          save.textContent = "Added";
          save.className = "small";
          await refreshView();
        } catch (error) {
          save.disabled = false;
          status.textContent = error.message + " You can retry this meal.";
        }
      });
      save.classList.add("small", "primary");
      if (!recipe) {
        save.disabled = true;
        status.textContent =
          "This recipe is no longer in your collection, so it cannot be added.";
      }
      add(row, info, save, status);
      list.append(row);
    });
    add(result, list);
  } catch (error) {
    result.className = "";
    result.replaceChildren();
    add(result, el("p", error.message, "form-error"));
    if (/setup|configured/i.test(error.message))
      add(
        result,
        el(
          "p",
          "Ask your household administrator to configure the AI provider, or add a dinner yourself.",
          "muted",
        ),
      );
    else if (/recipe/i.test(error.message))
      add(
        result,
        el(
          "p",
          "Add at least one recipe to your collection, then try again.",
          "muted",
        ),
      );
    add(
      result,
      button("Close", () => $("#modal").close(), "quiet"),
    );
  }
}
function mealForm(date = iso(day(0)), recipeId, slot = "dinner") {
  if (!state.recipes.length) {
    const root = openDialog("Start with a recipe");
    add(
      root,
      el("p", "Add your first recipe, then choose a day for it.", "muted"),
      button("Add a recipe", () => recipeForm(), "primary"),
    );
    return;
  }
  const root = openDialog("Plan a meal");
  const form = el("form");
  const chosenDate = field("Day", "date", date);
  chosenDate.input.required = true;
  const recipe = field("Recipe", "select");
  state.recipes.forEach((item) => {
    const option = el("option", item.title);
    option.value = item.id;
    add(recipe.input, option);
  });
  if (recipeId) recipe.input.value = recipeId;
  const mealSlot = field("Meal time", "select");
  ["breakfast", "lunch", "dinner"].forEach((value) => {
    const option = el("option", value[0].toUpperCase() + value.slice(1));
    option.value = value;
    add(mealSlot.input, option);
  });
  mealSlot.input.value = slot;
  const servings = field(
    "Servings",
    "number",
    state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  add(form, chosenDate.wrap, mealSlot.wrap, recipe.wrap, servings.wrap);
  formSubmit(form, "Add to week", async () => {
    const mealDate = chosenDate.input.value;
    await api("/meals", "POST", {
      date: mealDate,
      slot: mealSlot.input.value,
      recipe_id: recipe.input.value,
      servings: Number(servings.input.value),
    });
    state.week = parseDate(mealDate) || localDate(new Date());
    syncPlannerUrl();
    $("#modal").close();
    state.view = "week";
    location.hash = "week";
    await refreshView();
    notify("Meal planned.");
  });
  add(root, form);
}
function renderRecipes(main) {
  add(
    main,
    title(
      "Your own collection",
      "Recipes worth keeping.",
      "Everyday favourites, new discoveries, and your next good dinner.",
      [
        button("Find recipes", discoveryForm),
        button("Import link", importForm),
        button("+ Add recipe", () => recipeForm(), "primary"),
      ],
    ),
  );
  const tools = el("div", null, "toolbar");
  const search = el("input", null, "search");
  search.type = "search";
  search.placeholder = "Find a recipe…";
  search.ariaLabel = "Find a recipe";
  add(tools, search);
  add(main, tools);
  const results = el("div");
  const draw = () => {
    results.replaceChildren();
    const matches = state.recipes.filter((recipe) =>
      recipe.title.toLowerCase().includes(search.value.toLowerCase()),
    );
    if (!state.recipes.length)
      add(
        results,
        empty(
          "Your recipe book starts here",
          "Add a recipe you love or import one from a link. Your collection is yours to build.",
          button("Add your first recipe", () => recipeForm(), "primary"),
        ),
      );
    else if (!matches.length)
      add(results, empty("No recipes found", "Try another recipe name."));
    else {
      const grid = el("div", null, "grid");
      matches.forEach((recipe) =>
        add(
          grid,
          add(
            el("article", null, "card"),
            recipeImage(recipe),
            el(
              "span",
              recipe.source === "ai"
                ? "AI idea"
                : recipe.source === "imported"
                  ? "From a recipe link"
                  : "From your kitchen",
              "badge",
            ),
            el("h3", recipe.title),
            el(
              "p",
              recipe.servings +
                " servings · " +
                recipe.ingredients.length +
                " ingredients",
              "muted",
            ),
            add(
              el("div", null, "actions"),
              button("View recipe", () => viewRecipe(recipe)),
              button("Plan", () => mealForm(undefined, recipe.id), "quiet"),
            ),
          ),
        ),
      );
      add(results, grid);
    }
  };
  search.addEventListener("input", draw);
  draw();
  add(main, results);
}
function recipeForm(draft = {}, source = "manual") {
  const isEditing = Boolean(draft.id);
  const root = openDialog(
    isEditing ? "Edit recipe" : draft.title ? "Review your recipe" : "Add a recipe",
  );
  if (source !== "manual")
    add(
      root,
      el(
        "p",
        "Check the ingredients, quantities and instructions before saving.",
        "callout",
      ),
    );
  const form = el("form");
  const name = field("Recipe name", "text", draft.title);
  name.input.required = true;
  const servings = field(
    "Recipe makes",
    "number",
    draft.servings || state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  if (
    !isEditing &&
    (source === "imported" || draft.provenance?.original_source === "imported") &&
    draft.servings != null
  )
    add(
      form,
      el(
        "p",
        "Source recipe servings: " + draft.servings + ". The value below is what Kitchen will save.",
        "muted",
      ),
    );
  const imageUrl = field(
    "Recipe photo URL (optional)",
    "url",
    draft.image_url || "",
    "Use an HTTPS image URL. No photo is added unless you provide one.",
  );
  imageUrl.input.placeholder = "https://…";
  const ingredientEditor = el("fieldset", null, "ingredient-editor");
  add(ingredientEditor, el("legend", "Ingredients"));
  add(
    ingredientEditor,
    el(
      "p",
      "Review source lines. Use positive quantities with a decimal point or comma and explicit units before saving.",
      "muted",
    ),
  );
  if (draft.source_url) {
    const original = el("a", "Open original recipe", "source-link");
    original.href = draft.source_url;
    original.target = "_blank";
    original.rel = "noopener noreferrer";
    add(ingredientEditor, original);
  }
  const ingredientRows = el("div", null, "ingredient-rows");
  const sourceIngredients = Array.isArray(draft.source_ingredients)
    ? draft.source_ingredients
    : source === "imported" && Array.isArray(draft.ingredients)
      ? draft.ingredients.map((text, index) => ({ index, text }))
      : [];
  let rowControllers = (Array.isArray(draft.ingredients) ? draft.ingredients : []).map(
    (item, index) => {
      const row = window.KitchenRecipeEditor.rowFromDraft(item, index);
      if (!row.sourceText && row.source_indices.length)
        row.sourceText = row.source_indices
          .map((sourceIndex) => sourceIngredients.find((entry) => entry.index === sourceIndex)?.text)
          .filter(Boolean)
          .join(" · ");
      return row;
    },
  );
  if (!rowControllers.length) rowControllers.push(window.KitchenRecipeEditor.rowFromDraft({}));

  function updateIngredientRowLabels() {
    rowControllers.forEach((row, index) => {
      row.legend.textContent = "Ingredient " + (index + 1);
      row.remove.setAttribute("aria-label", "Remove ingredient " + (index + 1));
    });
  }

  function createIngredientRow(values) {
    const row = el("fieldset", null, "ingredient-row");
    const legend = el("legend");
    const sourceText = values.sourceText
      ? el("p", "Source text: " + values.sourceText, "ingredient-source")
      : null;
    const fields = el("div", null, "ingredient-fields");
    const ingredientName = field("Name", "text", values.name);
    const quantity = field("Quantity", "text", values.quantity);
    quantity.input.inputMode = "decimal";
    const unit = field("Unit", "text", values.unit);
    const category = field("Category", "text", values.category || "Other");
    const rowError = el("p", "", "ingredient-error");
    rowError.setAttribute("role", "alert");
    const remove = button("Remove", () => {
      const index = rowControllers.indexOf(controller);
      if (index < 0) return;
      rowControllers.splice(index, 1);
      row.remove();
      updateIngredientRowLabels();
    }, "quiet small");
    const controller = {
      row,
      legend,
      remove,
      name: ingredientName.input,
      quantity: quantity.input,
      unit: unit.input,
      source_indices: [...(values.source_indices || [])],
      category: category.input,
      error: rowError,
    };
    add(fields, ingredientName.wrap, quantity.wrap, unit.wrap, category.wrap);
    add(row, legend, sourceText, fields, rowError, remove);
    return controller;
  }

  rowControllers = rowControllers.map(createIngredientRow);
  rowControllers.forEach((row) => ingredientRows.append(row.row));
  updateIngredientRowLabels();
  const addIngredient = button("+ Add ingredient", () => {
    const row = createIngredientRow(window.KitchenRecipeEditor.rowFromDraft({}));
    rowControllers.push(row);
    ingredientRows.append(row.row);
    updateIngredientRowLabels();
    row.name.focus();
  }, "quiet");
  add(ingredientEditor, ingredientRows, addIngredient);
  const instructions = field(
    "Instructions",
    "textarea",
    (draft.instructions || []).join("\n"),
    "One step per line.",
  );
  instructions.input.required = true;
  add(
    form,
    name.wrap,
    servings.wrap,
    imageUrl.wrap,
    ingredientEditor,
    instructions.wrap,
  );
  formSubmit(form, "Save recipe", async () => {
    const photo = imageUrl.input.value.trim();
    if (photo && !httpsUrl(photo))
      throw new Error("Recipe photo URL must use HTTPS.");
    if (!rowControllers.length) throw new Error("Add at least one ingredient before saving.");
    let hasIngredientErrors = false;
    const rows = rowControllers.map((row) => {
      const item = {
        name: row.name.value.trim(),
        quantity: window.KitchenRecipeEditor.normalizeQuantity(row.quantity.value),
        unit: row.unit.value.trim(),
        category: row.category.value.trim() || "Other",
        source_indices: [...row.source_indices],
      };
      const errors = [];
      const fieldErrors = {
        name: !item.name,
        quantity: !window.KitchenRecipeEditor.isPositiveQuantity(item.quantity),
        unit: !item.unit,
      };
      if (fieldErrors.name) errors.push("Add an ingredient name.");
      if (fieldErrors.quantity) {
        errors.push("Enter an exact positive quantity using a decimal point or comma; ambiguous source text needs your review.");
      }
      if (fieldErrors.unit) errors.push("Enter the ingredient unit explicitly.");
      row.error.textContent = errors.join(" ");
      row.name.setAttribute("aria-invalid", String(fieldErrors.name));
      row.quantity.setAttribute("aria-invalid", String(fieldErrors.quantity));
      row.unit.setAttribute("aria-invalid", String(fieldErrors.unit));
      hasIngredientErrors ||= errors.length > 0;
      return item;
    });
    if (hasIngredientErrors)
      throw new Error("Fix the ingredient fields marked with an error before saving.");
    const payload = {
      title: name.input.value.trim(),
      servings: Number(servings.input.value),
      image_url: photo || null,
      ingredients: rows,
      instructions: instructions.input.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
      source,
      source_url: draft.source_url || null,
      source_ingredients: sourceIngredients,
      provenance: draft.provenance || null,
    };
    await api(
      isEditing ? "/recipes/" + encodeURIComponent(draft.id) : "/recipes",
      isEditing ? "PUT" : "POST",
      payload,
    );
    state.recipes = await api("/recipes");
    $("#modal").close();
    navigate("recipes");
    notify(isEditing ? "Recipe changes saved." : "Recipe added to your collection.");
  });
  add(root, form);
  if (state.integrations.ai?.configured && (isEditing || source === "ai")) {
    const revision = el("details", null, "recipe-ai-revision");
    add(
      revision,
      el("summary", "Revise with AI"),
      el(
        "p",
        isEditing
          ? "AI proposes a draft. Your saved recipe changes only when you choose Save."
          : "AI proposes a draft. Review it and choose Save recipe when ready.",
        "muted",
      ),
    );
    const prompt = field(
      "What should change?",
      "textarea",
      "",
      "For example: make it vegetarian, faster, or suitable for more servings.",
    );
    prompt.input.required = true;
    prompt.input.maxLength = 1000;
    prompt.input.rows = 3;
    const revisionError = el("p", "", "form-error");
    revisionError.setAttribute("role", "alert");
    const revise = button("Review AI revision", async () => {
      revise.disabled = true;
      revisionError.textContent = "";
      try {
        const payload = window.KitchenRecipeEditor.buildRevisionRequest(
          {
            title: name.input.value,
            servings: servings.input.value,
            ingredients: rowControllers.map((row) => ({
              name: row.name.value,
              quantity: row.quantity.value,
              unit: row.unit.value,
              category: row.category.value,
              source_indices: row.source_indices,
            })),
            instructions: instructions.input.value,
            source_url: draft.source_url || null,
            image_url: imageUrl.input.value,
            source_ingredients: sourceIngredients,
            provenance: draft.provenance || null,
          },
          { isEditing, source },
          prompt.input.value,
        );
        const response = await api("/recipe-drafts/ai", "POST", payload);
        if (!response?.adapted)
          throw new Error("AI did not return an editable draft. Your recipe is unchanged.");
        const revisedDraft = isEditing
          ? { ...response.adapted, id: draft.id }
          : response.adapted;
        recipeForm(revisedDraft, isEditing ? source : "ai");
      } catch (error) {
        revisionError.textContent = error.message;
      } finally {
        if (revise.isConnected) revise.disabled = false;
      }
    }, "primary");
    add(revision, prompt.wrap, revisionError, revise);
    add(root, revision);
  }
}
function viewRecipe(recipe) {
  const root = openDialog(recipe.title);
  const body = el("div", null, "recipe-body");
  add(
    body,
    recipeImage(recipe),
    el("p", recipe.servings + " servings", "muted"),
    el("h3", "Ingredients"),
  );
  const ingredients = el("ul");
  recipe.ingredients.forEach((item) =>
    add(
      ingredients,
      el("li", item.quantity + " " + item.unit + " " + item.name),
    ),
  );
  const steps = el("ol");
  recipe.instructions.forEach((step) => add(steps, el("li", step)));
  add(body, ingredients, el("h3", "Method"), steps);
  if (recipe.source_url) {
    try {
      const url = new URL(recipe.source_url);
      if (["https:", "http:"].includes(url.protocol)) {
        const link = el("a", "Original recipe", "source-link");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        add(body, link);
      }
    } catch {}
  }
  add(
    body,
    add(
      el("div", null, "actions"),
      button("Plan a meal", () => mealForm(undefined, recipe.id), "primary"),
      button("Edit recipe", () => recipeForm(recipe, recipe.source || "manual"), "quiet"),
      button("Rate this recipe", () => feedbackForm(recipe), "quiet"),
      button(
        "Delete recipe",
        () => {
          const confirmRoot = openDialog("Delete this recipe?");
          add(
            confirmRoot,
            el(
              "p",
              "This removes “" +
                recipe.title +
                "” from your collection. Meals using it may also need updating.",
              "muted",
            ),
            button("Keep recipe", () => viewRecipe(recipe)),
            button(
              "Delete recipe",
              async () => {
                try {
                  await api(
                    "/recipes/" + encodeURIComponent(recipe.id),
                    "DELETE",
                  );
                  state.recipes = await api("/recipes");
                  $("#modal").close();
                  await refreshView();
                  notify("Recipe deleted.");
                } catch (error) {
                  notify(error.message, true);
                }
              },
              "danger",
            ),
          );
        },
        "quiet danger",
      ),
    ),
  );
  add(root, body);
}
function importForm() {
  const root = openDialog("Bring a recipe along");
  add(
    root,
    el(
      "p",
      "Paste a supported recipe link. AI will shape it into an editable draft for review.",
      "muted",
    ),
  );
  const form = el("form");
  const url = field("Recipe link", "url");
  url.input.required = true;
  url.input.placeholder = "https://…";
  add(form, url.wrap);
  formSubmit(form, "Shape URL for review", async () => {
    const response = await api("/recipe-drafts/ai", "POST", { url: url.input.value });
    if (!response?.adapted)
      throw new Error("AI did not return an editable recipe draft. No recipe was saved.");
    recipeForm(response.adapted, "ai");
  });
  add(root, form);
}
function discoveryForm() {
  const root = openDialog("Find a recipe from a source");
  add(
    root,
    el(
      "p",
      "Search Oda, Marmiton and Matprat. Choose a result to shape its exact source link into a draft for review.",
      "muted",
    ),
  );
  const form = el("form");
  const query = field(
    "Dish or ingredients",
    "search",
    "",
    "For example: salmon, dill and potatoes.",
  );
  query.input.required = true;
  query.input.maxLength = 200;
  const sources = el("fieldset", null, "discovery-source-picker");
  add(sources, el("legend", "Search sources"));
  const selected = new Set(
    window.KitchenRecipeDiscovery.SOURCES.map((source) => source.id),
  );
  window.KitchenRecipeDiscovery.SOURCES.forEach((source) => {
    const label = el("label", null, "discovery-source-option");
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.value = source.id;
    checkbox.checked = true;
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selected.add(source.id);
      else selected.delete(source.id);
    });
    add(label, checkbox, el("span", source.label));
    sources.append(label);
  });
  const content = el("div", null, "discovery-content");
  add(form, query.wrap, sources);
  formSubmit(form, "Search sources", async () => {
    const request = window.KitchenRecipeDiscovery.buildRequest(
      query.input.value,
      [...selected],
    );
    const response = await api("/recipe-discovery", "POST", request);
    const view = window.KitchenRecipeDiscovery.normalizeResponse(response);
    const nextContent = el("div", null, "discovery-content");
    if (view.sources.length) {
      const statuses = el("section", null, "discovery-statuses");
      statuses.setAttribute("aria-live", "polite");
      add(statuses, el("h3", "Source status"));
      const list = el("ul", null, "discovery-status-list");
      view.sources.forEach((source) => {
        const line = el("li");
        add(line, el("strong", source.label + ": "), el("span", source.status));
        if (source.detail) add(line, el("span", " — " + source.detail));
        if (source.status === "Search completed")
          add(
            line,
            el(
              "span",
              source.resultCount
                ? " · " + source.resultCount + " found"
                : " · No matches",
            ),
          );
        if (source.indexUrl) {
          const link = el("a", "Browse source");
          link.href = source.indexUrl;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          add(line, link);
        }
        list.append(line);
      });
      add(statuses, list);
      nextContent.append(statuses);
    }
    if (!view.results.length) {
      add(
        nextContent,
        empty("No recipes found", "See each source’s status above for search availability."),
      );
    } else {
      const grid = el("div", null, "grid discovery-results");
      view.results.forEach((result) => {
        const card = el("article", null, "card discovery-card");
        if (result.imageUrl)
          add(
            card,
            recipeImage({ title: result.title, image_url: result.imageUrl }),
          );
        add(card, el("span", result.sourceLabel, "badge"), el("h3", result.title));
        if (result.sourceAttribution) add(card, el("p", result.sourceAttribution, "muted"));
        const facts = [result.duration, result.difficulty].filter(Boolean).join(" · ");
        if (facts) add(card, el("p", facts, "muted"));
        if (result.detail) add(card, el("p", result.detail, "muted"));
        const actions = el("div", null, "actions");
        if (result.url) {
          const sourceLink = el("a", "View source");
          sourceLink.href = result.url;
          sourceLink.target = "_blank";
          sourceLink.rel = "noopener noreferrer";
          add(actions, sourceLink);
        }
        if (result.importable) {
          const error = el("p", "", "form-error discovery-import-error");
          error.setAttribute("role", "alert");
          const importButton = button(
            "Import for review",
            async () => {
              importButton.disabled = true;
              error.textContent = "";
              try {
                const draft = await api("/recipe-drafts/ai", "POST", {
                  url: result.url,
                });
                if (!draft?.adapted)
                  throw new Error(
                    "AI did not return an editable draft. Nothing was saved.",
                  );
                if (!importButton.isConnected) return;
                recipeForm(draft.adapted, "ai");
              } catch (err) {
                error.textContent = err.message;
              } finally {
                if (importButton.isConnected) importButton.disabled = false;
              }
            },
            "primary small",
          );
          add(actions, importButton);
          add(card, actions, error);
        } else {
          add(actions, el("span", "This result cannot be imported yet.", "muted"));
          add(card, actions);
        }
        grid.append(card);
      });
      nextContent.append(grid);
    }
    content.replaceChildren(...nextContent.childNodes);
  });
  add(root, form, content);
}
async function openPlannedMeal(meal) {
  try {
    const response = await api("/meals/" + encodeURIComponent(meal.id) + "/recipe");
    plannedMealView(window.KitchenPlannedCooking.buildPlannedCookingView(response));
  } catch (error) {
    notify(error.message, true);
  }
}
function plannedMealView(view) {
  const recipe = view.sourceRecipe;
  const root = openDialog(recipe.title);
  const body = el("div", null, "recipe-body planned-cooking-view");
  add(
    body,
    recipeImage(recipe),
    el(
      "p",
      "For " + view.servings + " servings · saved recipe makes " + recipe.servings,
      "callout",
    ),
    el("h3", "Ingredients"),
  );
  const ingredients = el("ul");
  view.ingredients.forEach((item) => {
    const exact = String(item.quantity);
    const row = el(
      "li",
      window.KitchenShoppingView.formatQuantityForDisplay(exact) +
        " " +
        item.unit +
        " " +
        item.name,
    );
    row.setAttribute("aria-label", exact + " " + item.unit + " " + item.name);
    add(ingredients, row);
  });
  const provenance = recipe.provenance;
  if (provenance?.original_source)
    add(body, el("p", "Adapted from " + provenance.original_source + ".", "muted"));
  if (recipe.source_url) {
    const url = window.KitchenRecipeDiscovery.safeHttpsUrl(recipe.source_url);
    if (url) {
      const link = el("a", "Original recipe", "source-link");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      add(body, link);
    }
  }
  const steps = el("ol");
  recipe.instructions.forEach((step) => add(steps, el("li", step)));
  add(body, ingredients, el("h3", "Method"), steps);
  add(body, button("View saved recipe amounts", () => viewRecipe(recipe), "quiet"));
  add(root, body);
}
function shoppingOrigins(origins = []) {
  const labels = new Set();
  origins.forEach((origin) => {
    if (origin.startsWith("meal:")) labels.add("Meal plan");
    else if (origin.startsWith("item:")) labels.add("Quick addition");
    else if (origin.startsWith("regular:")) labels.add("Regular");
  });
  return [...labels].join(", ");
}
function shoppingSections() {
  const nav = el("div", null, "section-tabs");
  [
    ["needs", "Needs"],
    ["oda", "Oda"],
    ["in_store", "In-store"],
    ["pantry", "At home"],
  ].forEach(([key, label]) => {
    const tab = button(
      label,
      async () => {
        state.shoppingPanel = key;
        await refreshView();
      },
      state.shoppingPanel === key ? "primary" : "quiet",
    );
    if (state.shoppingPanel === key) tab.setAttribute("aria-current", "page");
    nav.append(tab);
  });
  return nav;
}
function renderInStore(main) {
  const trip = state.storeTrip || {
    lines: [],
    checked_count: 0,
    total_count: 0,
  };
  const remaining = trip.lines.filter(
    window.KitchenShoppingShare.isRemainingShoppingLine,
  );
  const rangeTitle =
    dateLabel(day(0), { month: "short", day: "numeric" }) +
    " – " +
    dateLabel(day(6), { month: "short", day: "numeric", year: "numeric" });
  const shareTitle = "Kitchen shopping list · " + rangeTitle;
  const sharedText = () =>
    window.KitchenShoppingShare.formatRemainingShoppingText(
      shareTitle,
      remaining,
      state.household.shopping_category_order || [],
    );
  const copyDialog = (text, explanation) => {
    const root = openDialog("Copy shopping list");
    add(root, el("p", explanation, "muted"));
    const content = el("textarea");
    content.setAttribute("aria-label", "Remaining shopping list text");
    content.readOnly = true;
    content.rows = Math.min(16, Math.max(6, text.split("\n").length));
    content.value = text;
    add(root, content);
    content.focus();
    content.select();
  };
  const copyRemaining = async (forShare = false) => {
    const text = sharedText();
    if (typeof navigator.clipboard?.writeText !== "function") {
      copyDialog(
        text,
        forShare
          ? "Sharing and clipboard access are unavailable here. Select the text and copy it into another app."
          : "Clipboard access is unavailable here. Select the text and copy it into another app.",
      );
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      notify(
        forShare
          ? "Share sheet unavailable; shopping list copied. Paste it into the app you choose."
          : "Remaining shopping list copied.",
      );
    } catch {
      copyDialog(
        text,
        forShare
          ? "Sharing and clipboard access are unavailable here. Select the text and copy it into another app."
          : "Clipboard access was blocked. Select the text and copy it into another app.",
      );
    }
  };
  const shareRemaining = async () => {
    const text = sharedText();
    if (typeof navigator.share !== "function") {
      await copyRemaining(true);
      return;
    }
    try {
      await navigator.share({ title: shareTitle, text });
      notify("Shopping list shared.");
    } catch (error) {
      if (error?.name === "AbortError") return;
      notify("Could not open the share sheet. Use Copy remaining list instead.", true);
    }
  };
  const share = button("Share remaining list", shareRemaining, "quiet");
  const copy = button("Copy remaining list", () => copyRemaining(), "quiet");
  share.disabled = copy.disabled = remaining.length === 0;
  const progressData = window.KitchenShoppingView.pickupProgress(
    trip.checked_count,
    trip.total_count,
  );
  const progress = el("progress");
  progress.max = 100;
  progress.value = progressData.percent;
  progress.setAttribute("aria-label", "Trip pickup progress");
  const progressSummary = add(
    el("div", null, "trip-progress"),
    el("strong", progressData.label),
    progress,
  );
  add(
    main,
    add(el("div", null, "trip-toolbar"), progressSummary, add(el("div", null, "actions shopping-share-actions"), share, copy)),
  );
  const pickupInfo = el("details", null, "shopping-info");
  add(
    pickupInfo,
    el("summary", "About pickup tracking"),
    el(
      "p",
      "Checking an item off means you picked it up for this trip. It does not record a purchase or change at-home quantities.",
    ),
  );
  if (!trip.lines.length) {
    add(
      main,
      empty(
        "No in-store needs",
        "Add meals or shopping needs first. Only quantities still needed after your at-home inventory appear here.",
        button(
          "Review shopping needs",
          () => {
            state.shoppingPanel = "needs";
            render();
          },
          "quiet",
        ),
      ),
    );
    add(main, pickupInfo);
    return;
  }
  const categoryOrder = state.household.shopping_category_order || [];
  window.KitchenShoppingView.groupedByCategory(trip.lines, categoryOrder).forEach((lines, category) => {
    const section = add(
      el("section", null, "panel category-section"),
      el("h2", category),
    );
    const list = el("ul", null, "list");
    lines.forEach((line) => {
      const row = el("li", null, line.checked ? "completed" : "");
      const check = el("input");
      check.type = "checkbox";
      check.checked = Boolean(line.checked);
      check.setAttribute(
        "aria-label",
        (check.checked ? "Uncheck " : "Mark picked up: ") + line.name,
      );
      check.addEventListener("change", async () => {
        check.disabled = true;
        try {
          await api(
            "/store-trip/items/" + encodeURIComponent(line.id) + range(),
            "PUT",
            { checked: check.checked },
          );
          await refreshView();
        } catch (error) {
          check.checked = Boolean(line.checked);
          notify(error.message, true);
        } finally {
          check.disabled = false;
        }
      });
      const detail = add(el("div", null, "detail"), el("strong", line.name));
      const exactQuantity = String(line.to_buy_quantity ?? "0");
      const unit = line.unit || "";
      const quantity = el(
        "small",
        window.KitchenShoppingView.formatQuantityForDisplay(exactQuantity) +
          (unit ? " " + unit : "") +
          " to pick up" +
          (line.origins?.length ? " · " + shoppingOrigins(line.origins) : ""),
      );
      quantity.setAttribute(
        "aria-label",
        "Exact amount: " + exactQuantity + (unit ? " " + unit : "") + " to pick up",
      );
      quantity.title = "Exact amount: " + exactQuantity + (unit ? " " + unit : "");
      add(
        detail,
        quantity,
      );
      add(row, check, detail);
      list.append(row);
    });
    add(section, list);
    main.append(section);
  });
  add(main, pickupInfo);
}
function inventoryForm(item = {}) {
  const root = openDialog(
    item.id ? "Update what you have" : "Add to at-home inventory",
  );
  const form = el("form");
  const name = field("Item name", "text", item.name || "");
  name.input.required = true;
  if (item.id) name.input.readOnly = true;
  const quantity = field("Quantity on hand", "number", item.quantity ?? "0");
  quantity.input.min = "0";
  quantity.input.step = "any";
  quantity.input.required = true;
  const unit = field("Unit", "text", item.unit || "item");
  unit.input.required = true;
  if (item.id) unit.input.readOnly = true;
  const category = field(
    "Category",
    "text",
    item.category || "Other",
    "Used to organize the in-store list.",
  );
  category.input.maxLength = 60;
  const note = field("Note (optional)", "text", item.note || "");
  note.input.maxLength = 500;
  add(
    form,
    name.wrap,
    add(el("div", null, "form-grid"), quantity.wrap, unit.wrap),
    category.wrap,
    note.wrap,
  );
  formSubmit(form, item.id ? "Save quantity" : "Add at home", async () => {
    const payload = {
      name: name.input.value.trim(),
      quantity: quantity.input.value,
      unit: unit.input.value.trim(),
      category: category.input.value.trim() || "Other",
      note: note.input.value.trim() || null,
    };
    await api("/inventory", "PUT", payload);
    state.inventory = await api("/inventory");
    $("#modal").close();
    render();
    notify("At-home quantity saved.");
  });
  add(root, form);
}
function renderInventory(main) {
  add(
    main,
    el(
      "p",
      "Keep a simple quantity on hand. Kitchen subtracts it from shopping needs; checking off an in-store item does not change this inventory.",
      "muted",
    ),
  );
  if (!state.inventory.length) {
    add(
      main,
      empty(
        "Nothing in your at-home list yet",
        "Add what you already have so the shopping list can show the remaining quantity.",
      ),
    );
    return;
  }
  const list = el("ul", null, "list");
  state.inventory.forEach((item) => {
    const row = add(
      el("li"),
      add(
        el("div", null, "detail"),
        el("strong", item.name),
        el(
          "small",
          item.quantity +
            " " +
            item.unit +
            " · " +
            (item.category || "Other") +
            (item.note ? " · " + item.note : ""),
        ),
      ),
      button("Edit", () => inventoryForm(item), "quiet small"),
      button(
        "Remove",
        async () => {
          try {
            await api("/inventory/" + encodeURIComponent(item.id), "DELETE");
            await refreshView();
            notify("At-home item removed.");
          } catch (error) {
            notify(error.message, true);
          }
        },
        "quiet small",
      ),
    );
    list.append(row);
  });
  add(main, list);
}
function renderShopping(main) {
  const inStore = state.shoppingPanel === "in_store";
  const isOda = state.shoppingPanel === "oda";
  const isPantry = state.shoppingPanel === "pantry";
  const heading = title(
    "Shopping",
    inStore
      ? "In-store trip"
      : isOda
        ? "Oda products"
        : isPantry
          ? "At home"
          : "The shopping list.",
    inStore
      ? "Pick up what remains for this period."
      : isOda
        ? "Review product choices for this period."
        : isPantry
          ? "Quantities here reduce what remains to buy."
          : "Your list combines planned meals, everyday items, and anything you add.",
    inStore || isOda
      ? []
      : [
          isPantry
            ? button("+ Add at-home item", () => inventoryForm(), "primary")
            : button("+ Add shopping need", () => itemForm(), "primary"),
        ],
  );
  if (inStore) heading.classList.add("shopping-compact-header");
  add(main, heading, weekControls(), shoppingSections());
  if (state.shoppingPanel === "in_store") return renderInStore(main);
  if (state.shoppingPanel === "oda") return renderBasket(main);
  if (state.shoppingPanel === "pantry") return renderInventory(main);
  const columns = el("div", null, "two-columns");
  const left = el("div");
  const planned = add(
    el("section", null, "panel"),
    el("h2", "Shopping totals"),
    el(
      "p",
      "Required quantity, what is at home, and the remaining amount to buy.",
      "muted",
    ),
  );
  const planLines = state.purchasePlan.lines || [];
  if (!planLines.length)
    add(
      planned,
      el("p", "Plan a meal to bring its ingredients here.", "muted"),
      button("Go to meal plan", () => navigate("week"), "quiet"),
    );
  else {
    const list = el("ul", null, "list");
    planLines.forEach((item) => {
      const unit = item.unit || "";
      const required = String(item.required_quantity ?? item.quantity ?? "");
      const available = item.available_quantity == null ? null : String(item.available_quantity);
      const toBuy = String(item.to_buy_quantity ?? item.quantity ?? "0");
      const summary = el(
        "small",
        window.KitchenShoppingView.formatQuantityForDisplay(required) +
          (unit ? " " + unit : "") +
          (available != null
            ? " · " + window.KitchenShoppingView.formatQuantityForDisplay(available) + " at home"
            : "") +
          " · " +
          (window.KitchenShoppingView.isPositiveDecimal(toBuy)
            ? window.KitchenShoppingView.formatQuantityForDisplay(toBuy) + " to buy"
            : "covered at home") +
          " · " +
          (item.category || "Other") +
          (item.origins?.length ? " · " + shoppingOrigins(item.origins) : ""),
      );
      summary.setAttribute(
        "aria-label",
        "Exact quantities: " +
          required +
          (unit ? " " + unit : "") +
          " required" +
          (available != null ? "; " + available + " " + unit + " at home" : "") +
          "; " +
          toBuy +
          " " +
          unit +
          " to buy",
      );
      add(list, add(el("li"), add(el("div", null, "detail"), el("strong", item.name), summary)));
    });
    add(planned, list);
  }
  const extras = add(
    el("section", null, "panel"),
    el("h2", "Manage quick additions"),
    el(
      "p",
      "Items you add here are included in the shopping totals above. Exclude a capture from planning; picking progress is in In-store trip.",
      "muted",
    ),
  );
  if (!state.items.length)
    add(extras, el("p", "Running low on something? Add it here.", "muted"));
  const itemList = el("ul", null, "list");
  state.items.forEach((item) => {
    const row = el("li", null, item.completed ? "completed" : "");
    const check = el("input");
    check.type = "checkbox";
    check.checked = item.completed;
    check.ariaLabel = "Exclude " + item.name + " from plan";
    check.addEventListener("change", async () => {
      check.disabled = true;
      try {
        await api("/items/" + encodeURIComponent(item.id), "PATCH", {
          completed: check.checked,
        });
        await refreshView();
      } catch (error) {
        check.checked = item.completed;
        notify(error.message, true);
      } finally {
        check.disabled = false;
      }
    });
    const exclude = add(
      el("label", null, "check-label"),
      check,
      el("span", "Exclude " + item.name + " from plan"),
    );
    add(
      row,
      exclude,
      add(
        el("div", null, "detail"),
        el("strong", item.name),
        el("small", item.quantity + " " + item.unit),
      ),
      button(
        "Remove",
        async () => {
          try {
            await mutate("/items/" + encodeURIComponent(item.id), "DELETE");
          } catch (error) {
            notify(error.message, true);
          }
        },
        "quiet small",
      ),
    );
    add(itemList, row);
  });
  add(extras, itemList);
  add(left, planned, extras);
  const right = el("div");
  const regular = add(
    el("section", null, "panel"),
    el("h2", "Everyday items"),
    el(
      "p",
      "Set a gentle reminder for things you buy often. Add due items when you need them.",
      "muted",
    ),
  );
  const suggestions = state.shopping.suggestions || [];
  if (suggestions.length) {
    const list = el("ul", null, "list");
    suggestions.forEach((item) =>
      add(
        list,
        add(
          el("li"),
          add(
            el("div", null, "detail"),
            el("strong", item.name),
            el("small", item.quantity + " " + item.unit + " · Suggested"),
          ),
          button(
            "Add",
            async () => {
              try {
                await mutate(
                  "/regular-items/" + encodeURIComponent(item.id) + "/confirm",
                  "POST",
                  {},
                  "Added to the shopping list.",
                );
              } catch (error) {
                notify(error.message, true);
              }
            },
            "small",
          ),
        ),
      ),
    );
    add(regular, list);
  }
  if (state.regular.length) {
    const list = el("ul", null, "list");
    state.regular.forEach((item) =>
      add(
        list,
        add(
          el("li"),
          add(
            el("div", null, "detail"),
            el("strong", item.name),
            el(
              "small",
              "Every " +
                item.interval_days +
                " days" +
                (item.last_purchased
                  ? " · Last bought " + item.last_purchased
                  : ""),
            ),
          ),
          button(
            "Bought today",
            async () => {
              try {
                await mutate(
                  "/regular-items/" + encodeURIComponent(item.id) + "/purchase",
                  "POST",
                  { purchased_on: iso(new Date()) },
                  "Purchase recorded.",
                );
              } catch (error) {
                notify(error.message, true);
              }
            },
            "small quiet",
          ),
        ),
      ),
    );
    add(regular, list);
  }
  add(
    regular,
    button("+ Add a regular", () => itemForm(true), "quiet"),
    button("Share feedback", () => feedbackForm(), "quiet"),
  );
  const oda = add(
    el("section", null, "panel"),
    el("h2", "Oda"),
    el(
      "p",
      "Connect your Oda account from the Oda tab. In-store checklists work without Oda.",
      "muted",
    ),
  );
  add(
    oda,
    button("Set up Oda in Kitchen", () => navigate("basket"), "quiet"),
  );
  add(right, regular, oda);
  add(columns, left, right);
  add(main, columns);
}
function basketProduct(product, packages, alternatives = false) {
  const card = el(
    "article",
    null,
    alternatives ? "product-card alternative-card" : "product-card",
  );
  const imageUrl = httpsUrl(product.image_url);
  if (imageUrl) {
    const image = el("img", null, "product-image");
    image.src = imageUrl;
    image.alt = product.name + " product photo";
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () =>
      image.replaceWith(el("span", "Image unavailable", "image-fallback")),
    );
    card.append(image);
  }
  const details = el("div", null, "product-details");
  add(
    details,
    el("h4", product.name),
    product.brand ? el("p", product.brand, "muted") : null,
    product.provider ? el("p", "Source: " + product.provider, "muted") : null,
    (product.product_id ?? product.id) != null
      ? el("small", "Oda product ID: " + (product.product_id ?? product.id), "muted")
      : null,
  );
  const pack =
    product.pack_quantity != null
      ? product.pack_quantity +
        (product.pack_unit ? " " + product.pack_unit : "") +
        " per pack"
      : "Pack size unavailable";
  add(
    details,
    el(
      "p",
      pack +
        (packages != null
          ? " · " + packages + (Number(packages) === 1 ? " pack" : " packs")
          : ""),
      "muted",
    ),
  );
  add(
    details,
    el(
      "p",
      product.price == null
        ? "Price unknown"
        : product.price + (product.currency ? " " + product.currency : ""),
      product.price == null ? "muted" : "",
    ),
  );
  add(
    details,
    el(
      "p",
      product.available === true
        ? "Available"
        : product.available === false
          ? "Unavailable"
          : "Availability unknown",
      "muted",
    ),
  );
  add(
    details,
    el(
      "p",
      "Evidence: " +
        (product.evidence_status || "status unknown").replaceAll("_", " ") +
        (product.observed_at
          ? " · Checked " +
            (Number.isNaN(Date.parse(product.observed_at))
              ? product.observed_at
              : new Date(product.observed_at).toLocaleString())
          : ""),
      "muted",
    ),
  );
  const reference = httpsUrl(product.url);
  if (reference) {
    const link = el("a", "View product reference ↗");
    link.href = reference;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(details, link);
  }
  add(card, details);
  return card;
}
function officialOdaProductUrl(value) {
  try {
    const url = new URL(value);
    if (
      url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      (url.hostname === "oda.com" || url.hostname.endsWith(".oda.com"))
    )
      return value;
  } catch {}
  return "";
}
function safeImageUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password
      ? url.href
      : "";
  } catch {
    return "";
  }
}
function providerDecimalPrice(value) {
  if (typeof value !== "string") return null;
  const price = value.trim();
  return /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(price) ? price : null;
}
function chooseOdaProduct(product, need) {
  const productId = Number(product.id);
  if (!Number.isSafeInteger(productId) || productId < 1) {
    notify("Oda did not provide a usable product ID for this result.", true);
    return;
  }
  const root = openDialog("Confirm product and package size");
  add(
    root,
    el(
      "p",
      "You choose how many of " +
        need.unit +
        " are in one package. Kitchen will not infer the package size from the Oda listing.",
      "callout",
    ),
    el("h3", product.name || "Oda product"),
    product.brand ? el("p", product.brand, "muted") : null,
  );
  const form = el("form");
  const packages = field("Number of packages", "number", "");
  packages.input.min = "1";
  packages.input.max = "999";
  packages.input.step = "1";
  packages.input.required = true;
  const packQuantity = field(
    "Amount of " + need.unit + " per package",
    "number",
    "",
  );
  packQuantity.input.min = "0.000001";
  packQuantity.input.step = "any";
  packQuantity.input.required = true;
  add(form, packages.wrap, packQuantity.wrap);
  formSubmit(form, "Save product choice", async () => {
    const packageCount = Number(packages.input.value);
    const quantityText = packQuantity.input.value.trim();
    if (!Number.isSafeInteger(packageCount) || packageCount < 1)
      throw new Error("Enter a whole number of packages greater than zero.");
    if (
      !/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(quantityText) ||
      !Number.isFinite(Number(quantityText)) ||
      Number(quantityText) <= 0
    )
      throw new Error("Enter a positive decimal amount per package.");
    const productUrl = officialOdaProductUrl(product.url);
    const media = productUrl ? state.odaMedia[productUrl] || {} : {};
    await api("/basket/selections", "PUT", {
      start: iso(day(0)),
      end: iso(day(6)),
      selection: {
        need_id: need.id,
        product: {
          provider: "oda",
          product_id: String(productId),
          name: product.name,
          brand: product.brand || null,
          url: productUrl || null,
          image_url: safeImageUrl(media.image_url) || null,
          pack_quantity: quantityText,
          pack_unit: need.unit,
          price: providerDecimalPrice(product.price),
          currency: "NOK",
          available:
            typeof product.availability?.isAvailable === "boolean"
              ? product.availability.isAvailable
              : null,
          observed_at: new Date().toISOString(),
          evidence_status: "unverified",
        },
        packages: packageCount,
        alternatives: [],
      },
    });
    state.cartProposalHandled = false;
    $("#modal").close();
    await refreshView();
    notify("Oda product choice saved for review. It remains unverified.");
  });
  add(root, form);
}
function mediaRetryWait(media) {
  return Math.max(0, (media?.retryUntil || 0) - Date.now());
}
function odaSearchResultCard(product, need) {
  const productUrl = officialOdaProductUrl(product.url);
  const media = productUrl ? state.odaMedia[productUrl] || {} : {};
  const retryWait = mediaRetryWait(media);
  if (retryWait && !media.retryTimer) {
    media.retryTimer = setTimeout(() => {
      media.retryTimer = null;
      render();
    }, retryWait + 25);
    if (productUrl) state.odaMedia[productUrl] = media;
  }
  const card = add(
    el("article", null, "oda-result-card"),
    el("div", null, "oda-picture-gallery"),
  );
  const pictureGallery = card.firstChild;
  const imageUrls = [media.image_url, ...(media.additional_image_urls || [])]
    .map(safeImageUrl)
    .filter(Boolean)
    .filter((url, index, values) => values.indexOf(url) === index)
    .slice(0, 3);
  if (imageUrls.length) {
    imageUrls.forEach((imageUrl, index) => {
      const image = recipeImage(
        { title: product.name || "Oda product", image_url: imageUrl },
        "product-image",
      );
      if (image) {
        image.classList.add("oda-photo-frame");
        if (index > 0) image.classList.add("oda-photo-extra");
        pictureGallery.append(image);
      }
    });
  } else {
    add(
      pictureGallery,
      el(
        "div",
        media.error
          ? "Product picture could not be loaded"
          : "Picture unavailable",
        "oda-picture-fallback",
      ),
    );
  }
  const details = add(
    el("div", null, "product-details"),
    el("h3", product.name || "Oda product"),
  );
  if (product.brand) add(details, el("p", product.brand, "muted"));
  if (product.description) add(details, el("p", product.description));
  if (product.price)
    add(details, el("p", "Oda price: " + product.price, "product-price"));
  else add(details, el("p", "Price unavailable", "muted"));
  if (product.unitPrice || product.unitName)
    add(
      details,
      el(
        "p",
        "Unit price: " +
          (product.unitPrice || "not provided") +
          (product.unitName ? " per " + product.unitName : ""),
        "muted",
      ),
    );
  if (product.availability?.description)
    add(
      details,
      el("p", "Availability: " + product.availability.description, "muted"),
    );
  else if (product.availability?.isAvailable === true)
    add(details, el("p", "Oda marks this as available.", "muted"));
  else if (product.availability?.isAvailable === false)
    add(details, el("p", "Oda marks this as unavailable.", "muted"));
  else add(details, el("p", "Availability not provided", "muted"));
  if (productUrl) {
    const link = el("a", "View Oda reference ↗");
    link.href = productUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(details, link);
  }
  if (media.pack_description)
    add(details, el("p", "Pack details from Oda: " + media.pack_description));
  else
    add(
      details,
      el(
        "p",
        "Pack size unknown. Oda’s unit price is not a package quantity.",
        "muted",
      ),
    );
  if (media.detail) add(details, el("p", media.detail, "muted"));
  if (productUrl && !imageUrls.length) {
    const loadPhoto = button(
      media.loading
        ? "Loading product details…"
        : retryWait
          ? "Try again in " + Math.ceil(retryWait / 1000) + "s"
          : media.requested
            ? "Check for product photo again"
            : "Load product photo",
      async () => {
        if (media.loading || mediaRetryWait(media)) return;
        state.odaMedia[productUrl] = { loading: true, requested: true };
        render();
        try {
          const response = await api("/oda/products/media", "POST", {
            url: product.url,
          });
          const retryAfter = Number(response.retry_after_seconds);
          state.odaMedia[productUrl] = {
            ...response,
            requested: true,
            ...(Number.isFinite(retryAfter) && retryAfter > 0
              ? { retryUntil: Date.now() + retryAfter * 1000 }
              : {}),
          };
        } catch (error) {
          state.odaMedia[productUrl] = {
            error: error.message,
            requested: true,
            ...(Number.isFinite(error.retryAfterSeconds) &&
            error.retryAfterSeconds > 0
              ? { retryUntil: Date.now() + error.retryAfterSeconds * 1000 }
              : {}),
          };
        }
        render();
      },
      "quiet small",
    );
    loadPhoto.disabled = Boolean(media.loading || retryWait);
    add(details, loadPhoto);
  }
  if (Number.isSafeInteger(Number(product.id)) && Number(product.id) > 0)
    add(
      details,
      button(
        "Choose product",
        () => chooseOdaProduct(product, need),
        "primary small",
      ),
    );
  add(
    details,
    el("small", "Suggested match · check product and pack size", "muted"),
  );
  add(card, details);
  return card;
}
function odaSearchForNeed(need) {
  if (!state.oda?.connected) return null;
  const current = state.odaSearch[need.id] || {};
  const form = el("form", null, "oda-search-form");
  const query = field(
    "Search Oda products",
    "search",
    current.query || need.name,
  );
  query.input.required = true;
  query.input.maxLength = 300;
  const submit = el(
    "button",
    current.loading ? "Searching…" : "Search Oda",
    "quiet",
  );
  submit.type = "submit";
  submit.disabled = current.loading || !state.oda?.connected;
  add(form, query.wrap, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = query.input.value.trim();
    if (!text) return;
    state.odaSearch[need.id] = { query: text, loading: true };
    render();
    try {
      const response = await api("/oda/products/search", "POST", {
        queries: [text],
        page: 1,
        size: 5,
      });
      state.odaSearch[need.id] = {
        query: text,
        results: response.result || [],
      };
    } catch (error) {
      state.odaSearch[need.id] = { query: text, error: error.message };
    }
    render();
  });
  const root = add(
    el("section", null, "oda-search"),
    el("h3", "Find Oda products"),
    form,
  );
  if (!state.oda?.connected) {
    add(
      root,
      el("p", "Connect your Oda account to Kitchen before searching.", "muted"),
    );
  } else if (current.loading) {
    add(root, el("p", "Searching Oda…", "muted"));
  } else if (current.error) {
    add(root, el("p", current.error, "form-error"));
  } else if (current.results) {
    const products = current.results.flatMap((result) => result.products || []);
    if (products.length)
      products.forEach((product) =>
        add(root, odaSearchResultCard(product, need)),
      );
    else add(root, el("p", "No products found for that search.", "muted"));
    if (current.results.some((result) => result.hasMore))
      add(
        root,
        el(
          "p",
          "More results are available in Oda. Kitchen shows the first results here.",
          "muted",
        ),
      );
  }
  return root;
}
function odaCartProposalRequest(basket) {
  const byProduct = new Map();
  const queries = new Set();
  let invalidSelections = 0;
  (basket.needs || []).forEach((entry) => {
    const need = entry.need || {};
    (entry.selections || []).forEach((selection) => {
      const product = selection.product || {};
      const productId = Number(product.product_id);
      const quantity = Number(selection.packages);
      if (
        product.provider !== "oda" ||
        !Number.isSafeInteger(productId) ||
        productId < 1 ||
        !Number.isSafeInteger(quantity) ||
        quantity < 1
      ) {
        invalidSelections += 1;
        return;
      }
      const key = String(productId);
      const current = byProduct.get(key) || {
        product_id: productId,
        quantity: 0,
      };
      if (!Number.isSafeInteger(current.quantity + quantity)) {
        invalidSelections += 1;
        return;
      }
      current.quantity += quantity;
      byProduct.set(key, current);
      if (need.name) queries.add(need.name);
    });
  });
  const additions = [...byProduct.values()];
  return {
    queries: [...queries],
    additions,
    invalidSelections,
    issue:
      additions.length > 10 || queries.size > 10
        ? "Review at most 10 distinct product types at a time."
        : !queries.size
          ? "Each selected Oda product needs a shopping-need name for a fresh provider check."
          : invalidSelections
            ? "One or more saved product choices do not have a usable Oda product ID or package count. Review those choices first."
            : "",
  };
}
function proposalProductDetails(product) {
  const details = add(
    el("div", null, "product-details proposal-product"),
    el("h3", product.name || "Oda product"),
  );
  if (product.brand) add(details, el("p", product.brand, "muted"));
  if (product.description) add(details, el("p", product.description));
  const productUrl = officialOdaProductUrl(product.url);
  const savedMedia = productUrl ? state.odaMedia[productUrl] || {} : {};
  const imageUrl = safeImageUrl(product.image_url || savedMedia.image_url);
  if (imageUrl) {
    const image = recipeImage(
      { title: product.name || "Oda product", image_url: imageUrl },
      "product-image",
    );
    if (image) {
      image.classList.add("oda-proposal-image");
      details.prepend(image);
    }
  }
  const availability =
    product.availability?.description ||
    (product.availability?.isAvailable === true || product.available === true
      ? "Oda marks this available"
      : product.availability?.isAvailable === false ||
          product.available === false
        ? "Oda marks this unavailable"
        : "Availability not provided");
  add(
    details,
    el(
      "p",
      product.price == null
        ? "Fresh Oda price unavailable"
        : "Fresh Oda price: " +
            product.price +
            (product.currency ? " " + product.currency : ""),
      product.price == null ? "muted" : "product-price",
    ),
    el("p", availability, "muted"),
  );
  if (productUrl) {
    const link = el("a", "View this Oda product ↗");
    link.href = productUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(details, link);
  } else {
    add(details, el("p", "Oda returned no product page reference.", "muted"));
  }
  return details;
}
function proposalWarningText(warning) {
  const code = typeof warning === "string" ? warning : warning?.code;
  if (code === "mapping_unverified")
    return "Oda refreshed the selected products, but their match to each shopping need is not independently verified. Review the product and quantity details above.";
  if (code === "payment_at_oda")
    return "Any order and payment must be reviewed and completed at Oda.";
  return typeof warning === "string"
    ? warning.replaceAll("_", " ")
    : warning?.message || warning?.detail || "Oda returned a warning.";
}
function selectedMappingDetails(productId) {
  const details = [];
  (state.basket?.needs || []).forEach((entry) => {
    const need = entry.need || {};
    (entry.selections || []).forEach((selection) => {
      if (String(selection.product?.product_id) === String(productId))
        details.push(
          "You entered " +
            selection.packages +
            " packages for " +
            (need.name || "this need") +
            " · " +
            selection.product.pack_quantity +
            " " +
            selection.product.pack_unit +
            " per package",
        );
    });
  });
  return details;
}
function proposalExpiryText(value) {
  if (!value) return "";
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp)
    ? ""
    : "Review expires " + new Date(timestamp).toLocaleString() + ".";
}
function renderProposalOutcome(
  proposal,
  status,
  detail = "",
  cleanupFailed = false,
) {
  const known = ["success", "stale", "uncertain", "attempted"].includes(status)
    ? status
    : "uncertain";
  state.cartProposalHandled = known === "success";
  state.cartProposalBlocked =
    cleanupFailed || known === "uncertain" || known === "attempted";
  render();
  const root = openDialog("Oda cart update status");
  const messages = {
    success:
      "Oda accepted the reviewed additions. This changed the Oda cart only; it did not place an order or make a payment.",
    stale:
      "The Oda cart changed after review. Kitchen did not apply these additions. Refresh and review again before deciding what to add.",
    uncertain:
      "Kitchen cannot confirm whether Oda applied these additions. Do not confirm again. Check the actual Oda cart first.",
    attempted:
      "Oda reported an attempted update, but Kitchen cannot confirm the final cart. Do not confirm again. Check the actual Oda cart first.",
  };
  add(root, el("p", "Status: " + known.replaceAll("_", " "), "eyebrow"));
  add(
    root,
    el(
      "p",
      detail || messages[known],
      known === "success" ? "callout" : "callout offline",
    ),
  );
  add(root, el("p", messages[known], "muted"));
  const cartLink = el("a", "Review your Oda cart ↗");
  cartLink.href = "https://oda.com/no/cart/";
  cartLink.target = "_blank";
  cartLink.rel = "noopener noreferrer";
  add(root, cartLink);
  const close = button("Close", () => $("#modal").close(), "quiet");
  add(root, add(el("div", null, "actions"), close));
  return root;
}
function renderOdaProposal(proposal, request) {
  const root = openDialog("Review proposed Oda cart additions");
  add(
    root,
    el(
      "p",
      "This adds these packages to your existing Oda cart. Review the full cart and pay at Oda. Kitchen does not place an order or handle payment.",
      "callout",
    ),
  );
  if (proposal.expires_at)
    add(root, el("p", proposalExpiryText(proposal.expires_at), "muted"));
  const list = el("div", null, "proposal-additions");
  (proposal.additions || []).forEach((addition) => {
    const item = add(el("section", null, "proposal-addition"));
    const product = addition.product || {};
    add(item, proposalProductDetails(product));
    selectedMappingDetails(product.id ?? product.product_id).forEach(
      (mapping) => add(item, el("p", mapping, "muted")),
    );
    add(
      item,
      el(
        "p",
        "Packages added: " +
          addition.quantity +
          " · Oda cart quantity: " +
          (addition.current_quantity ?? "unknown") +
          " → " +
          (addition.after_quantity ?? "unknown"),
        "proposal-quantity",
      ),
    );
    list.append(item);
  });
  add(root, list);
  if (proposal.warnings?.length) {
    const warnings = add(
      el("section", null, "callout offline"),
      el("strong", "Review these notes"),
    );
    const warningList = el("ul");
    proposal.warnings.forEach((warning) =>
      add(warningList, el("li", proposalWarningText(warning))),
    );
    add(warnings, warningList);
    add(root, warnings);
  }
  add(
    root,
    el(
      "p",
      "Only the listed additions are proposed. Other items already in your Oda cart stay as they are.",
      "muted",
    ),
  );
  const expiry = Date.parse(proposal.expires_at || "");
  const expired = Number.isFinite(expiry) && expiry <= Date.now();
  const confirm = button(
    expired ? "Review expired" : "Add these packs to Oda cart",
    async () => confirmOdaCartProposal(proposal, request),
    "primary",
  );
  confirm.disabled =
    proposal.status !== "pending" ||
    !proposal.proposal_id ||
    !proposal.additions?.length ||
    expired;
  const close = button("Cancel", () => $("#modal").close(), "quiet");
  add(root, add(el("div", null, "actions"), close, confirm));
  if (confirm.disabled)
    add(
      root,
      el(
        "p",
        "This proposal cannot be confirmed. Request a fresh review before continuing.",
        "form-error",
      ),
    );
  else
    add(
      root,
      el(
        "p",
        "Confirming adds only the package counts listed above. No order or payment is made.",
        "muted",
      ),
    );
}
async function clearConfirmedSelections(request) {
  const confirmedIds = new Set(
    request.additions.map((item) => String(item.product_id)),
  );
  const toRemove = [];
  (state.basket?.needs || []).forEach((entry) => {
    (entry.selections || []).forEach((selection) => {
      const product = selection.product || {};
      if (
        product.provider === "oda" &&
        confirmedIds.has(String(product.product_id))
      )
        toRemove.push({
          needId: entry.need?.id,
          productId: product.product_id,
        });
    });
  });
  const removed = [];
  const failures = [];
  for (const selection of toRemove) {
    const key = String(selection.needId) + ":" + String(selection.productId);
    if (removed.includes(key)) continue;
    try {
      await api(
        "/basket/selections/" +
          encodeURIComponent(selection.needId) +
          "/" +
          encodeURIComponent(selection.productId) +
          range(),
        "DELETE",
      );
      removed.push(key);
    } catch (error) {
      failures.push(error.message);
    }
  }
  if (removed.length && state.basket?.needs) {
    state.basket.needs = state.basket.needs.map((entry) => ({
      ...entry,
      selections: (entry.selections || []).filter(
        (selection) =>
          !removed.includes(
            String(entry.need?.id) +
              ":" +
              String(selection.product?.product_id),
          ),
      ),
    }));
  }
  if (!failures.length) {
    try {
      state.basket = await api("/basket" + range());
    } catch (error) {
      failures.push(error.message);
    }
  }
  return failures;
}
async function confirmOdaCartProposal(proposal, request) {
  if (state.cartConfirming || !proposal.proposal_id) return;
  state.cartConfirming = true;
  const buttonStates = [...document.querySelectorAll("button")].map((node) => [
    node,
    node.disabled,
  ]);
  buttonStates.forEach(([node]) => {
    node.disabled = true;
  });
  const root = $("#modal-content");
  root.replaceChildren(
    el("h2", "Confirming Oda cart additions"),
    el(
      "p",
      "Waiting for Oda. Do not close this dialog or retry the confirmation.",
      "callout",
    ),
  );
  let status = "uncertain";
  let detail = "";
  let cleanupFailed = false;
  try {
    const result = await api(
      "/oda/cart/proposals/" +
        encodeURIComponent(proposal.proposal_id) +
        "/confirm",
      "POST",
      { reviewed: true },
    );
    status = result?.status || "uncertain";
    detail = result?.detail || result?.message || "";
    if (status === "success") {
      const failures = await clearConfirmedSelections(request);
      if (failures.length) {
        cleanupFailed = true;
        detail =
          "Oda accepted the additions, but Kitchen could not clear every saved product choice. Remove any remaining choices from review before another Oda cart update.";
      }
    }
  } catch (error) {
    detail = error.message;
    status = "uncertain";
  } finally {
    state.cartConfirming = false;
    buttonStates.forEach(([node, disabled]) => {
      if (node.isConnected) node.disabled = disabled;
    });
  }
  renderProposalOutcome(proposal, status, detail, cleanupFailed);
}
async function createOdaCartProposal(request, trigger) {
  if (state.cartProposalLoading) return;
  state.cartProposalLoading = true;
  if (trigger) {
    trigger.disabled = true;
    trigger.textContent = "Checking current Oda cart…";
  }
  try {
    const proposal = await api("/oda/cart/proposals", "POST", {
      queries: request.queries,
      additions: request.additions,
    });
    if (!proposal || typeof proposal !== "object")
      throw new Error(
        "Oda returned an unreadable cart proposal. No additions were confirmed.",
      );
    renderOdaProposal(proposal, request);
  } catch (error) {
    notify(error.message + " No cart additions were confirmed.", true);
  } finally {
    state.cartProposalLoading = false;
    if (trigger?.isConnected) {
      trigger.disabled = false;
      trigger.textContent = "Review additions to Oda cart";
    }
  }
}
function renderBasket(main) {
  const basket = state.basket || { needs: [], issues: [] };
  const odaSetup = add(
    el("section", null, "panel oda-connection"),
    el("h2", state.oda?.connected ? "Oda connected" : "Connect Oda"),
  );
  if (state.oda?.connected) {
    add(
      odaSetup,
      button(
        "Disconnect Oda",
        async () => {
          try {
            await api("/oda/connection", "DELETE");
            state.oda = await api("/oda/status");
            render();
            notify("Oda disconnected from Kitchen.");
          } catch (error) {
            notify(error.message, true);
          }
        },
        "quiet",
      ),
    );
    const odaInfo = el("details", null, "shopping-info");
    add(
      odaInfo,
      el("summary", "About Oda product choices"),
      el("p", "Product matches are unverified suggestions. Check the exact product and enter package quantities explicitly. Kitchen never places orders or pays."),
    );
    add(odaSetup, odaInfo);
  } else if (state.oda?.configured) {
    add(
      odaSetup,
      el(
        "p",
        "Connect your Oda account to search products and review cart additions.",
        "muted",
      ),
    );
    add(
      odaSetup,
      button(
        "Connect Oda account",
        async () => {
          try {
            const result = await api("/oda/connect", "POST", {});
            let authorization;
            try {
              authorization = new URL(result.authorization_url);
            } catch {}
            if (
              !authorization ||
              authorization.protocol !== "https:" ||
              authorization.host !== "oda.com" ||
              !authorization.pathname.startsWith("/o/authorize/")
            )
              throw new Error(
                "Kitchen did not return a valid Oda authorization link.",
              );
            window.location.assign(authorization.href);
          } catch (error) {
            notify(error.message, true);
          }
        },
        "primary",
      ),
    );
  } else {
    add(
      odaSetup,
      el(
        "p",
        "Oda setup needs to be completed by your household administrator.",
        "muted",
      ),
    );
    const unavailable = button("Oda setup required", () => {}, "quiet");
    unavailable.disabled = true;
    add(odaSetup, unavailable);
  }
  add(main, odaSetup);
  if (basket.needs?.length) {
    const hasSelections = window.KitchenShoppingView.hasSelectedProducts(basket);
    const status = el(
      "p",
      basket.ready_for_review
        ? "Ready for cart review"
        : hasSelections
          ? "Review selected products"
          : "Choose a product for each need",
      "basket-status" + (basket.ready_for_review ? "" : " needs-attention"),
    );
    const priceSummary = window.KitchenShoppingView.basketPriceSummary(basket);
    if (priceSummary) {
      const summary = el("span", priceSummary.label, "basket-price-summary");
      if (priceSummary.accessibleLabel)
        summary.setAttribute("aria-label", priceSummary.accessibleLabel);
      add(status, summary);
    }
    add(main, status);
  }
  if (basket.issues?.length) {
    const issues = el("details", null, "shopping-info basket-issues");
    add(issues, el("summary", "Review details (" + basket.issues.length + ")"));
    const list = el("ul");
    basket.issues.forEach((issue) =>
      (() => {
        const exact =
          typeof issue === "string" ? issue : issue.message || JSON.stringify(issue);
        const item = el("li", window.KitchenShoppingView.formatQuantityMentions(exact));
        item.title = exact;
        return add(list, item);
      })(),
    );
    add(issues, list);
    add(main, issues);
  }
  if (!basket.needs?.length) {
    add(
      main,
      empty(
        "No shopping needs to match yet",
        "Plan meals or add shopping needs first.",
        button(
          "Open shopping needs",
          () => {
            state.shoppingPanel = "needs";
            navigate("shopping");
          },
          "primary",
        ),
      ),
    );
    return;
  }
  const needs = el("div", null, "basket-needs");
  basket.needs.forEach((entry) => {
    const need = entry.need || {};
    const section = el("section", null, "panel need-card");
    const heading = el("div", null, "need-heading");
    const needQuantity = el(
      "p",
      window.KitchenShoppingView.formatQuantityForDisplay(String(need.quantity ?? "")) +
        (need.unit ? " " + need.unit : ""),
      "need-quantity",
    );
    needQuantity.setAttribute(
      "aria-label",
      "Exact quantity: " + String(need.quantity ?? "") + (need.unit ? " " + need.unit : ""),
    );
    add(
      heading,
      add(
        el("div"),
        el("h2", need.name || "Shopping need"),
        needQuantity,
      ),
    );
    if (need.origins?.length)
      add(heading, el("p", "From " + shoppingOrigins(need.origins), "muted"));
    add(section, heading);
    if (
      entry.allocated_quantity != null ||
      entry.remaining_quantity != null ||
      entry.overbuy_quantity != null
    ) {
      const allocated = String(entry.allocated_quantity ?? "0");
      const remaining = String(entry.remaining_quantity ?? "unknown");
      const overbuy = String(entry.overbuy_quantity ?? "unknown");
      const unit = need.unit || "";
      const allocation = el(
        "p",
        "Covered by selected packs: " +
          window.KitchenShoppingView.formatQuantityForDisplay(allocated) +
          (unit ? " " + unit : "") +
          " · Still needed: " +
          window.KitchenShoppingView.formatQuantityForDisplay(remaining) +
          (unit ? " " + unit : "") +
          " · Extra from selected packs: " +
          window.KitchenShoppingView.formatQuantityForDisplay(overbuy) +
          (unit ? " " + unit : ""),
        "muted",
      );
      allocation.setAttribute(
        "aria-label",
        "Exact quantities: covered by selected packs " +
          allocated +
          (unit ? " " + unit : "") +
          "; still needed " +
          remaining +
          (unit ? " " + unit : "") +
          "; extra from selected packs " +
          overbuy +
          (unit ? " " + unit : ""),
      );
      add(section, allocation);
    }
    const selections = entry.selections || [];
    add(section, odaSearchForNeed(need));
    selections.forEach((selection) => {
      if (selection.product) {
        const selected = el("div", null, "selected-product");
        add(selected, basketProduct(selection.product, selection.packages));
        add(
          selected,
          button(
            "Remove from review",
            async () => {
              try {
                await api(
                  "/basket/selections/" +
                    encodeURIComponent(need.id) +
                    "/" +
                    encodeURIComponent(selection.product.product_id) +
                    range(),
                  "DELETE",
                );
                state.cartProposalHandled = false;
                await refreshView();
              } catch (error) {
                notify(error.message, true);
              }
            },
            "quiet small",
          ),
        );
        add(section, selected);
      }
      if (selection.alternatives?.length) {
        add(section, el("h3", "Alternatives"));
        const alternatives = el("div", null, "alternatives-grid");
        selection.alternatives.forEach((product) =>
          add(alternatives, basketProduct(product, null, true)),
        );
        add(section, alternatives);
      }
    });
    add(needs, section);
  });
  add(main, needs);
  const proposalRequest = odaCartProposalRequest(basket);
  if (
    state.oda?.connected &&
    (proposalRequest.additions.length || proposalRequest.invalidSelections)
  ) {
    const cartPanel = add(
      el("section", null, "panel oda-cart-panel"),
      el("h2", "Add selected products to Oda"),
      el(
        "p",
        "Review exact additions against the current Oda cart before confirming. Existing Oda cart items are kept.",
        "muted",
      ),
    );
    if (proposalRequest.issue)
      add(cartPanel, el("p", proposalRequest.issue, "form-error"));
    else if (state.cartProposalBlocked)
      add(
        cartPanel,
        el(
          "p",
          "The last Oda cart update may have been applied. Do not retry it from Kitchen. Review the actual Oda cart first.",
          "callout offline",
        ),
      );
    else if (state.cartProposalHandled)
      add(
        cartPanel,
        el(
          "p",
          "The reviewed additions were accepted and removed from this Kitchen review.",
          "callout",
        ),
      );
    else {
      let review;
      review = button(
        state.cartProposalLoading
          ? "Checking current Oda cart…"
          : "Review additions to Oda cart",
        () => createOdaCartProposal(proposalRequest, review),
        "primary",
      );
      review.disabled = state.cartProposalLoading;
      add(cartPanel, review);
    }
    add(main, cartPanel);
  }
}
function feedbackForm(recipe) {
  const root = openDialog(recipe ? "Rate this recipe" : "Share feedback");
  if (recipe) {
    add(
      root,
      el(
        "p",
        "Your rating or note is saved as household context for future recommendations. This form records only the feedback you enter, not a full chat.",
        "muted",
      ),
    );
    const form = el("form");
    const rating = field("Rating (optional)", "number", "", "Choose a whole number from 1 to 5.");
    rating.input.min = "1";
    rating.input.max = "5";
    rating.input.step = "1";
    const wouldMakeAgain = field("Would cook this again? (optional)", "select");
    [
      ["", "No answer"],
      ["true", "Yes"],
      ["false", "No"],
    ].forEach(([value, label]) => {
      const option = el("option", label);
      option.value = value;
      add(wouldMakeAgain.input, option);
    });
    const notes = field("Notes (optional)", "textarea", "", "What worked well, or what should change next time?");
    notes.input.maxLength = 2000;
    notes.input.rows = 4;
    add(form, rating.wrap, wouldMakeAgain.wrap, notes.wrap);
    formSubmit(form, "Save feedback", async () => {
      const payload = {};
      if (rating.input.value.trim()) payload.rating = Number(rating.input.value);
      if (wouldMakeAgain.input.value) {
        payload.would_make_again = wouldMakeAgain.input.value === "true";
      }
      if (notes.input.value.trim()) payload.notes = notes.input.value.trim();
      await api(`/recipes/${recipe.id}/feedback`, "POST", payload);
      $("#modal").close();
      notify("Feedback saved for future recommendations.");
    });
    add(root, form);
    return;
  }
  add(
    root,
    el(
      "p",
      "Your note is saved as household context for future recommendations. This form records only the feedback you enter, not a full chat.",
      "muted",
    ),
  );
  const form = el("form");
  const kind = field("Type", "select");
  [
    ["feedback", "General feedback"],
    ["preference", "Food preference"],
    ["recipe_rating", "Recipe rating"],
  ].forEach(([value, label]) => {
    const option = el("option", label);
    option.value = value;
    add(kind.input, option);
  });
  if (recipe) {
    kind.input.value = "recipe_rating";
    kind.input.disabled = true;
  }
  const message = field(
    recipe ? "Your rating or note" : "Your note",
    "textarea",
    "",
    recipe
      ? "What worked well, or what should change next time?"
      : "A short note to guide future recommendations.",
  );
  message.input.required = true;
  message.input.maxLength = 2000;
  message.input.rows = 4;
  add(form, kind.wrap, message.wrap);
  formSubmit(form, "Save feedback", async () => {
    await api("/interactions", "POST", {
      channel: "app",
      kind: kind.input.value,
      message: message.input.value.trim(),
      structured: {},
      ...(recipe ? { recipe_id: recipe.id } : {}),
    });
    $("#modal").close();
    notify("Feedback saved for future recommendations.");
  });
  add(root, form);
}
function itemForm(regular = false) {
  const root = openDialog(
    regular ? "Add an everyday regular" : "Add to the shopping list",
  );
  const form = el("form");
  const name = field("Item name");
  name.input.required = true;
  const quantity = field("Quantity", "number", "1");
  quantity.input.min = "0.001";
  quantity.input.step = "any";
  quantity.input.required = true;
  const unit = field("Unit", "text", "item");
  unit.input.required = true;
  const category = field(
    "Category",
    "text",
    "Other",
    "Used to organize the in-store list.",
  );
  category.input.maxLength = 60;
  add(
    form,
    name.wrap,
    add(el("div", null, "form-grid"), quantity.wrap, unit.wrap),
    category.wrap,
  );
  const interval = field("Usually buy every (days)", "number", 7);
  interval.input.min = 1;
  interval.input.required = true;
  if (regular) add(form, interval.wrap);
  formSubmit(form, regular ? "Save regular" : "Add item", () =>
    mutate(
      regular ? "/regular-items" : "/items",
      "POST",
      {
        name: name.input.value.trim(),
        quantity: quantity.input.value,
        unit: unit.input.value.trim(),
        category: category.input.value.trim() || "Other",
        ...(regular ? { interval_days: Number(interval.input.value) } : {}),
      },
      regular ? "Regular item saved." : "Item added.",
    ),
  );
  add(root, form);
}
function renderSettings(main) {
  const household = state.household;
  const first = !household.name;
  add(
    main,
    title(
      "Make yourself at home",
      first ? "Welcome to your kitchen." : "Your household.",
      first
        ? "A few details help make meal planning fit your home. You can change these anytime."
        : "The people, preferences, and portions behind your plan.",
    ),
  );
  const form = el("form", null, "panel");
  const name = field(
    "Household name",
    "text",
    household.name || "",
    "Choose a name for your shared kitchen.",
  );
  name.input.required = true;
  name.input.maxLength = 100;
  const adults = field("Adults", "number", household.adults ?? 1);
  adults.input.min = 0;
  adults.input.max = 30;
  adults.input.required = true;
  const children = field(
    "Children’s ages",
    "text",
    (household.children_ages || []).join(", "),
    "Optional. Separate ages with commas, for example: 4, 8.",
  );
  const servings = field(
    "Default meal servings",
    "number",
    household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  const language = field("Preferred language", "select");
  [
    ["en", "English"],
    ["no", "Norsk"],
    ["fr", "Français"],
  ].forEach(([value, label]) => {
    const option = el("option", label);
    option.value = value;
    add(language.input, option);
  });
  language.input.value = household.language || "en";
  const preferences = field(
    "Food preferences",
    "textarea",
    (household.dietary_preferences || []).join("\n"),
    "Optional. One per line, including allergies or ingredients to avoid. Always check recipes and product labels.",
  );
  preferences.input.rows = 3;
  add(
    form,
    name.wrap,
    add(
      el("div", null, "form-grid"),
      adults.wrap,
      children.wrap,
      servings.wrap,
      language.wrap,
    ),
    el("br"),
    preferences.wrap,
    el(
      "p",
      "The interface currently uses English. Your preferred language is saved for recipe and household context.",
      "muted",
    ),
  );
  formSubmit(form, first ? "Set up my kitchen" : "Save household", async () => {
    const values = children.input.value
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (values.some((value) => !/^\d+$/.test(value) || Number(value) > 17))
      throw new Error(
        "Enter children’s ages as whole numbers from 0 to 17, separated by commas.",
      );
    state.household = await api("/household", "PUT", {
      name: name.input.value.trim(),
      adults: Number(adults.input.value),
      children_ages: values.map(Number),
      dietary_preferences: preferences.input.value
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean),
      default_servings: Number(servings.input.value),
      language: language.input.value,
    });
    notify("Household saved.");
    if (first) navigate("week");
  });
  add(main, form);
  if (!first) {
    add(
      main,
      add(
        el("section", null, "panel"),
        el("h2", "Connected services"),
        el(
          "p",
          "AI assistance: " +
            (state.integrations.ai?.configured
              ? "configured"
              : "setup required") +
            ". Oda account connection and setup are managed from Shopping.",
          "muted",
        ),
        el(
          "p",
          "You can install Kitchen from your browser’s menu on Android. The app shell opens offline; shared recipes and changes need a connection.",
          "muted",
        ),
      ),
    );
  }
}
$("#modal").addEventListener("cancel", (event) => {
  if (state.cartConfirming) event.preventDefault();
});
document
  .querySelectorAll("[data-view]")
  .forEach((node) =>
    node.addEventListener("click", () => navigate(node.dataset.view)),
  );
window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  const panelChanged = view === "basket" && state.shoppingPanel !== "oda";
  if (view === "basket") state.shoppingPanel = "oda";
  const nextView = view === "basket" ? "shopping" : view;
  if (
    ["week", "recipes", "shopping", "settings"].includes(nextView) &&
    (nextView !== state.view || panelChanged)
  ) {
    state.view = nextView;
    refreshView();
  }
});
window.addEventListener("offline", () => {
  notify(
    "You’re offline. Reconnect before saving changes. Your open form stays here.",
    true,
  );
});
window.addEventListener("online", () => {
  notify("Connection restored.");
  if (!$("#modal").open && !$("#main form")) refreshView();
});
async function start() {
  try {
    const odaResult = new URLSearchParams(location.search).get("oda");
    const requestedDate = parseDate(new URLSearchParams(location.search).get("date"));
    if (requestedDate) state.week = requestedDate;
    [state.household, state.recipes, state.integrations] = await Promise.all([
      api("/household"),
      api("/recipes"),
      api("/integrations"),
    ]);
    const requested = location.hash.slice(1);
    if (odaResult || requested === "basket") state.shoppingPanel = "oda";
    const requestedView = requested === "basket" ? "shopping" : requested;
    state.view = !state.household.name
      ? "settings"
      : odaResult
        ? "shopping"
        : ["week", "recipes", "shopping", "settings"].includes(requestedView)
          ? requestedView
          : "week";
    syncPlannerUrl();
    await refreshView();
    if (odaResult) {
      const remainingQuery = new URLSearchParams(location.search);
      remainingQuery.delete("oda");
      history.replaceState(
        null,
        "",
        location.pathname +
          (remainingQuery.size ? "?" + remainingQuery.toString() : "") +
          (state.household.name ? "#shopping" : ""),
      );
      if (odaResult === "connected") {
        try {
          state.oda = await api("/oda/status");
        } catch {}
        notify(
          state.oda?.connected
            ? "Oda connected to Kitchen."
            : "Kitchen could not confirm the Oda connection.",
          !state.oda?.connected,
        );
      } else {
        notify("Oda connection did not complete. You can try again.", true);
      }
    }
  } catch (error) {
    const main = $("#main");
    main.replaceChildren(
      title(
        "Welcome to Kitchen",
        "Let’s get connected.",
        "Your shared kitchen needs a connection to load.",
      ),
    );
    add(
      main,
      empty(
        "Your kitchen is unavailable",
        error.message,
        button("Try again", start, "primary"),
      ),
    );
  }
}
if ("serviceWorker" in navigator)
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
start();
