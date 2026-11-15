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
  purchaseMode: "oda",
  basket: null,
  items: [],
  regular: [],
  integrations: {},
  week: monday(new Date()),
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
function monday(value) {
  const date = new Date(value);
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
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
  const row = el("div", null, "toolbar");
  const change = async (amount) => {
    state.week.setDate(state.week.getDate() + amount);
    await refreshView();
  };
  add(
    row,
    button("←", () => change(-7)),
    el(
      "span",
      dateLabel(day(0), { month: "short", day: "numeric" }) +
        " – " +
        dateLabel(day(6), { month: "short", day: "numeric", year: "numeric" }),
      "week-range",
    ),
    button("→", () => change(7)),
    button(
      "This week",
      async () => {
        state.week = monday(new Date());
        await refreshView();
      },
      "quiet small",
    ),
  );
  row.children[0].ariaLabel = "Previous week";
  row.children[2].ariaLabel = "Next week";
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
      if (state.shoppingPanel === "in_store")
        state.storeTrip = await api("/store-trip" + range());
    }
    if (state.view === "basket") {
      const results = await Promise.all([
        api("/basket" + range()),
        api("/oda/status"),
      ]);
      [state.basket, state.oda] = results;
      if (state.purchaseMode === "in_store")
        state.storeTrip = await api("/store-trip" + range());
    }
    render();
  } catch (error) {
    notify(error.message, true);
  }
}
function navigate(view) {
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
      basket: renderBasket,
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
      "A simple plan for the days ahead.",
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
    if (meals.length) {
      meals.forEach((meal) => {
        const recipe = state.recipes.find((item) => item.id === meal.recipe_id);
        add(
          card,
          el("span", "DINNER", "eyebrow"),
          el("h3", recipe?.title || "Recipe unavailable", "meal-title"),
          el("p", meal.servings + " servings", "muted"),
          add(
            el("div", null, "meal-actions"),
            recipe
              ? button("View recipe", () => viewRecipe(recipe), "small")
              : null,
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
    } else
      add(
        card,
        el("p", "Something good?", "empty-meal"),
        button("+ Add dinner", () => mealForm(iso(date)), "quiet"),
      );
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
function mealForm(date = iso(day(0)), recipeId) {
  if (!state.recipes.length) {
    const root = openDialog("Start with a recipe");
    add(
      root,
      el("p", "Add your first recipe, then choose a day for it.", "muted"),
      button("Add a recipe", () => recipeForm(), "primary"),
    );
    return;
  }
  const root = openDialog("Plan a dinner");
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
  const servings = field(
    "Servings",
    "number",
    state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  add(form, chosenDate.wrap, recipe.wrap, servings.wrap);
  formSubmit(form, "Add to week", () =>
    mutate(
      "/meals",
      "POST",
      {
        date: chosenDate.input.value,
        slot: "dinner",
        recipe_id: recipe.input.value,
        servings: Number(servings.input.value),
      },
      "Dinner is planned.",
    ),
  );
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
  add(tools, search, button("Ask AI for an idea", aiForm, "quiet"));
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
          "Add a recipe you love, import a recipe link, or ask for a new idea. Your collection is yours to build.",
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
  const root = openDialog(draft.title ? "Review your recipe" : "Add a recipe");
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
    "Recipe servings",
    "number",
    draft.servings || state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  const imageUrl = field(
    "Recipe photo URL (optional)",
    "url",
    draft.image_url || "",
    "Use an HTTPS image URL. No photo is added unless you provide one.",
  );
  imageUrl.input.placeholder = "https://…";
  const ingredientText = (draft.ingredients || [])
    .map((item) =>
      typeof item === "string"
        ? item + " | | | Other"
        : [item.name, item.quantity, item.unit, item.category || "Other"].join(
            " | ",
          ),
    )
    .join("\n");
  const ingredients = field(
    "Ingredients",
    "textarea",
    ingredientText,
    "One per line: ingredient | quantity | unit | category. For example: Carrots | 3 | item | Produce. Use Other when unsure and a decimal point for quantities, such as 0.5.",
  );
  ingredients.input.required = true;
  ingredients.input.placeholder =
    "Carrots | 3 | item | Produce\nOlive oil | 2 | tbsp | Pantry";
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
    ingredients.wrap,
    instructions.wrap,
  );
  formSubmit(form, "Save recipe", async () => {
    const photo = imageUrl.input.value.trim();
    if (photo && !httpsUrl(photo))
      throw new Error("Recipe photo URL must use HTTPS.");
    const rows = ingredients.input.value
      .split("\n")
      .filter((line) => line.trim())
      .map((line, index) => {
        const parts = line.split("|").map((value) => value.trim());
        if (
          ![3, 4].includes(parts.length) ||
          !parts[0] ||
          !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(parts[1]) ||
          Number(parts[1]) <= 0 ||
          !parts[2]
        )
          throw new Error(
            "Ingredient line " +
              (index + 1) +
              " needs a name, positive quantity and unit separated by |.",
          );
        return {
          name: parts[0],
          quantity: parts[1],
          unit: parts[2],
          category: parts[3] || "Other",
        };
      });
    await api("/recipes", "POST", {
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
    });
    state.recipes = await api("/recipes");
    $("#modal").close();
    navigate("recipes");
    notify("Recipe added to your collection.");
  });
  add(root, form);
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
      button("Plan a dinner", () => mealForm(undefined, recipe.id), "primary"),
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
      "Paste a recipe page link. You’ll review the result and add ingredient quantities before saving.",
      "muted",
    ),
  );
  const form = el("form");
  const url = field("Recipe link", "url");
  url.input.required = true;
  url.input.placeholder = "https://…";
  add(form, url.wrap);
  formSubmit(form, "Import for review", async () => {
    const draft = await api("/recipe-import", "POST", { url: url.input.value });
    recipeForm(draft, "imported");
  });
  add(root, form);
}
function aiForm() {
  const root = openDialog("What sounds good?");
  if (!state.integrations.ai?.configured) {
    add(
      root,
      el(
        "p",
        "AI recipe ideas need an AI provider configured by your household administrator. You can add recipes manually or import a link in the meantime.",
        "callout",
      ),
      button("Add a recipe", () => recipeForm(), "primary"),
    );
    return;
  }
  add(
    root,
    el(
      "p",
      "Describe the meal you have in mind. Review generated ingredients and instructions before saving.",
      "muted",
    ),
  );
  const form = el("form");
  const prompt = field(
    "Your idea",
    "textarea",
    "",
    "For example: a quick vegetarian dinner with lentils and carrots.",
  );
  prompt.input.required = true;
  add(form, prompt.wrap);
  formSubmit(form, "Create an idea", async () => {
    const draft = await api("/ai/recipe", "POST", {
      prompt: prompt.input.value,
    });
    recipeForm(draft, "ai");
  });
  add(root, form);
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
    ["needs", "Shopping needs"],
    ["in_store", "In-store trip"],
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
function groupedByCategory(lines) {
  const groups = new Map();
  [...lines]
    .sort(
      (a, b) =>
        (a.category || "Other").localeCompare(b.category || "Other") ||
        a.name.localeCompare(b.name),
    )
    .forEach((line) => {
      const category = line.category?.trim() || "Other";
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(line);
    });
  return groups;
}
function renderInStore(main) {
  const trip = state.storeTrip || {
    lines: [],
    checked_count: 0,
    total_count: 0,
  };
  add(
    main,
    el(
      "p",
      "Checked means picked up for this trip. It does not mark an item as bought or reduce what you have at home.",
      "callout",
    ),
    el(
      "p",
      (trip.checked_count || 0) +
        " of " +
        (trip.total_count || 0) +
        " needs picked up",
      "muted",
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
    return;
  }
  groupedByCategory(trip.lines).forEach((lines, category) => {
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
      add(
        detail,
        el(
          "small",
          (line.to_buy_quantity || "0") +
            " " +
            (line.unit || "") +
            " to pick up" +
            (line.origins?.length ? " · " + shoppingOrigins(line.origins) : ""),
        ),
      );
      add(row, check, detail);
      list.append(row);
    });
    add(section, list);
    main.append(section);
  });
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
  add(
    main,
    title(
      "One less thing to remember",
      "The shopping list.",
      "Plan needs from meals, regulars, and quick additions. At-home quantities reduce what you need to buy.",
      [
        state.shoppingPanel === "pantry"
          ? button("+ Add at-home item", () => inventoryForm(), "primary")
          : button("+ Add shopping need", () => itemForm(), "primary"),
      ],
    ),
    weekControls(),
    shoppingSections(),
  );
  if (state.shoppingPanel === "in_store") return renderInStore(main);
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
      el("p", "Plan a dinner to bring its ingredients here.", "muted"),
      button("Go to meal plan", () => navigate("week"), "quiet"),
    );
  else {
    const list = el("ul", null, "list");
    planLines.forEach((item) =>
      add(
        list,
        add(
          el("li"),
          add(
            el("div", null, "detail"),
            el("strong", item.name),
            el(
              "small",
              (item.required_quantity ?? item.quantity) +
                " " +
                item.unit +
                (item.available_quantity != null
                  ? " · " + item.available_quantity + " at home"
                  : "") +
                " · " +
                (Number(item.to_buy_quantity ?? item.quantity ?? 0) > 0
                  ? (item.to_buy_quantity ?? item.quantity) + " to buy"
                  : "covered at home") +
                " · " +
                (item.category || "Other") +
                (item.origins?.length
                  ? " · " + shoppingOrigins(item.origins)
                  : ""),
            ),
          ),
        ),
      ),
    );
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
    el("h2", "Your regulars"),
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
    el("h2", "Oda shopping"),
    el(
      "p",
      "Connect your Oda account to Kitchen from the Buy view. The in-store list works without Oda, and Kitchen does not place orders.",
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
    el("small", "Oda search result · not independently verified", "muted"),
  );
  add(card, details);
  return card;
}
function odaSearchForNeed(need) {
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
    expired ? "Review expired" : "Confirm cart additions",
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
  add(
    main,
    title(
      "Shopping with confidence",
      "Review what to buy.",
      "Choose Oda or an in-store trip. In-store checkoffs track pickup only; Oda cart additions need a separate review and confirmation.",
      [button("Refresh basket", refreshView, "quiet")],
    ),
  );
  const purchaseModes = el("div", null, "section-tabs purchase-modes");
  [
    ["oda", "Oda"],
    ["in_store", "In-store"],
  ].forEach(([mode, label]) => {
    const option = button(
      label,
      async () => {
        state.purchaseMode = mode;
        if (mode === "in_store")
          state.storeTrip = await api("/store-trip" + range());
        render();
      },
      state.purchaseMode === mode ? "primary" : "quiet",
    );
    if (state.purchaseMode === mode)
      option.setAttribute("aria-current", "page");
    purchaseModes.append(option);
  });
  add(main, purchaseModes);
  if (state.purchaseMode === "in_store") {
    add(
      main,
      el(
        "p",
        "This checklist tracks items picked up for this trip. It does not record purchases or change at-home quantities.",
        "callout",
      ),
    );
    renderInStore(main);
    return;
  }
  const odaSetup = add(
    el("section", null, "panel"),
    el("h2", "Connect Oda to Kitchen"),
  );
  if (state.oda?.connected) {
    add(
      odaSetup,
      el(
        "p",
        "Oda is connected to Kitchen. Search products for each need, choose a package size, then review cart additions before confirming.",
        "muted",
      ),
    );
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
  } else if (state.oda?.configured) {
    add(
      odaSetup,
      el(
        "p",
        "Connect your Oda account securely to Kitchen. Kitchen will not place an order.",
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
        "Oda connection is not configured for this Kitchen yet. Ask the household administrator to finish setup here.",
        "muted",
      ),
    );
    const unavailable = button("Oda setup required", () => {}, "quiet");
    unavailable.disabled = true;
    add(odaSetup, unavailable);
  }
  add(main, odaSetup);
  add(
    main,
    el(
      "p",
      basket.ready_for_review
        ? "Basket needs are ready for review."
        : "Basket has items that need attention before review.",
      basket.ready_for_review ? "callout" : "callout offline",
    ),
  );
  if (basket.total_cost != null)
    add(
      main,
      el(
        "p",
        "Product total: " +
          basket.total_cost +
          (basket.currency ? " " + basket.currency : "") +
          (basket.price_complete
            ? ""
            : " · incomplete because some prices are unknown"),
        "callout",
      ),
    );
  else if (basket.price_complete === false)
    add(
      main,
      el(
        "p",
        "Product total is incomplete because some prices are unknown.",
        "callout",
      ),
    );
  if (basket.issues?.length) {
    const issues = add(
      el("section", null, "callout"),
      el("strong", "Needs attention"),
    );
    const list = el("ul");
    basket.issues.forEach((issue) =>
      add(
        list,
        el(
          "li",
          typeof issue === "string"
            ? issue
            : issue.message || JSON.stringify(issue),
        ),
      ),
    );
    add(issues, list);
    add(main, issues);
  }
  if (!basket.needs?.length) {
    add(
      main,
      empty(
        "Your basket is empty",
        "Plan meals or add shopping needs first. Oda product references appear here when the Kitchen connection supports them.",
        button("Open shopping list", () => navigate("shopping"), "primary"),
      ),
    );
    add(
      main,
      add(
        el("section", null, "panel"),
        el("h2", "Oda product search"),
        el(
          "p",
          "Connect Oda to Kitchen above, then search products from a shopping need. Kitchen does not place orders.",
          "muted",
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
    add(
      heading,
      add(
        el("div"),
        el("p", "NEED", "eyebrow"),
        el("h2", need.name || "Shopping need"),
        el("p", need.quantity + " " + (need.unit || ""), "need-quantity"),
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
      const allocation = el(
        "p",
        "Allocated: " +
          (entry.allocated_quantity ?? "0") +
          " " +
          (need.unit || "") +
          " · Remaining: " +
          (entry.remaining_quantity ?? "unknown") +
          " " +
          (need.unit || "") +
          " · Extra: " +
          (entry.overbuy_quantity ?? "unknown") +
          " " +
          (need.unit || ""),
        "muted",
      );
      add(section, allocation);
    }
    const selections = entry.selections || [];
    if (!selections.length)
      add(
        section,
        el(
          "p",
          state.oda?.connected
            ? "No Oda product selected yet. Search below to review product options."
            : "Connect Oda to Kitchen before searching for products.",
          "callout",
        ),
      );
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
    if (entry.ready === false)
      add(
        section,
        el("p", "This need is not ready for review yet.", "form-error"),
      );
    add(needs, section);
  });
  add(main, needs);
  const proposalRequest = odaCartProposalRequest(basket);
  if (proposalRequest.additions.length || proposalRequest.invalidSelections) {
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
    else if (!state.oda?.connected)
      add(
        cartPanel,
        el(
          "p",
          "Connect Oda to Kitchen before preparing cart additions.",
          "muted",
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
  add(
    main,
    add(
      el("section", null, "panel"),
      el("h2", "Orders and payment"),
      el(
        "p",
        "Kitchen does not place orders or handle payment. Review product references here and complete any purchase on Oda.",
        "muted",
      ),
    ),
  );
}
function feedbackForm(recipe) {
  const root = openDialog(recipe ? "Rate this recipe" : "Share feedback");
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
          "AI recipe ideas: " +
            (state.integrations.ai?.configured
              ? "configured"
              : "setup required") +
            ". Oda account connection and setup are managed from Buy in Kitchen.",
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
  if (
    ["week", "recipes", "shopping", "basket", "settings"].includes(view) &&
    view !== state.view
  ) {
    state.view = view;
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
    [state.household, state.recipes, state.integrations] = await Promise.all([
      api("/household"),
      api("/recipes"),
      api("/integrations"),
    ]);
    const requested = location.hash.slice(1);
    state.view = !state.household.name
      ? "settings"
      : odaResult
        ? "basket"
        : ["week", "recipes", "shopping", "basket", "settings"].includes(
              requested,
            )
          ? requested
          : "week";
    await refreshView();
    if (odaResult) {
      history.replaceState(
        null,
        "",
        location.pathname + (state.household.name ? "#basket" : ""),
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
