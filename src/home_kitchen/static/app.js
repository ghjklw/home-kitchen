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
  tripCompletionReview: null,
  tripCompletionRetry: null,
  oda: null,
  odaStatusError: null,
  odaSearch: {},
  odaMedia: {},
  basketMatchPlanKey: null,
  basketMatchPlanEpoch: 0,
  basketMatchNeedVersions: {},
  basketMatchPages: {},
  basketMatchCurrentPage: {},
  basketMatchBusy: {},
  basketMatchErrors: {},
  basketMatchObservedAt: {},
  basketMatchJobs: {},
  basketBatchReviewJobId: null,
  basketBatchChoices: {},
  basketBatchSaving: false,
  basketBatchSaveError: "",
  basketBatchNeedsRefresh: false,
  basketBatchNeedsCorrection: false,
  basketMatchStartSequence: 0,
  basketMatchStarted: false,
  basketMatchGlobalError: "",
  basketMatchGlobalRefreshing: false,
  basketMatchConnected: null,
  basketMatchSkippedNeedCount: 0,
  odaManualSearchOpen: {},
  cartProposalLoading: false,
  cartConfirming: false,
  cartProposalHandled: false,
  cartProposalBlocked: false,
  shoppingPanel: "single",
  shoppingGrouping: "category",
  shoppingLastRefreshedAt: null,
  shoppingRefreshInFlight: false,
  shoppingRefreshPending: false,
  shoppingRefreshLastEventAt: 0,
  shoppingRefreshRequestCount: 0,
  shoppingLoadSequence: 0,
  shoppingWriteInFlight: 0,
  basket: null,
  items: [],
  regular: [],
  integrations: [],
  integrationsError: null,
  settingsRefreshInFlight: false,
  week: localDate(new Date()),
  selectedDate: localDate(new Date()),
  recipeCollectionJob: null,
  recipeCollectionStatusNode: null,
  recipeCollectionPollGeneration: 0,
};
window.KitchenTripCompletion = {
  purchaseSourceField(kind) {
    return kind === "meal" ? "close_meal_ids" : kind === "capture" ? "close_capture_ids" : kind === "regular" ? "regular_ids" : null;
  },
  exactProductPurchase(line, selectionValue, packageCount) {
    if (selectionValue === "" || selectionValue === null || selectionValue === undefined) return null;
    const selectionIndex = Number(selectionValue);
    if (!Number.isInteger(selectionIndex) || selectionIndex < 0) return null;
    const selection = line?.selected_products?.[selectionIndex];
    const packages = Number(packageCount);
    if (!selection || !Number.isInteger(packages) || packages < 1 || packages > 99) return null;
    return { provider: selection.product.provider, product_id: selection.product.product_id, packages };
  },
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
function rememberRecipeCollectionJob(job) {
  state.recipeCollectionJob = job;
  try {
    if (job?.job_id) sessionStorage.setItem("recipeCollectionJobId", job.job_id);
    else sessionStorage.removeItem("recipeCollectionJobId");
  } catch {}
  updateRecipeCollectionStatus();
}
function updateRecipeCollectionStatus() {
  const host = state.recipeCollectionStatusNode;
  const job = state.recipeCollectionJob;
  if (!host?.isConnected) return;
  if (!job) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const summary = window.KitchenRecipeCollections.progress(job);
  const counts = window.KitchenRecipeCollections.completionCounts(job);
  const partial = ["completed", "cancelled", "failed"].includes(job.status)
    ? ` · ${counts.imported} nye importert · ${counts.already} fantes fra før · ${counts.failed} feilet${job.status === "cancelled" ? " · allerede importerte oppskrifter beholdes" : ""}`
    : "";
  const label = host.querySelector("[data-collection-summary]");
  if (label) label.textContent = summary + partial;
  const cancel = host.querySelector("[data-collection-cancel]");
  if (cancel) cancel.hidden = !["previewing", "importing"].includes(job.status);
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
      /^\/meal-plan-proposals\/[^/]+\/confirm$/.test(path)
        ? "Fikk ikke svar på bekreftelsen. Sjekk tilkoblingen og prøv igjen."
        : "Fikk ikke kontakt med kjøkkenet. Sjekk tilkoblingen og prøv igjen. Endringene dine er ikke lagret.",
    );
  }
  if (!response.ok) {
    let data;
    let detail;
    try {
      data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : data.message;
      if (path === "/basket/selections/batch" && Array.isArray(data.detail)) {
        detail = data.detail.map((issue) => {
          const location = Array.isArray(issue.loc) ? issue.loc.slice(1).join(".") : "";
          return [location, issue.msg || "Invalid value"].filter(Boolean).join(": ");
        }).join("; ");
      }
    } catch {}
    const isIntegration = path.startsWith("/integrations") || path.startsWith("/api/integrations");
    const error = new Error(
      (response.status === 401 || response.status === 403) && !isIntegration
        ? "Logg inn via husholdningens godkjente tilgangslenke, og prøv igjen."
        : response.status === 409 && path === "/meals"
          ? "Dette måltidstidspunktet er allerede brukt. Velg et annet tidspunkt, eller fjern det eksisterende måltidet først. Skjemaet er fortsatt åpent."
          : detail ||
          (response.status === 503
            ? path === "/ai/plan"
              ? "KI-måltidsplanlegging krever at husholdningsadministratoren setter opp en leverandør."
              : "Denne tjenesten må settes opp før den kan brukes."
            : response.status === 422 && path === "/ai/plan"
              ? "Det finnes ingen oppskrifter å planlegge med. Legg til en oppskrift og prøv igjen."
              : isIntegration && (response.status === 401 || response.status === 403)
                ? "Ugyldig påloggingsinformasjon eller manglende tilgang. Vennligst sjekk detaljene og prøv igjen."
                : "Forespørselen kunne ikke fullføres. Prøv igjen."),
    );
    error.status = response.status;
    error.detail = detail;
    error.path = path;
    if (Number.isFinite(data?.retry_after_seconds))
      error.retryAfterSeconds = Math.max(0, data.retry_after_seconds);
    throw error;
  }
  return response.status === 204 ? null : response.json();
}
function integrationById(id) {
  if (Array.isArray(state.integrations))
    return state.integrations.find((integration) => integration.id === id) || null;
  return state.integrations?.[id] || null;
}
function recipeAssistantConfigured() {
  const integration = integrationById("anthropic") || integrationById("ai");
  return (
    ["connected", "configured"].includes(integration?.status) ||
    integration?.configured === true
  );
}
function connectionSettingsButton(label = "Administrer tilkoblinger i innstillinger") {
  return button(
    label,
    () => {
      if ($("#modal")?.open) {
        const url = new URL(location.href);
        url.hash = "settings";
        window.open(url.href, "_blank", "noopener,noreferrer");
        return;
      }
      navigate("settings");
    },
    "quiet",
  );
}
async function loadConnectionStatus() {
  state.settingsRefreshInFlight = true;
  const [integrationsResult, odaResult] = await Promise.allSettled([
    api("/integrations"),
    api("/oda/status"),
  ]);
  const snapshot = window.KitchenSettingsView.connectionStatusSnapshot(
    integrationsResult,
    odaResult,
  );
  state.integrations = snapshot.integrations;
  state.oda = snapshot.oda;
  state.integrationsError = snapshot.integrationsError;
  state.odaStatusError = snapshot.odaStatusError;
  state.settingsRefreshInFlight = false;
  return snapshot;
}
async function refreshConnectionSettings(event) {
  const control = event?.currentTarget;
  const section = $(".connection-settings");
  const keepFocus = document.activeElement === control;
  if (control) {
    control.disabled = true;
    control.textContent = "Oppdaterer …";
  }
  await loadConnectionStatus();
  const replacement = renderConnectionSettings();
  if (section) section.replaceWith(replacement);
  if (keepFocus)
    replacement.querySelector("#settings-connection-refresh")?.focus({ preventScroll: true });
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
function startOfWeek(date) {
  const d = localDate(date);
  const dayOfWeek = d.getDay();
  const diffToMonday = dayOfWeek === 0 ? -6 : 1 - dayOfWeek;
  d.setDate(d.getDate() + diffToMonday);
  return d;
}
function formatRecipeBadge(recipe) {
  if (window.KitchenRecipeEditor?.formatRecipeBadge) {
    return window.KitchenRecipeEditor.formatRecipeBadge(recipe);
  }
  if (!recipe || typeof recipe !== "object") return "";
  const typeLabels = {
    middag: "Middag",
    dessert: "Dessert",
    frokost: "Frokost",
    lunsj: "Lunsj",
    tilbehør: "Tilbehør",
    bakst: "Bakst",
    annet: "Annet",
  };
  const prepLabels = {
    rask: "Rask",
    normal: "Normal",
    langtid: "Langtid",
    batch_cooking: "Batch-cooking",
  };
  const typeText = typeLabels[recipe.recipe_type] || "";
  const prepCatText = prepLabels[recipe.prep_category] || "";
  const rawMinutes = recipe.prep_time_minutes;
  const minutes = rawMinutes != null && rawMinutes !== "" ? Number(rawMinutes) : null;
  const timeText = minutes != null && Number.isFinite(minutes) && minutes > 0 ? `${minutes} min` : "";
  let prepText = "";
  if (prepCatText && timeText) prepText = `${prepCatText} (${timeText})`;
  else if (prepCatText) prepText = prepCatText;
  else if (timeText) prepText = timeText;
  return [typeText, prepText].filter(Boolean).join(" · ");
}
function isOdaSourceUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== "string") return false;
  try {
    const host = new URL(rawUrl).hostname.toLowerCase();
    return host === "oda.com" || host.endsWith(".oda.com");
  } catch {
    return false;
  }
}
function isOdaRecipe(recipe) {
  return (
    recipe?.source === "imported" &&
    (recipe?.provenance?.provider === "oda" || isOdaSourceUrl(recipe?.source_url))
  );
}
function formatRecipeSourceBadge(recipe) {
  if (window.KitchenRecipeEditor?.formatRecipeSourceBadge) {
    return window.KitchenRecipeEditor.formatRecipeSourceBadge(recipe);
  }
  if (!recipe || typeof recipe !== "object") return "";
  if (recipe.source === "cookidoo") return "Cookidoo";
  if (recipe.source === "ai") return "KI-idé";
  if (recipe.source === "imported") {
    if (isOdaRecipe(recipe)) return "Oda";
    if (
      recipe.provenance?.kind === "photo" ||
      recipe.photo_source?.kind === "photo_book" ||
      Boolean(recipe.photo_source?.id)
    ) {
      return "Fra bilde";
    }
    return "Fra en oppskriftslenke";
  }
  return "Fra ditt kjøkken";
}
function matchesRecipeSourceFilter(recipe, filterValue) {
  if (!filterValue) return true;
  const source = recipe?.source || "manual";
  if (filterValue === "oda") return isOdaRecipe(recipe);
  if (filterValue === "imported") return source === "imported" && !isOdaRecipe(recipe);
  return source === filterValue;
}
function renderRecipeStatsBadges(recipe) {
  const formatter = window.KitchenRecipeQuickFeedback?.formatRecipeStatsBadges;
  if (typeof formatter !== "function") return null;
  const labels = formatter(recipe?.activity_stats || recipe?.stats);
  if (!labels.length) return null;
  const wrap = el("div", null, "recipe-stats-badges");
  labels.forEach((label) => {
    add(wrap, el("span", label, "badge recipe-stat-badge"));
  });
  return wrap;
}
function day(offset) {
  const date = new Date(state.week);
  date.setDate(date.getDate() + offset);
  return date;
}
function range() {
  return "?start=" + iso(day(0)) + "&end=" + iso(day(6));
}
function agendaMeals(meals, date) {
  return meals.filter((meal) => meal.date === date);
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
  if (iso(state.selectedDate) === iso(state.week)) url.searchParams.delete("day");
  else url.searchParams.set("day", iso(state.selectedDate));
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}
function dateLabel(date, options) {
  return date.toLocaleDateString("nb-NO", options);
}
function formatUiCount(value) {
  return new Intl.NumberFormat("nb-NO").format(value);
}
function formatServings(value) {
  return formatUiCount(value) + (Number(value) === 1 ? " porsjon" : " porsjoner");
}
function formatUiQuantity(value) {
  return window.KitchenShoppingView.formatQuantityForDisplay(String(value ?? ""));
}
function formatUiAccessibleQuantity(value) {
  return window.KitchenShoppingView.formatQuantityForAccessibility(String(value ?? ""));
}
function formatUiNeedQuantity(need, accessible = false) {
  return window.KitchenShoppingView.formatNeedQuantity(
    need?.quantity,
    need?.unit,
    accessible,
    need?.approximate === true,
  );
}
function formatUiTime(value) {
  if (typeof value !== "string") return "";
  const match = /^(\d{2}:\d{2})(?::00)?$/u.exec(value);
  return match ? match[1] : value;
}
function recipeOriginLabel(value) {
  return ({
    imported: "en importert oppskrift",
    manual: "en manuelt oppgitt oppskrift",
    ai: "en KI-oppskrift",
    oda: "en Oda-oppskrift",
  })[value] || value;
}
function ingredientQuantityText(item) {
  const quantity = window.KitchenShoppingView.formatNeedQuantity(
    item.quantity,
    item.unit,
    false,
  );
  return [quantity, item.name].filter(Boolean).join(" ");
}
function purchaseEstimateText(estimate) {
  if (
    !estimate ||
    estimate.quantity == null ||
    !String(estimate.unit || "").trim() ||
    !window.KitchenShoppingView.isPositiveDecimal(String(estimate.quantity))
  ) return "";
  return (estimate.approximate === true ? "Anslått handlemengde: " : "Omregnet handlemengde: ") +
    formatUiQuantity(estimate.quantity) + " " + estimate.unit;
}
function renderPurchaseEvidence(item) {
  if (!item || !(item.approximate === true || item.provenance || item.source_url)) return null;
  const details = el("details", null, "purchase-evidence muted");
  add(details, el("summary", "Om mengdegrunnlaget"));
  if (item.approximate === true) add(details, el("p", "Handlemengden er omtrentlig.", "muted"));
  if (item.provenance) add(details, el("p", String(item.provenance), "muted"));
  const sourceUrl = window.KitchenBasketMatchView.sourceUrl(item.source_url);
  if (sourceUrl) {
    const source = el("a", "Se kilden for handlemengden ↗");
    source.href = sourceUrl;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    add(details, source);
  }
  return details;
}
function mealSlotLabel(slot) {
  return ({ breakfast: "Frokost", lunch: "Lunsj", dinner: "Middag" })[slot] || slot || "";
}
function mealAgendaMetadata(meal) {
  return [
    meal.label || mealSlotLabel(meal.slot),
    formatUiTime(meal.scheduled_time),
    formatServings(meal.servings),
  ]
    .filter(Boolean)
    .join(" · ");
}
function buildMealWriteRequest(existingMeal, values) {
  const payload = {
    date: values.date,
    label: values.label.trim() || null,
    scheduled_time: values.scheduled_time || null,
    recipe_id: values.recipe_id,
    servings: Number(values.servings),
  };
  return existingMeal
    ? {
        path: "/meals/" + encodeURIComponent(existingMeal.id),
        method: "PUT",
        payload,
      }
    : { path: "/meals", method: "POST", payload };
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
    frame.replaceChildren(el("span", "Bildet er ikke tilgjengelig", "image-fallback"));
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
    state.selectedDate.setDate(state.selectedDate.getDate() + amount);
    state.week = startOfWeek(state.week);
    syncPlannerUrl();
    await refreshView();
  };
  const previous = button("Forrige uke", () => change(-7));
  previous.ariaLabel = "Forrige uke";
  previous.dataset.focusKey = "planner-previous-week";
  const next = button("Neste uke", () => change(7));
  next.ariaLabel = "Neste uke";
  next.dataset.focusKey = "planner-next-week";
  const jump = el("label", null, "date-jump period-jump");
  const jumpInput = el("input");
  jumpInput.type = "date";
  jumpInput.value = iso(state.selectedDate);
  jumpInput.setAttribute("aria-label", "Gå til dato");
  jumpInput.dataset.focusKey = "planner-date-jump";
  jump.append(el("span", "Gå til dato"), jumpInput);
  jumpInput.addEventListener("change", async () => {
    const date = parseDate(jumpInput.value);
    if (!date) return;
    state.selectedDate = new Date(date);
    state.week = startOfWeek(date);
    syncPlannerUrl();
    await refreshView();
  });
  const thisWeek = button(
    "Denne uken",
    async () => {
      state.selectedDate = localDate(new Date());
      state.week = startOfWeek(new Date());
      syncPlannerUrl();
      await refreshView();
    },
    "quiet small today-period",
  );
  thisWeek.ariaLabel = "Denne uken";
  thisWeek.dataset.focusKey = "planner-this-week";
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
    thisWeek,
    jump,
  );
  return row;
}
function selectedDateControls() {
  const row = el("div", null, "toolbar period-toolbar selected-date-toolbar");
  const change = async (amount) => {
    state.selectedDate.setDate(state.selectedDate.getDate() + amount);
    if (state.selectedDate < state.week) state.week.setDate(state.week.getDate() - 7);
    else if (state.selectedDate > day(6)) state.week.setDate(state.week.getDate() + 7);
    syncPlannerUrl();
    await refreshView();
  };
  const previous = button("←", () => change(-1));
  previous.ariaLabel = "Forrige dag";
  previous.dataset.focusKey = "planner-previous-day";
  const next = button("→", () => change(1));
  next.ariaLabel = "Neste dag";
  next.dataset.focusKey = "planner-next-day";
  const jump = el("label", null, "date-jump");
  const jumpInput = el("input");
  jumpInput.type = "date";
  jumpInput.value = iso(state.selectedDate);
  jumpInput.setAttribute("aria-label", "Velg planleggingsdato");
  jumpInput.dataset.focusKey = "planner-selected-date";
  jump.append(el("span", "Velg dato"), jumpInput);
  jumpInput.addEventListener("change", async () => {
    const date = parseDate(jumpInput.value);
    if (!date) return;
    state.week = new Date(date);
    state.selectedDate = new Date(date);
    syncPlannerUrl();
    await refreshView();
  });
  const today = button(
    "I dag",
    async () => {
      state.week = localDate(new Date());
      state.selectedDate = new Date(state.week);
      syncPlannerUrl();
      await refreshView();
    },
    "quiet small today-period",
  );
  today.dataset.focusKey = "planner-today";
  add(
    row,
    previous,
    el(
      "span",
      dateLabel(state.selectedDate, {
        weekday: "long",
        month: "short",
        day: "numeric",
        year: "numeric",
      }),
      "week-range selected-date-label",
    ),
    next,
    today,
    jump,
  );
  return row;
}
function openDialog(heading) {
  const modal = $("#modal");
  if (modal.open) modal.close();
  modal.dataset.kind = "";
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
  root.querySelector("button").ariaLabel = "Lukk dialogvindu";
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
function formSubmit(form, text, action, { settingsOnFailure = false } = {}) {
  const error = el("p", "", "form-error");
  error.setAttribute("role", "alert");
  const settingsLink = settingsOnFailure
    ? connectionSettingsButton("Se over tilkoblingene i innstillingene")
    : null;
  if (settingsLink) settingsLink.hidden = true;
  const submit = el("button", text, "primary");
  submit.type = "submit";
  add(form, error, settingsLink, add(el("div", null, "actions"), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    error.textContent = "";
    try {
      await action();
    } catch (err) {
      error.textContent = err.message;
      if (settingsLink) settingsLink.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
  return error;
}
async function mutate(path, method, body, message) {
  await api(path, method, body);
  $("#modal").close();
  await refreshView();
  if (message) notify(message);
}
function shouldRefreshShopping(view, visibility, modalOpen, formOpen) {
  return (
    view === "shopping" &&
    visibility !== "hidden" &&
    !modalOpen &&
    !formOpen
  );
}
function isCurrentShoppingLoad(view, activeSequence, requestSequence, currentPeriod, requestPeriod) {
  return view === "shopping" && activeSequence === requestSequence && currentPeriod === requestPeriod;
}
function shoppingRefreshLabel() {
  if (state.shoppingRefreshInFlight) return "Oppdaterer …";
  if (!state.shoppingLastRefreshedAt) return "Ikke oppdatert ennå";
  const time = new Intl.DateTimeFormat("nb-NO", {
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(state.shoppingLastRefreshedAt));
  return "Oppdatert kl. " + time;
}
function updateShoppingRefreshControl() {
  const control = $("#shopping-refresh");
  const status = $("#shopping-refresh-status");
  if (control) {
    control.disabled = state.shoppingRefreshInFlight;
    control.textContent = state.shoppingRefreshInFlight ? "Oppdaterer …" : "Oppdater";
    control.setAttribute(
      "aria-label",
      state.shoppingRefreshInFlight
        ? "Oppdaterer delt handleliste"
        : "Oppdater delt handleliste",
    );
  }
  if (status) status.textContent = shoppingRefreshLabel();
}
function queuePendingShoppingRefresh() {
  if (!state.shoppingRefreshPending) return;
  setTimeout(() => {
    if (state.shoppingRefreshPending) void refreshShoppingIfSafe(true);
  }, 0);
}
async function refreshShoppingIfSafe(force = false) {
  if (state.view !== "shopping") return;
  if (
    !shouldRefreshShopping(
      state.view,
      document.visibilityState,
      $("#modal").open,
      Boolean($("#main form")),
    ) || state.shoppingWriteInFlight > 0
  ) {
    state.shoppingRefreshPending = true;
    return;
  }
  if (state.shoppingRefreshInFlight || state.shoppingRefreshRequestCount > 0) {
    state.shoppingRefreshPending = true;
    return;
  }
  const now = Date.now();
  if (!force && now - state.shoppingRefreshLastEventAt < 1500) return;
  state.shoppingRefreshLastEventAt = now;
  state.shoppingRefreshPending = false;
  state.shoppingRefreshInFlight = true;
  updateShoppingRefreshControl();
  try {
    await refreshView();
  } finally {
    state.shoppingRefreshInFlight = false;
    updateShoppingRefreshControl();
    queuePendingShoppingRefresh();
  }
}
async function refreshView() {
  const matchingVisible = state.view === "shopping" && ["single", "oda"].includes(state.shoppingPanel);
  const refreshingShopping = state.view === "shopping";
  const shoppingLoadSequence = refreshingShopping ? ++state.shoppingLoadSequence : null;
  const shoppingRange = refreshingShopping ? range() : null;
  const shoppingLoadIsCurrent = () =>
    !refreshingShopping || isCurrentShoppingLoad(
      state.view,
      state.shoppingLoadSequence,
      shoppingLoadSequence,
      range(),
      shoppingRange,
    );
  if (refreshingShopping) {
    state.shoppingRefreshPending = false;
    state.shoppingRefreshRequestCount += 1;
  }
  pauseBasketMatchPolling();
  try {
    if (state.view === "week") state.meals = await api("/meals" + range());
    if (state.view === "recipes") state.recipes = await api("/recipes?include_stats=true");
    if (state.view === "settings") await loadConnectionStatus();
    if (state.view === "shopping") {
      const results = await Promise.all([
        api("/shopping" + shoppingRange),
        api("/purchase-plan" + shoppingRange),
        api("/items"),
        api("/regular-items"),
        api("/inventory"),
      ]);
      if (!shoppingLoadIsCurrent()) return false;
      [
        state.shopping,
        state.purchasePlan,
        state.items,
        state.regular,
        state.inventory,
      ] = results;
      if (state.shoppingPanel === "oda") {
        const [basket, oda] = await Promise.all([
          api("/basket" + shoppingRange),
          api("/oda/status"),
        ]);
        if (!shoppingLoadIsCurrent()) return false;
        state.basket = basket;
        state.oda = oda;
        if (state.oda?.connected) ensureBasketMatches();
        else resetBasketMatching();
      } else if (state.shoppingPanel === "single") {
        const [storeTrip, meals, basket, oda] = await Promise.all([
          api("/store-trip" + shoppingRange),
          api("/meals" + shoppingRange),
          api("/basket" + shoppingRange),
          api("/oda/status"),
        ]);
        if (!shoppingLoadIsCurrent()) return false;
        [state.storeTrip, state.meals, state.basket, state.oda] = [storeTrip, meals, basket, oda];
        if (state.oda?.connected) ensureBasketMatches();
        else resetBasketMatching();
      } else if (state.shoppingPanel === "in_store") {
        const storeTrip = await api("/store-trip" + shoppingRange);
        if (!shoppingLoadIsCurrent()) return false;
        state.storeTrip = storeTrip;
      } else if (state.shoppingPanel === "complete_trip") {
        const completionReview = await api("/store-trip/completion-review" + shoppingRange);
        if (!shoppingLoadIsCurrent()) return false;
        if (state.tripCompletionRetry?.token !== completionReview.review_token)
          state.tripCompletionRetry = null;
        state.tripCompletionReview = completionReview;
      }
      if (!shoppingLoadIsCurrent()) return false;
      state.shoppingLastRefreshedAt = Date.now();
    }
    if (!shoppingLoadIsCurrent()) return false;
    render();
    return true;
  } catch (error) {
    if (!shoppingLoadIsCurrent()) return false;
    notify(error.message, true);
    if (matchingVisible)
      Object.values(state.basketMatchJobs).forEach((record) =>
        scheduleBasketMatchPoll(record),
      );
    return false;
  } finally {
    if (!matchingVisible) pauseBasketMatchPolling();
    if (refreshingShopping) {
      state.shoppingRefreshRequestCount = Math.max(
        0,
        state.shoppingRefreshRequestCount - 1,
      );
      if (!state.shoppingRefreshRequestCount && !state.shoppingRefreshInFlight)
        queuePendingShoppingRefresh();
    }
  }
}
function pauseBasketMatchPolling() {
  Object.values(state.basketMatchJobs).forEach((record) => {
    if (record.timer) clearTimeout(record.timer);
    record.timer = null;
    record.pollVersion = (record.pollVersion || 0) + 1;
  });
}
function resetBasketMatching() {
  pauseBasketMatchPolling();
  const modal = $("#modal");
  if (modal.open && modal.dataset.kind === "basket-batch-review") modal.close();
  state.basketMatchPlanEpoch += 1;
  state.basketMatchPlanKey = null;
  state.basketMatchNeedVersions = {};
  state.basketMatchPages = {};
  state.basketMatchCurrentPage = {};
  state.basketMatchBusy = {};
  state.basketMatchErrors = {};
  state.basketMatchObservedAt = {};
  state.basketMatchJobs = {};
  state.basketBatchReviewJobId = null;
  state.basketBatchChoices = {};
  state.basketBatchSaving = false;
  state.basketBatchSaveError = "";
  state.basketBatchNeedsRefresh = false;
  state.basketBatchNeedsCorrection = false;
  state.basketMatchStarted = false;
  state.basketMatchGlobalError = "";
  state.basketMatchGlobalRefreshing = false;
  state.basketMatchSkippedNeedCount = 0;
}
function currentBasketMatchDates() {
  return { start: iso(day(0)), end: iso(day(6)) };
}
function currentBasketMatchNeeds() {
  return (state.basket?.needs || [])
    .map((entry) => entry.need)
    .filter((need) => need?.id);
}
function isCurrentBasketMatchRecord(record) {
  const { start, end } = currentBasketMatchDates();
  return (
    record.planEpoch === state.basketMatchPlanEpoch &&
    record.planKey === state.basketMatchPlanKey &&
    record.start === start &&
    record.end === end
  );
}
function ensureBasketMatches() {
  if (!state.oda?.connected) {
    if (state.basketMatchConnected !== false) resetBasketMatching();
    state.basketMatchConnected = false;
    return;
  }
  if (state.basketMatchConnected !== true) {
    resetBasketMatching();
    state.basketMatchConnected = true;
  }
  const { start, end } = currentBasketMatchDates();
  const needs = currentBasketMatchNeeds();
  const planKey = window.KitchenBasketMatchView.planKey(start, end, needs);
  if (planKey !== state.basketMatchPlanKey) {
    resetBasketMatching();
    state.basketMatchPlanKey = planKey;
    state.basketMatchNeedVersions = Object.fromEntries(
      needs.map((need) => [String(need.id), 0]),
    );
  }
  if (!needs.length) return;
  if (!state.basketMatchStarted && !state.basketMatchGlobalError) {
    void startBasketMatchJob({ initial: true, renderAfter: state.shoppingPanel === "single" });
    return;
  }
  Object.values(state.basketMatchJobs).forEach((record) => {
    if (record.jobId && ["queued", "running"].includes(record.status))
      scheduleBasketMatchPoll(record);
  });
}
function applyBasketMatchSnapshot(job, record) {
  if (!isCurrentBasketMatchRecord(record)) return;
  state.basketMatchSkippedNeedCount = job.skipped_need_count || 0;
  if (
    job.status === "complete" &&
    record.page === 1 &&
    record.needIds.length === currentBasketMatchNeeds().length &&
    record.needIds.every(
      (id) => record.needVersions[id] === state.basketMatchNeedVersions[id],
    ) &&
    currentBasketMatchNeeds().length > 0
  ) {
    state.basketBatchReviewJobId = job.job_id || record.jobId;
    state.basketBatchNeedsRefresh = false;
  }
  state.basketMatchPages = window.KitchenBasketMatchView.mergeNeedPages(
    state.basketMatchPages,
    (job.needs || []).map((need) => ({
      ...need,
      ...(["pending", "searching"].includes(need.status)
        ? {}
        : { match_job_id: job.job_id || record.jobId }),
    })),
    state.basketMatchNeedVersions,
    record.needVersions,
  );
  (job.needs || []).forEach((need) => {
    const id = String(need.need_id);
    if (record.needVersions[id] !== state.basketMatchNeedVersions[id]) return;
    if (job.status === "complete" && ["matched", "no_results"].includes(need.status) &&
        typeof job.observed_at === "string" && !Number.isNaN(new Date(job.observed_at).valueOf())) {
      state.basketMatchObservedAt[id] ||= {};
      state.basketMatchObservedAt[id][need.page || 1] = job.observed_at;
    }
    const terminal = !["pending", "searching"].includes(need.status);
    if (terminal) {
      state.basketMatchCurrentPage[id] = need.page || 1;
      delete state.basketMatchBusy[id];
      delete state.basketMatchErrors[id];
    } else if (!state.basketMatchBusy[id]) {
      state.basketMatchBusy[id] = { page: need.page || 1 };
    }
  });
  if (job.status === "failed" && job.detail) {
    const allNeeds = currentBasketMatchNeeds();
    if (record.needIds.length === allNeeds.length)
      state.basketMatchGlobalError = job.detail;
  }
}
function scheduleBasketMatchPoll(record) {
  if (
    !record.jobId ||
    !["queued", "running"].includes(record.status) ||
    state.view !== "shopping" ||
    !["single", "oda"].includes(state.shoppingPanel) ||
    !navigator.onLine ||
    !isCurrentBasketMatchRecord(record) ||
    record.timer
  )
    return;
  const version = record.pollVersion || 0;
  record.timer = setTimeout(() => {
    record.timer = null;
    void pollBasketMatchJob(record, version);
  }, 800);
}
async function pollBasketMatchJob(record, version) {
  if (
    version !== record.pollVersion ||
    state.basketMatchJobs[record.key] !== record ||
    !isCurrentBasketMatchRecord(record)
  )
    return;
  try {
    const job = await api(
      "/basket/match-jobs/" + encodeURIComponent(record.jobId),
    );
    if (
      version !== record.pollVersion ||
      state.basketMatchJobs[record.key] !== record ||
      !isCurrentBasketMatchRecord(record)
    )
      return;
    record.status = job.status;
    applyBasketMatchSnapshot(job, record);
    if (["queued", "running"].includes(job.status)) scheduleBasketMatchPoll(record);
    else delete state.basketMatchJobs[record.key];
    render();
  } catch (error) {
    if (
      version !== record.pollVersion ||
      state.basketMatchJobs[record.key] !== record ||
      !isCurrentBasketMatchRecord(record)
    )
      return;
    record.needIds.forEach((id) => {
      if (record.needVersions[id] !== state.basketMatchNeedVersions[id]) return;
      delete state.basketMatchBusy[id];
      state.basketMatchErrors[id] = error.message;
    });
    if (record.needIds.length === currentBasketMatchNeeds().length)
      state.basketMatchGlobalError = error.message;
    delete state.basketMatchJobs[record.key];
    render();
  }
}
async function startBasketMatchJob({
  needIds = null,
  page = 1,
  forceRefresh = false,
  initial = false,
  renderAfter = true,
} = {}) {
  const { start, end } = currentBasketMatchDates();
  const needs = currentBasketMatchNeeds();
  const allIds = needs.map((need) => String(need.id));
  const targetIds = needIds ? needIds.map(String).filter((id) => allIds.includes(id)) : allIds;
  if (!state.oda?.connected || !targetIds.length) return;
  if (state.basketMatchPlanKey !== window.KitchenBasketMatchView.planKey(start, end, needs))
    return;
  if (!needIds) {
    state.basketBatchReviewJobId = null;
    state.basketBatchNeedsRefresh = false;
    state.basketBatchNeedsCorrection = false;
    if (forceRefresh) state.basketBatchSaveError = "";
  }

  const needVersions = {};
  targetIds.forEach((id) => {
    const version = state.basketMatchNeedVersions[id] || 0;
    state.basketMatchNeedVersions[id] = initial ? version : version + 1;
    needVersions[id] = state.basketMatchNeedVersions[id];
    state.basketMatchBusy[id] = { page };
    delete state.basketMatchErrors[id];
  });
  if (!needIds) state.basketMatchGlobalError = "";
  state.basketMatchStarted = true;
  state.basketMatchGlobalRefreshing = !needIds && !initial && forceRefresh;
  const sequence = ++state.basketMatchStartSequence;
  const tempKey = "starting-" + sequence;
  const record = {
    key: tempKey,
    jobId: null,
    planEpoch: state.basketMatchPlanEpoch,
    planKey: state.basketMatchPlanKey,
    start,
    end,
    needIds: targetIds,
    needVersions,
    page,
    status: "starting",
    pollVersion: 0,
    timer: null,
  };
  state.basketMatchJobs[tempKey] = record;
  if (renderAfter) render();
  try {
    const request = {
      start,
      end,
      page,
      size: 5,
      force_refresh: forceRefresh,
      ...(needIds ? { need_ids: targetIds } : {}),
    };
    const job = await api("/basket/match-jobs", "POST", request);
    if (!isCurrentBasketMatchRecord(record)) {
      delete state.basketMatchJobs[tempKey];
      return;
    }
    delete state.basketMatchJobs[tempKey];
    record.jobId = job.job_id;
    record.key = job.job_id;
    record.status = job.status;
    state.basketMatchJobs[record.key] = record;
    applyBasketMatchSnapshot(job, record);
    if (["queued", "running"].includes(job.status)) scheduleBasketMatchPoll(record);
    else delete state.basketMatchJobs[record.key];
  } catch (error) {
    if (!isCurrentBasketMatchRecord(record)) {
      delete state.basketMatchJobs[tempKey];
      return;
    }
    delete state.basketMatchJobs[tempKey];
    targetIds.forEach((id) => {
      if (needVersions[id] !== state.basketMatchNeedVersions[id]) return;
      delete state.basketMatchBusy[id];
      state.basketMatchErrors[id] = error.message;
    });
    if (!needIds) state.basketMatchGlobalError = error.message;
  } finally {
    if (isCurrentBasketMatchRecord(record)) {
      if (!needIds) state.basketMatchGlobalRefreshing = false;
      if (renderAfter || !record.jobId) render();
    }
  }
}
function navigate(view) {
  if (view === "basket") {
    state.shoppingPanel = "oda";
    view = "shopping";
  } else if (view === "shopping") state.shoppingPanel = "single";
  state.view = view;
  location.hash = view;
  refreshView();
}
function render() {
  const main = $("#main");
  const focused = document.activeElement;
  const focusKey = focused?.dataset?.focusKey;
  let selection = null;
  if (focusKey) {
    try {
      if (typeof focused.selectionStart === "number")
        selection = [focused.selectionStart, focused.selectionEnd];
    } catch {}
  }
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
        "Du er frakoblet. Koble til igjen for å laste inn eller lagre endringer for husholdningen.",
        "callout offline",
      ),
    );
  (
    ({
      week: renderWeek,
      recipes: renderRecipes,
      shopping: renderShopping,
      settings: renderSettings,
      catalogue: renderCatalogue,
    })[state.view] || renderWeek
  )(main);
  if (focusKey) {
    const replacement = [...main.querySelectorAll("[data-focus-key]")].find(
      (node) => node.dataset.focusKey === focusKey,
    );
    replacement?.focus({ preventScroll: true });
    if (replacement && selection) {
      try {
        replacement.setSelectionRange(...selection);
      } catch {}
    }
  }
}
function renderWeek(main) {
  state.week = startOfWeek(state.week || state.selectedDate || new Date());
  add(
    main,
    title(
      "Planlegg gode måltider",
      "Litt rom i dagen.",
      "Oversikt over ukens måltider for husholdningen.",
      [
        button("Foreslå måltider", suggestWeek),
        button("Planlegg et måltid", () => mealForm(iso(state.selectedDate)), "primary"),
      ],
    ),
    weekControls(),
  );
  const grid = el("div", null, "week-grid");
  for (let i = 0; i < 7; i++) {
    const date = day(i);
    const dateIso = iso(date);
    const dayMeals = agendaMeals(state.meals, dateIso);
    const isToday = dateIso === iso(new Date());
    const isSelected = dateIso === iso(state.selectedDate);
    const card = el(
      "section",
      null,
      "day" + (isToday ? " today" : "") + (isSelected ? " selected-day" : ""),
    );
    card.dataset.date = dateIso;
    add(
      card,
      el("h3", dateLabel(date, { weekday: "long" })),
      el(
        "p",
        dateLabel(date, { month: "short", day: "numeric" }) +
          (isToday ? " · I dag" : ""),
        "date",
      ),
    );
    if (dayMeals.length) {
      const list = el("div", null, "agenda-list");
      dayMeals.forEach((meal) => add(list, renderAgendaMeal(meal)));
      add(card, list);
    } else {
      add(card, el("p", "Ingen planlagte måltider", "muted empty-day"));
    }
    const quickAdd = button("+ Legg til måltid", () => {
      state.selectedDate = new Date(date);
      mealForm(dateIso);
    }, "quiet small add-meal-button");
    quickAdd.dataset.date = dateIso;
    quickAdd.setAttribute(
      "aria-label",
      "Legg til måltid for " + dateLabel(date, { weekday: "long", day: "numeric", month: "short" }),
    );
    add(card, quickAdd);
    add(grid, card);
  }
  add(main, grid);
}
function renderAgendaMeal(meal) {
  const recipe = state.recipes.find((item) => item.id === meal.recipe_id);
  const card = el("article", null, "agenda-meal");
  const metadata = mealAgendaMetadata(meal);
  add(
    card,
    add(
      el("div", null, "agenda-meal-copy"),
      el("h3", recipe?.title || "Oppskriften er ikke tilgjengelig", "meal-title"),
      metadata ? el("p", metadata, "muted") : null,
    ),
    add(
      el("div", null, "meal-actions"),
      recipe ? button("Vis oppskrift", () => viewRecipe(recipe), "small") : null,
      button("Start matlaging", () => openPlannedMeal(meal), "small primary"),
      button("Rediger", () => mealForm(iso(parseDate(meal.date) || state.selectedDate), meal.recipe_id, meal), "small quiet"),
      button(
        "Fjern",
        async () => {
          try {
            await mutate(
              "/meals/" + encodeURIComponent(meal.id),
              "DELETE",
              undefined,
              "Måltidet er fjernet.",
            );
          } catch (err) {
            notify(err.message, true);
          }
        },
        "small quiet",
      ),
    ),
  );
  return card;
}
async function suggestWeek() {
  const root = openDialog("Ukeplanforslag");
  add(
    root,
    el(
      "p",
      "Se over nye måltidsforslag fra KI og velg hvilke du vil legge til. De valgte måltidene legges til samlet når du bekrefter. Eksisterende måltider beholdes.",
      "muted",
    ),
  );
  add(root, recentFeedbackPanel().node);
  const result = el("div", null, "loading");
  result.setAttribute("aria-live", "polite");
  result.textContent = "Lager forslag …";
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
          "Fikk ingen måltidsforslag. Prøv igjen senere, eller planlegg en middag selv.",
          "muted",
        ),
      );
      return;
    }
    if (Array.isArray(draft.notes) && draft.notes.length) {
      const notes = add(
        el("section", null, "callout"),
        el("strong", "Merknader fra planleggeren"),
      );
      const list = el("ul");
      draft.notes.forEach((note) => add(list, el("li", String(note))));
      add(notes, list);
      add(result, notes);
    }
    const list = el("div", null, "draft-meals");
    const selections = [];
    let proposalId = null;
    let applying = false;
    let applied = false;
    const status = el("p", "", "draft-status");
    status.setAttribute("role", "status");
    const updateApply = () => {
      const count = selections.filter(({ input }) => input.checked).length;
      apply.disabled = applying || applied || !count;
      apply.textContent = applied
        ? "Lagt til i ukeplanen"
        : proposalId
          ? "Prøv å bekrefte de valgte måltidene igjen"
          : `Legg til valgte måltider (${count})`;
      selections.forEach(({ input, available }) => {
        input.disabled = !available || applying || applied || Boolean(proposalId);
      });
    };
    const apply = button("", async () => {
      if (applying || applied || !selections.some(({ input }) => input.checked)) return;
      applying = true;
      updateApply();
      status.textContent = "Legger til valgte måltider samlet …";
      try {
        if (!proposalId) {
          const proposal = await api("/meal-plan-proposals", "POST", {
            meals: selections.filter(({ input }) => input.checked).map(({ meal }) => meal),
          });
          if (!proposal?.id) throw new Error("Fikk ikke en gyldig bekreftelse på forslaget.");
          proposalId = proposal.id;
        }
        await api("/meal-plan-proposals/" + encodeURIComponent(proposalId) + "/confirm", "POST", {
          reviewed: true,
        });
        applied = true;
        status.textContent = "De valgte måltidene er lagt til i ukeplanen.";
      } catch (error) {
        if (error.status === 409) {
          proposalId = null;
          status.textContent = "Forslaget er utdatert eller utløpt. Se over utvalget og bekreft på nytt for å opprette et nytt forslag.";
        } else {
          status.textContent = error.message + (proposalId
            ? " Bekreftelsen kunne ikke avklares. Prøv igjen med det samme utvalget; måltidene legges ikke til to ganger."
            : " Se over utvalget og prøv igjen.");
        }
      } finally {
        applying = false;
        updateApply();
      }
      if (applied) {
        try {
          await refreshView();
        } catch {
          status.textContent = "Måltidene er lagt til, men ukevisningen kunne ikke oppdateres. Last siden på nytt for å se dem.";
        }
      }
    }, "primary");
    draft.meals.forEach((meal) => {
      const recipe = state.recipes.find((item) => item.id === meal.recipe_id);
      const date = new Date(meal.date + "T12:00:00");
      const row = el("article", null, "draft-meal");
      const info = el("div");
      const mealDate = Number.isNaN(date.valueOf())
        ? meal.date
        : dateLabel(date, { weekday: "long", month: "short", day: "numeric" });
      const input = el("input");
      input.type = "checkbox";
      input.checked = Boolean(recipe);
      selections.push({ input, meal, available: Boolean(recipe) });
      input.addEventListener("change", () => {
        status.textContent = "";
        updateApply();
      });
      add(
        info,
        add(
          el("label"),
          input,
          el("span", "Legg til: " + (recipe?.title || "Oppskriften er ikke tilgjengelig"), "meal-title"),
        ),
        el("p", [mealDate, mealAgendaMetadata(meal)].filter(Boolean).join(" · "), "muted"),
      );
      if (!recipe) {
        add(info, el("p", "Oppskriften finnes ikke lenger i samlingen din, og kan derfor ikke legges til.", "muted"));
      }
      add(row, info);
      list.append(row);
    });
    updateApply();
    add(result, list, apply, status);
  } catch (error) {
    result.className = "";
    result.replaceChildren();
    add(result, el("p", error.message, "form-error"));
    if (/setup|configured/i.test(error.message))
      add(
        result,
        el(
          "p",
          "Be husholdningsadministratoren om å sette opp KI-leverandøren, eller legg til en middag selv.",
          "muted",
        ),
      );
    else if (/recipe/i.test(error.message))
      add(
        result,
        el(
          "p",
          "Legg til minst én oppskrift i samlingen, og prøv igjen.",
          "muted",
        ),
      );
    add(
      result,
      button("Lukk", () => $("#modal").close(), "quiet"),
    );
  }
}
function mealForm(date = iso(state.selectedDate), recipeId, existingMeal = null) {
  if (!state.recipes.length) {
    const root = openDialog("Start med en oppskrift");
    add(
      root,
      el("p", "Legg til den første oppskriften, og velg deretter en dag.", "muted"),
      button("Legg til oppskrift", addRecipeForm, "primary"),
    );
    return;
  }
  const root = openDialog(existingMeal ? "Rediger måltid" : "Planlegg et måltid");
  const form = el("form");
  const chosenDate = field("Dato", "date", date);
  chosenDate.input.required = true;
  const label = field(
    "Etikett (valgfritt)",
    "text",
    existingMeal?.label || mealSlotLabel(existingMeal?.slot),
    "For eksempel frokost, matpakke eller restemiddag.",
  );
  const scheduledTime = field(
    "Tidspunkt (valgfritt)",
    "time",
    existingMeal?.scheduled_time || "",
  );
  const recipe = field("Oppskrift", "select");
  state.recipes.forEach((item) => {
    const option = el("option", item.title);
    option.value = item.id;
    add(recipe.input, option);
  });
  recipe.input.value = recipeId || existingMeal?.recipe_id || "";
  const servings = field(
    "Porsjoner",
    "number",
    existingMeal?.servings || state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  add(form, chosenDate.wrap, label.wrap, scheduledTime.wrap, recipe.wrap, servings.wrap);
  formSubmit(form, existingMeal ? "Lagre endringer" : "Legg til i ukeplanen", async () => {
    const mealDate = chosenDate.input.value;
    const request = buildMealWriteRequest(existingMeal, {
      date: mealDate,
      label: label.input.value,
      scheduled_time: scheduledTime.input.value,
      recipe_id: recipe.input.value,
      servings: servings.input.value,
    });
    await api(request.path, request.method, request.payload);
    state.selectedDate = parseDate(mealDate) || localDate(new Date());
    state.week = startOfWeek(state.selectedDate);
    syncPlannerUrl();
    $("#modal").close();
    state.view = "week";
    location.hash = "week";
    await refreshView();
    notify(existingMeal ? "Måltidet er oppdatert." : "Måltidet er lagt til i planen.");
  });
  add(root, form);
}
function renderRecipes(main) {
  add(
    main,
    title(
      "Din egen samling",
      "Oppskrifter å ta vare på.",
      "Hverdagsfavoritter, nye funn og din neste gode middag.",
      [
        button("Ukartlagte råvarer", () => unmappedIngredientsDialog(), "quiet"),
        button("Legg til oppskrift", addRecipeForm, "primary"),
      ],
    ),
  );
  if (!recipeAssistantConfigured()) {
    add(
      main,
      add(
        el("section", null, "callout connection-prompt"),
        el(
          "p",
          "KI-hjelp for oppskrifter og måltidsforslag er ikke satt opp. Du kan fortsatt bla i kilder og redigere oppskrifter. Administrer tilkoblingen for oppskriftsassistenten i innstillingene.",
        ),
        connectionSettingsButton(),
      ),
    );
  }
  const collectionStatus = el("section", null, "callout recipe-collection-progress");
  collectionStatus.hidden = true;
  const collectionSummary = el("p", "", "muted");
  collectionSummary.dataset.collectionSummary = "true";
  collectionSummary.setAttribute("role", "status");
  collectionSummary.setAttribute("aria-live", "polite");
  const collectionActions = el("div", null, "actions");
  const resumeCollection = button("Vis importen", () => recipeCollectionForm(state.recipeCollectionJob?.job_id));
  const cancelCollection = button("Avbryt gjenstående import", async () => {
    const jobId = state.recipeCollectionJob?.job_id;
    if (!jobId) return;
    cancelCollection.disabled = true;
    try {
      const job = window.KitchenRecipeCollections.normalizeJob(
        await api("/recipe-collections/" + encodeURIComponent(jobId) + "/cancel", "POST", {}),
      );
      rememberRecipeCollectionJob(job);
      state.recipes = await api("/recipes?include_stats=true");
      if (state.view === "recipes") render();
    } catch (error) {
      collectionSummary.textContent = error.message;
    } finally {
      cancelCollection.disabled = false;
    }
  }, "quiet");
  cancelCollection.dataset.collectionCancel = "true";
  add(collectionActions, resumeCollection, cancelCollection);
  add(collectionStatus, collectionSummary, collectionActions);
  state.recipeCollectionStatusNode = collectionStatus;
  updateRecipeCollectionStatus();
  add(main, collectionStatus);
  const tools = el("div", null, "toolbar");
  const search = el("input", null, "search");
  search.type = "search";
  search.placeholder = "Søk etter en oppskrift …";
  search.ariaLabel = "Søk etter en oppskrift";
  add(tools, search);
  add(main, tools);

  let selectedTypeFilter = null;
  let selectedPrepFilter = null;
  let selectedSourceFilter = null;
  const filterSection = el("div", null, "recipe-filters");
  const typeChips = el("div", null, "filter-chips type-chips");
  const prepChips = el("div", null, "filter-chips prep-chips");
  const sourceChips = el("div", null, "filter-chips source-chips");

  const typeOptions = [
    ["", "Alle typer"],
    ["middag", "Middag"],
    ["dessert", "Dessert"],
    ["frokost", "Frokost"],
    ["lunsj", "Lunsj"],
    ["tilbehør", "Tilbehør"],
    ["bakst", "Bakst"],
    ["annet", "Annet"],
  ];
  typeOptions.forEach(([val, label]) => {
    const chip = button(label, () => {
      selectedTypeFilter = (selectedTypeFilter === val || !val) ? null : val;
      updateFilterChipActive();
      draw();
    }, "filter-chip" + (!selectedTypeFilter && !val ? " active" : ""));
    chip.dataset.typeFilter = val;
    add(typeChips, chip);
  });

  const prepOptions = [
    ["", "Alle tider"],
    ["rask", "Rask"],
    ["normal", "Normal"],
    ["langtid", "Langtid"],
    ["batch_cooking", "Batch-cooking"],
  ];
  prepOptions.forEach(([val, label]) => {
    const chip = button(label, () => {
      selectedPrepFilter = (selectedPrepFilter === val || !val) ? null : val;
      updateFilterChipActive();
      draw();
    }, "filter-chip" + (!selectedPrepFilter && !val ? " active" : ""));
    chip.dataset.prepFilter = val;
    add(prepChips, chip);
  });

  const sourceOptions = [
    ["", "Alle kilder"],
    ["cookidoo", "Cookidoo"],
    ["oda", "Oda"],
    ["imported", "Lenke"],
    ["ai", "KI-idé"],
    ["manual", "Egen"],
  ];
  sourceOptions.forEach(([val, label]) => {
    const chip = button(label, () => {
      selectedSourceFilter = (selectedSourceFilter === val || !val) ? null : val;
      updateFilterChipActive();
      draw();
    }, "filter-chip" + (!selectedSourceFilter && !val ? " active" : ""));
    chip.dataset.sourceFilter = val;
    add(sourceChips, chip);
  });

  function updateFilterChipActive() {
    typeChips.querySelectorAll(".filter-chip").forEach((btn) => {
      const val = btn.dataset.typeFilter;
      btn.classList.toggle("active", (!selectedTypeFilter && !val) || selectedTypeFilter === val);
    });
    prepChips.querySelectorAll(".filter-chip").forEach((btn) => {
      const val = btn.dataset.prepFilter;
      btn.classList.toggle("active", (!selectedPrepFilter && !val) || selectedPrepFilter === val);
    });
    sourceChips.querySelectorAll(".filter-chip").forEach((btn) => {
      const val = btn.dataset.sourceFilter;
      btn.classList.toggle("active", (!selectedSourceFilter && !val) || selectedSourceFilter === val);
    });
  }

  add(filterSection, typeChips, prepChips, sourceChips);
  add(main, filterSection);

  const results = el("div");
  const draw = () => {
    results.replaceChildren();
    const query = search.value.trim().toLowerCase();
    const matches = state.recipes.filter((recipe) => {
      if (query && !recipe.title.toLowerCase().includes(query)) return false;
      if (selectedTypeFilter && recipe.recipe_type !== selectedTypeFilter) return false;
      if (selectedPrepFilter && recipe.prep_category !== selectedPrepFilter) return false;
      if (selectedSourceFilter && !matchesRecipeSourceFilter(recipe, selectedSourceFilter)) return false;
      return true;
    });
    if (!state.recipes.length)
      add(
        results,
        empty(
          "Oppskriftsboken starter her",
          "Legg til en oppskrift du liker, eller importer en fra en lenke. Samlingen bygger du selv.",
          button("Legg til din første oppskrift", addRecipeForm, "primary"),
        ),
      );
    else if (!matches.length)
      add(results, empty("Fant ingen oppskrifter", "Prøv et annet oppskriftsnavn eller nullstill filtrene."));
    else {
      const grid = el("div", null, "grid");
      matches.forEach((recipe) => {
        const metaBadge = formatRecipeBadge(recipe);
        const sourceBadge = formatRecipeSourceBadge(recipe);
        const badges = (sourceBadge || metaBadge)
          ? add(
              el("div", null, "recipe-badges"),
              sourceBadge ? el("span", sourceBadge, "badge recipe-badge-source") : null,
              metaBadge ? el("span", metaBadge, "badge recipe-badge-meta") : null,
            )
          : null;
        add(
          grid,
          add(
            el("article", null, "card recipe-card"),
            recipeImage(recipe),
            badges,
            el("h3", recipe.title),
            el(
              "p",
                formatServings(recipe.servings) +
                " · " +
                formatUiCount(recipe.ingredients.length) +
                " ingredienser",
              "muted",
            ),
            renderRecipeStatsBadges(recipe),
            add(
              el("div", null, "actions"),
              button("Vis oppskrift", () => viewRecipe(recipe)),
              button("Planlegg", () => mealForm(undefined, recipe.id), "quiet"),
            ),
          ),
        );
      });
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
    isEditing ? "Rediger oppskrift" : draft.title ? "Se over oppskriften" : "Legg til oppskrift",
  );
  if (source !== "manual")
    add(
      root,
      el(
        "p",
        "Kontroller ingrediensene, mengdene og fremgangsmåten før du lagrer.",
        "callout",
      ),
    );
  if (draft.photo_uncertainties?.length)
    add(
      root,
      add(
        el("section", null, "callout"),
        el("strong", "Kontroller dette fra bildet"),
        add(
          el("ul"),
          ...draft.photo_uncertainties.map((note) => el("li", note)),
        ),
      ),
    );
  if (draft.identity_roundtrip_warning)
    add(root, el(
      "p",
      "KI-utkastet har ikke bekreftede produkt- og enhetskoblinger. Hvis du lagrer det, fjernes koblingene fra oppskriften. Den lagrede oppskriften er uendret fram til du lagrer.",
      "callout",
    ));
  if (draft.photo_source)
    {
      const photoEvidence = add(
        el("div", null, "photo-source-evidence"),
        el(
          "p",
          [draft.photo_source.book_title, draft.photo_source.page ? "side " + draft.photo_source.page : ""]
            .filter(Boolean)
            .join(" · ") || "Kilde: privat bilde av oppskriftsside",
          "muted",
        ),
        button("Vis originalbildet", () => viewRecipePhoto(draft.photo_source), "quiet"),
      );
      if (!isEditing) {
        const modal = $("#modal");
        const discardStatus = el("p", "", "muted");
        discardStatus.setAttribute("role", "status");
        const discardPrompt = el(
          "div",
          null,
          "photo-discard-confirmation",
        );
        discardPrompt.setAttribute("role", "group");
        discardPrompt.setAttribute("aria-label", "Bekreft forkasting av bildeutkast");
        discardPrompt.hidden = true;
        add(
          discardPrompt,
          el(
            "p",
            "Dette forkaster originalbildet og den usparte oppskriftskladden. Oppskriften blir ikke lagret.",
            "muted",
          ),
        );
        const discardButton = button("Forkast bilde og utkast", async () => {
          discardButton.disabled = true;
          keepDraftButton.disabled = true;
          discardStatus.textContent = "Forkaster bildeutkastet …";
          try {
            await window.KitchenRecipePhoto.applyDiscardIfActive(
              api("/recipe-photos/" + encodeURIComponent(draft.photo_source.id), "DELETE"),
              () => modal.open && photoEvidence.isConnected,
              {
                deleted: () => {
                  modal.close();
                  notify("Bildeutkastet er forkastet.");
                },
                retained: () => {
                  discardPrompt.hidden = true;
                  discardTrigger.focus();
                  discardStatus.textContent =
                    "Bildet er knyttet til en lagret oppskrift og ble beholdt. Kladden er fortsatt åpen.";
                },
                unconfirmed: () => {
                  discardPrompt.hidden = true;
                  discardTrigger.focus();
                  discardStatus.textContent =
                    "Vi kunne ikke bekrefte at bildet ble forkastet. Kladden er fortsatt åpen.";
                },
              },
            );
          } catch {
            if (modal.open && photoEvidence.isConnected)
              discardStatus.textContent =
                "Vi kunne ikke bekrefte at bildet ble forkastet. Kladden er fortsatt åpen.";
          } finally {
            if (discardButton.isConnected) {
              discardButton.disabled = false;
              keepDraftButton.disabled = false;
            }
          }
        }, "quiet");
        const keepDraftButton = button("Behold oppskriftsutkastet", () => {
          discardPrompt.hidden = true;
          discardTrigger.focus();
        }, "quiet");
        const discardTrigger = button("Forkast bildeutkast", () => {
          discardPrompt.hidden = false;
          keepDraftButton.focus();
        }, "quiet");
        add(discardPrompt, add(el("div", null, "actions"), keepDraftButton, discardButton));
        add(
          photoEvidence,
          discardTrigger,
          discardPrompt,
          discardStatus,
        );
      }
      add(root, photoEvidence);
    }
  const form = el("form");
  const name = field("Oppskriftsnavn", "text", draft.title);
  name.input.required = true;
  const servings = field(
    "Antall porsjoner",
    "number",
    draft.servings || state.household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  const recipeTypeLabels = window.KitchenRecipeEditor?.RECIPE_TYPE_LABELS || {
    middag: "Middag",
    dessert: "Dessert",
    frokost: "Frokost",
    lunsj: "Lunsj",
    tilbehør: "Tilbehør",
    bakst: "Bakst",
    annet: "Annet",
  };
  const recipeTypeValues = window.KitchenRecipeEditor?.RECIPE_TYPES || Object.keys(recipeTypeLabels);
  const prepCategoryLabels = window.KitchenRecipeEditor?.PREP_CATEGORY_LABELS || {
    rask: "Rask",
    normal: "Normal",
    langtid: "Langtid",
    batch_cooking: "Batch-cooking",
  };
  const prepCategoryValues = window.KitchenRecipeEditor?.PREP_CATEGORIES || Object.keys(prepCategoryLabels);
  const recipeType = field("Type oppskrift (valgfritt)", "select", draft.recipe_type || "");
  recipeType.input.name = "recipe_type";
  const emptyTypeOpt = el("option", "Ikke valgt");
  emptyTypeOpt.value = "";
  add(recipeType.input, emptyTypeOpt);
  recipeTypeValues.forEach((value) => {
    const opt = el("option", recipeTypeLabels[value] || value);
    opt.value = value;
    add(recipeType.input, opt);
  });
  recipeType.input.value = draft.recipe_type || "";
  const prepTimeMinutes = field(
    "Tilberedningstid (min, valgfritt)",
    "number",
    draft.prep_time_minutes ?? "",
  );
  prepTimeMinutes.input.name = "prep_time_minutes";
  prepTimeMinutes.input.min = 1;
  prepTimeMinutes.input.max = 2880;
  prepTimeMinutes.input.step = 1;
  prepTimeMinutes.input.placeholder = "f.eks. 30";
  const prepCategory = field("Tidskategori (valgfritt)", "select", draft.prep_category || "");
  prepCategory.input.name = "prep_category";
  const emptyPrepOpt = el("option", "Ikke valgt");
  emptyPrepOpt.value = "";
  add(prepCategory.input, emptyPrepOpt);
  prepCategoryValues.forEach((value) => {
    const opt = el("option", prepCategoryLabels[value] || value);
    opt.value = value;
    add(prepCategory.input, opt);
  });
  prepCategory.input.value = draft.prep_category || "";
  const metadataRow = add(
    el("div", null, "recipe-metadata-row"),
    recipeType.wrap,
    prepTimeMinutes.wrap,
    prepCategory.wrap,
  );
  if (
    !isEditing &&
    (source === "imported" || draft.provenance?.original_source === "imported") &&
    draft.servings != null
  )
    add(
      form,
      el(
        "p",
        "Porsjoner i kildeoppskriften: " + formatUiCount(draft.servings) + ". Verdien nedenfor er det som lagres.",
        "muted",
      ),
    );
  const imageUrl = field(
    "Lenke til oppskriftsbilde (valgfritt)",
    "url",
    draft.image_url || "",
    "Bruk en HTTPS-lenke til et bilde. Bildet legges bare til hvis du oppgir en lenke.",
  );
  imageUrl.input.placeholder = "https://…";
  const imageCredit = recipeImageCredit(draft.image_attribution);
  const ingredientEditor = el("fieldset", null, "ingredient-editor");
  add(ingredientEditor, el("legend", "Ingredienser"));
  add(
    ingredientEditor,
    el(
      "p",
      "Se over kildeteksten. Mengder kan stå tomme hvis teksten er uklar. Oppgi bare mengder og enheter du kan bekrefte.",
      "muted",
    ),
  );
  if (draft.source_url) {
    const original = el("a", "Åpne kildeoppskriften", "source-link");
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
  let ingredientRowSequence = 0;
  let ingredientValidationFailed = false;
  let recipeFormError = null;

  function updateIngredientRowLabels() {
    rowControllers.forEach((row, index) => {
      row.legend.textContent = "Ingrediens " + formatUiCount(index + 1);
      row.remove.setAttribute("aria-label", "Fjern ingrediens " + formatUiCount(index + 1));
    });
  }

  function updateIngredientRowErrors(row, show = false) {
    if (show) row.validationShown = true;
    if (!row.validationShown) return null;
    const { item, errors: fieldErrors } = window.KitchenRecipeEditor.recipeIngredientForSave({
      name: row.name.value,
      quantity: row.quantity.value,
      unit: row.unit.value,
      category: row.category.value,
      source_indices: row.source_indices,
      purchase_estimate: row.purchase_estimate,
      original_name: row.original_name,
      original_unit: row.original_unit,
      canonical_product_id: row.canonical_product_id,
      canonical_unit_id: row.canonical_unit_id,
      source_unit_expression: row.source_unit_expression,
    });
    const messages = {};
    if (fieldErrors.name) messages.name = "Oppgi navnet på ingrediensen.";
    if (fieldErrors.quantity)
      messages.quantity = "Oppgi en gyldig positiv mengde med komma eller punktum, eller la feltet stå tomt.";
    if (fieldErrors.unit) messages.unit = "Oppgi enheten når du har oppgitt en mengde.";
    row.error.replaceChildren();
    Object.entries(messages).forEach(([key, message]) => {
      const messageNode = el("span", message);
      messageNode.id = row.error.id + "-" + key;
      row.error.append(messageNode, el("span", " "));
      row[key].setAttribute("aria-describedby", messageNode.id);
    });
    ["name", "quantity", "unit"].forEach((key) => {
      if (!messages[key]) row[key].removeAttribute("aria-describedby");
    });
    row.name.setAttribute("aria-invalid", String(fieldErrors.name));
    row.quantity.setAttribute("aria-invalid", String(fieldErrors.quantity));
    row.unit.setAttribute("aria-invalid", String(fieldErrors.unit));
    row.invalidFields = Object.keys(messages);
    return { item, invalidFields: row.invalidFields };
  }

  function clearIngredientValidationSummaryIfCorrected() {
    if (!ingredientValidationFailed || !rowControllers.length) return;
    if (!rowControllers.every((row) => row.validationShown && row.invalidFields.length === 0)) return;
    ingredientValidationFailed = false;
    if (recipeFormError) recipeFormError.textContent = "";
  }

  function createIngredientRow(values) {
    const row = el("fieldset", null, "ingredient-row");
    const legend = el("legend");
    const sourceText = values.sourceText
      ? el("p", "Tekst fra kilden: " + values.sourceText, "ingredient-source")
      : null;
    const fields = el("div", null, "ingredient-fields");
    const ingredientName = field("Navn", "text", values.name);
    const quantity = field("Mengde", "text", values.quantity);
    quantity.input.inputMode = "decimal";
    const unit = field("Enhet", "text", values.unit);
    const category = field("Kategori", "text", values.category || "Other");
    const estimateText = purchaseEstimateText(values.purchase_estimate);
    const estimateNode = estimateText ? el("p", estimateText, "muted purchase-estimate") : null;
    const rowError = el("p", "", "ingredient-error");
    rowError.setAttribute("role", "alert");
    rowError.id = "recipe-ingredient-errors-" + (++ingredientRowSequence);
    const remove = button("Fjern", () => {
      const index = rowControllers.indexOf(controller);
      if (index < 0) return;
      rowControllers.splice(index, 1);
      row.remove();
      updateIngredientRowLabels();
      clearIngredientValidationSummaryIfCorrected();
    }, "quiet small");
    const controller = {
      row,
      legend,
      remove,
      name: ingredientName.input,
      quantity: quantity.input,
      unit: unit.input,
      source_indices: [...(values.source_indices || [])],
      original_name: values.name == null ? "" : String(values.name),
      original_unit: values.unit == null ? "" : String(values.unit),
      ...(Object.prototype.hasOwnProperty.call(values, "canonical_product_id")
        ? { canonical_product_id: values.canonical_product_id }
        : {}),
      ...(Object.prototype.hasOwnProperty.call(values, "canonical_unit_id")
        ? { canonical_unit_id: values.canonical_unit_id }
        : {}),
      ...(Object.prototype.hasOwnProperty.call(values, "source_unit_expression")
        ? { source_unit_expression: values.source_unit_expression }
        : {}),
      purchase_estimate: values.purchase_estimate ? { ...values.purchase_estimate } : null,
      category: category.input,
      error: rowError,
      validationShown: false,
    };
    [controller.name, controller.quantity, controller.unit].forEach((input) => {
      input.addEventListener("input", () => {
        updateIngredientRowErrors(controller);
        clearIngredientValidationSummaryIfCorrected();
      });
    });
    add(fields, ingredientName.wrap, quantity.wrap, unit.wrap, category.wrap);
    add(row, legend, sourceText, fields, estimateNode, rowError, remove);
    return controller;
  }

  rowControllers = rowControllers.map(createIngredientRow);
  rowControllers.forEach((row) => ingredientRows.append(row.row));
  updateIngredientRowLabels();
  const addIngredient = button("+ Legg til ingrediens", () => {
    const row = createIngredientRow(window.KitchenRecipeEditor.rowFromDraft({}));
    rowControllers.push(row);
    ingredientRows.append(row.row);
    updateIngredientRowLabels();
    row.name.focus();
  }, "quiet");
  add(ingredientEditor, ingredientRows, addIngredient);
  const instructions = field(
    "Fremgangsmåte",
    "textarea",
    (draft.instructions || []).join("\n"),
    "Skriv ett trinn per linje.",
  );
  instructions.input.required = true;
  add(
    form,
    name.wrap,
    servings.wrap,
    metadataRow,
    imageUrl.wrap,
    imageCredit,
    ingredientEditor,
    instructions.wrap,
  );
  recipeFormError = formSubmit(form, "Lagre oppskrift", async () => {
    const photo = imageUrl.input.value.trim();
    if (photo && !httpsUrl(photo))
      throw new Error("Lenken til oppskriftsbildet må bruke HTTPS.");
    const rawPrepMinutes = String(prepTimeMinutes.input.value ?? "").trim();
    const prepMinutes = rawPrepMinutes ? Number(rawPrepMinutes) : null;
    if (rawPrepMinutes && (!Number.isInteger(prepMinutes) || prepMinutes < 1))
      throw new Error("Tilberedningstid må være et positivt heltall i minutter.");
    if (!rowControllers.length) throw new Error("Legg til minst én ingrediens før du lagrer.");
    let hasIngredientErrors = false;
    let firstInvalidField = null;
    const rows = rowControllers.map((row) => {
      const validation = updateIngredientRowErrors(row, true);
      if (validation.invalidFields.length) {
        hasIngredientErrors = true;
        if (!firstInvalidField) firstInvalidField = row[validation.invalidFields[0]];
      }
      return validation.item;
    });
    ingredientValidationFailed = hasIngredientErrors;
    if (hasIngredientErrors) {
      firstInvalidField?.focus();
      throw new Error("Rett ingrediensfeltene som er markert med feil før du lagrer.");
    }
    const payload = {
      title: name.input.value.trim(),
      servings: Number(servings.input.value),
      recipe_type: recipeType.input.value || null,
      prep_time_minutes: prepMinutes,
      prep_category: prepCategory.input.value || null,
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
      photo_source: draft.photo_source || null,
      image_attribution: draft.image_attribution || null,
    };
    await api(
      isEditing ? "/recipes/" + encodeURIComponent(draft.id) : "/recipes",
      isEditing ? "PUT" : "POST",
      payload,
    );
    state.recipes = await api("/recipes?include_stats=true");
    $("#modal").close();
    navigate("recipes");
    notify(isEditing ? "Oppskriften er lagret." : "Oppskriften er lagt til i samlingen.");
  });
  add(root, form);
  if (recipeAssistantConfigured() && (isEditing || source === "ai")) {
    const revision = el("details", null, "recipe-ai-revision");
    add(
      revision,
      el("summary", "Få forslag fra KI"),
      el(
        "p",
        isEditing
          ? "KI foreslår et utkast. Den lagrede oppskriften endres først når du velger Lagre."
          : "KI foreslår et utkast. Se over det og velg Lagre oppskrift når du er klar.",
        "muted",
      ),
    );
    const prompt = field(
      "Hva vil du endre?",
      "textarea",
      "",
      "For eksempel: gjør den vegetarisk, raskere eller tilpasset flere porsjoner.",
    );
    prompt.input.required = true;
    prompt.input.maxLength = 1000;
    prompt.input.rows = 3;
    const revisionError = el("p", "", "form-error");
    revisionError.setAttribute("role", "alert");
    const revisionSettings = connectionSettingsButton("Se over innstillingene for oppskriftsassistenten");
    revisionSettings.hidden = true;
    const revisionEditorValues = () => ({
      title: name.input.value,
      servings: servings.input.value,
      recipe_type: recipeType.input.value,
      prep_time_minutes: prepTimeMinutes.input.value,
      prep_category: prepCategory.input.value,
      prompt: prompt.input.value,
      ingredients: rowControllers.map((row) => ({
        name: row.name.value,
        quantity: row.quantity.value,
        unit: row.unit.value,
        category: row.category.value,
        source_indices: row.source_indices,
        original_name: row.original_name,
        original_unit: row.original_unit,
        canonical_product_id: row.canonical_product_id,
        canonical_unit_id: row.canonical_unit_id,
        source_unit_expression: row.source_unit_expression,
      })),
      instructions: instructions.input.value,
      source_url: draft.source_url || null,
      image_url: imageUrl.input.value,
      source_ingredients: sourceIngredients,
      provenance: draft.provenance || null,
      photo_source: draft.photo_source || null,
      image_attribution: draft.image_attribution || null,
    });
    const revise = button("Se over KI-forslaget", async () => {
      revise.disabled = true;
      revisionError.textContent = "";
      try {
        const revisionValues = revisionEditorValues();
        const submittedSnapshot = window.KitchenRecipeEditor.recipeRevisionSnapshot(revisionValues);
        const payload = window.KitchenRecipeEditor.buildRevisionRequest(
          revisionValues,
          { isEditing, source },
          prompt.input.value,
        );
        const applied = await window.KitchenRecipeEditor.applyRevisionIfActive(
          api("/recipe-drafts/ai", "POST", payload),
          () => revise.isConnected && $("#modal").open &&
            submittedSnapshot === window.KitchenRecipeEditor.recipeRevisionSnapshot(revisionEditorValues()),
          (response) => {
            if (!response?.adapted)
              throw new Error("KI-en ga ikke et redigerbart utkast. Oppskriften din er uendret.");
            const revisedDraft = isEditing
              ? {
                  recipe_type: draft.recipe_type ?? null,
                  prep_time_minutes: draft.prep_time_minutes ?? null,
                  prep_category: draft.prep_category ?? null,
                  ...response.adapted,
                  id: draft.id,
                }
              : response.adapted;
            if (isEditing) {
              const sourceIngredients = revisionValues.ingredients.map((item) =>
                window.KitchenRecipeEditor.recipeIngredientForSave(item).item,
              );
              const identityReview = window.KitchenRecipeEditor.prepareAIRevisionForEditor(
                sourceIngredients,
                response.adapted.ingredients,
              );
              revisedDraft.ingredients = identityReview.ingredients;
              revisedDraft.identity_roundtrip_warning = identityReview.identityReviewRequired;
            }
            recipeForm(revisedDraft, isEditing ? source : "ai");
          },
        );
        if (!applied && revise.isConnected && $("#modal").open)
          revisionError.textContent = "Oppskriften ble endret mens KI-forslaget ble laget. Forslaget ble ikke brukt. Be om et nytt forslag for å ta med de siste endringene.";
      } catch (error) {
        if (revise.isConnected && $("#modal").open) {
          revisionError.textContent = error.message;
          revisionSettings.hidden = false;
        }
      } finally {
        if (revise.isConnected) revise.disabled = false;
      }
    }, "primary");
    add(revision, prompt.wrap, revisionError, revisionSettings, revise);
    add(root, revision);
  } else if (isEditing || source === "ai") {
    add(
      root,
      add(
        el("section", null, "callout connection-prompt"),
        el("p", "Oppskriftsassistenten er ikke satt opp, så KI-redigering er ikke tilgjengelig."),
        connectionSettingsButton(),
      ),
    );
  }
}
function recipeImageCredit(attribution) {
  if (!attribution) return null;
  const section = el("section", null, "image-credit");
  add(section, el("h3", "Kreditering av bilde"));
  if (attribution.title) add(section, el("p", attribution.title));
  const creator = attribution.artist || attribution.credit || attribution.attribution;
  if (creator) add(section, el("p", "Fotograf/kreditering: " + creator));
  const license = window.KitchenRecipeImages.safeUrl(
    attribution.license_url,
    new Set(["creativecommons.org", "www.creativecommons.org", "commons.wikimedia.org"]),
  );
  const source = window.KitchenRecipeImages.safeUrl(
    attribution.source_page_url,
    new Set(["commons.wikimedia.org"]),
  );
  const links = el("div", null, "actions");
  if (license) {
    const link = el("a", attribution.license_name || "Bildekreditering og lisens");
    link.href = license;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(links, link);
  }
  if (source) {
    const link = el("a", "Kilde hos Wikimedia Commons");
    link.href = source;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(links, link);
  }
  if (attribution.obligations?.length)
    add(section, add(el("ul"), ...attribution.obligations.map((item) => el("li", item))));
  if (links.childElementCount) add(section, links);
  return section;
}
function viewRecipe(recipe) {
  const root = openDialog(recipe.title);
  const body = el("div", null, "recipe-body");
  const metaBadge = formatRecipeBadge(recipe);
  const sourceBadge = formatRecipeSourceBadge(recipe);
  const badges = (sourceBadge || metaBadge)
    ? add(
        el("div", null, "recipe-badges"),
        sourceBadge ? el("span", sourceBadge, "badge recipe-badge-source") : null,
        metaBadge ? el("span", metaBadge, "badge recipe-badge-meta") : null,
      )
    : null;
  const statsContainer = el("div", null, "recipe-stats-host");
  function updateStatsDisplay(currentRecipe) {
    statsContainer.replaceChildren();
    const badges = renderRecipeStatsBadges(currentRecipe);
    if (badges) add(statsContainer, badges);
  }
  updateStatsDisplay(recipe);
  add(
    body,
    recipeImage(recipe),
    badges,
    el("p", formatUiCount(recipe.servings) + " porsjoner", "muted"),
    statsContainer,
  );
  if (recipe?.id && window.KitchenRecipeQuickFeedback?.renderQuickFeedbackBar) {
    let statsUpdateSeq = 0;
    const quickFeedbackWrap = el("section", null, "quick-feedback-widget");
    const quickFeedbackLabel = el("p", "Hurtigvurdering", "quick-feedback-label");
    const quickFeedbackBar = window.KitchenRecipeQuickFeedback.renderQuickFeedbackBar(
      recipe,
      async (result, payload) => {
        const seq = ++statsUpdateSeq;
        let nextStats = result?.activity_stats || result?.stats || null;
        if (!nextStats && typeof api === "function") {
          try {
            const fetched = await api("/recipes/" + encodeURIComponent(recipe.id) + "/stats");
            if (seq !== statsUpdateSeq) return;
            if (
              fetched &&
              typeof fetched === "object" &&
              (
                "times_planned" in fetched ||
                "times_cooked" in fetched ||
                "times_ordered" in fetched ||
                "latest_rating" in fetched ||
                "average_rating" in fetched ||
                "would_make_again" in fetched ||
                "feedback_count" in fetched
              )
            ) {
              nextStats = fetched;
            }
          } catch {}
        }
        if (seq !== statsUpdateSeq) return;
        if (!nextStats) {
          const prev = recipe.activity_stats || recipe.stats || {};
          nextStats = {
            ...prev,
            ...(payload?.rating != null ? { latest_rating: payload.rating } : {}),
            ...(payload?.would_make_again != null
              ? { would_make_again: payload.would_make_again }
              : {}),
            feedback_count: (Number(prev.feedback_count) || 0) + 1,
          };
          if (nextStats.average_rating == null && payload?.rating != null) {
            nextStats.average_rating = payload.rating;
          }
        }
        recipe.activity_stats = nextStats;
        recipe.stats = nextStats;
        if (Array.isArray(state?.recipes)) {
          state.recipes = state.recipes.map((item) =>
            item.id === recipe.id
              ? { ...item, activity_stats: nextStats, stats: nextStats }
              : item,
          );
        }
        updateStatsDisplay(recipe);
        if (typeof render === "function" && state?.view === "recipes") render();
      },
      {
        api,
        ...(typeof document !== "undefined" ? { document } : {}),
      },
    );
    add(quickFeedbackWrap, quickFeedbackLabel, quickFeedbackBar);
    add(body, quickFeedbackWrap);
  }
  if (recipe.photo_source) {
    const citation = [
      recipe.photo_source.book_title,
      recipe.photo_source.page ? "side " + recipe.photo_source.page : "",
    ]
      .filter(Boolean)
      .join(" · ");
    add(
      body,
      el("p", citation || "Oppskriftsfoto lagret privat", "muted"),
      button("Vis originalbildet", () => viewRecipePhoto(recipe.photo_source), "quiet"),
    );
  }
  if (recipe.image_attribution) add(body, recipeImageCredit(recipe.image_attribution));
  const translationContainer = el("div", null, "recipe-translation-section-wrapper");
  add(body, translationContainer);
  add(body, el("h3", "Ingredienser"));
  const ingredients = el("ul");
  function renderIngredientsList(items) {
    ingredients.replaceChildren();
    (items || []).forEach((item) => {
      const row = el("li");
      add(row, el("span", ingredientQuantityText(item)));
      const estimate = purchaseEstimateText(item.purchase_estimate);
      if (estimate) add(row, el("small", estimate, "muted purchase-estimate"));
      add(ingredients, row);
    });
  }
  renderIngredientsList(recipe.ingredients);

  const steps = el("ol");
  function renderInstructionsList(items) {
    steps.replaceChildren();
    (items || []).forEach((step) => add(steps, el("li", step)));
  }
  renderInstructionsList(recipe.instructions);
  add(body, ingredients, el("h3", "Fremgangsmåte"), steps);

  if (window.KitchenRecipeTranslationsView) {
    window.KitchenRecipeTranslationsView.attachRecipeTranslation(translationContainer, recipe, {
      api,
      notify,
      locale: state.household?.language || "nb",
      onContentChange: (content) => {
        const titleNode = $("#dialog-title");
        if (titleNode && content.title) titleNode.textContent = content.title;
        renderIngredientsList(content.ingredients);
        renderInstructionsList(content.instructions);
      },
    });
  }
  if (recipe.source_url) {
    try {
      const url = new URL(recipe.source_url);
      if (["https:", "http:"].includes(url.protocol)) {
        const link = el("a", "Kildeoppskrift", "source-link");
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
      button("Planlegg et måltid", () => mealForm(undefined, recipe.id), "primary"),
      button("Rediger oppskrift", () => recipeForm(recipe, recipe.source || "manual"), "quiet"),
      button("Kartlegg råvarer", () => unmappedIngredientsDialog({ recipeId: recipe.id }), "quiet"),
      recipe.source === "manual"
        ? button("Finn et bilde", () => recipeImageSearchForm(recipe), "quiet")
        : null,
      button("Vurder oppskriften", () => feedbackForm(recipe), "quiet"),
      recipe.image_url
        ? button("Fjern bilde", () => removeRecipeImage(recipe), "quiet")
        : null,
      button(
        "Slett oppskrift",
        () => {
          const confirmRoot = openDialog("Slette oppskriften?");
          add(
            confirmRoot,
            el(
              "p",
              "Dette fjerner «" +
                recipe.title +
                "» fra samlingen. Måltider som bruker den, kan også måtte oppdateres.",
              "muted",
            ),
            button("Behold oppskriften", () => viewRecipe(recipe)),
            button(
              "Slett oppskrift",
              async () => {
                try {
                  await api(
                    "/recipes/" + encodeURIComponent(recipe.id),
                    "DELETE",
                  );
                  state.recipes = await api("/recipes?include_stats=true");
                  $("#modal").close();
                  await refreshView();
                  notify("Oppskriften er slettet.");
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
function removeRecipeImage(recipe) {
  const root = openDialog("Fjerne oppskriftsbildet?");
  add(
    root,
    el("p", "Dette fjerner bildet og bildekrediteringen. Oppskriftens kilde, ingredienser og fremgangsmåte beholdes.", "muted"),
    button("Behold bildet", () => viewRecipe(recipe)),
    button("Fjern bilde", async () => {
      try {
        const updated = await api("/recipes/" + encodeURIComponent(recipe.id), "PUT", {
          title: recipe.title,
          servings: recipe.servings,
          ingredients: recipe.ingredients,
          instructions: recipe.instructions,
          source_url: recipe.source_url || null,
          image_url: null,
          source: recipe.source,
          source_ingredients: recipe.source_ingredients || [],
          provenance: recipe.provenance || null,
          photo_source: recipe.photo_source || null,
          image_attribution: null,
          recipe_type: recipe.recipe_type || null,
          prep_time_minutes: recipe.prep_time_minutes ?? null,
          prep_category: recipe.prep_category || null,
        });
        state.recipes = state.recipes.map((item) => item.id === recipe.id ? updated : item);
        viewRecipe(updated);
        notify("Oppskriftsbildet og krediteringen er fjernet.");
      } catch (error) {
        notify(error.message, true);
        viewRecipe(recipe);
      }
    }, "danger"),
  );
}
function recipeImageSearchForm(recipe) {
  const root = openDialog("Finn et bilde til oppskriften");
  add(
    root,
    el("p", "Søk etter bilder på Wikimedia Commons. Velg selv etter at du har sett bildet, fotografen, lisensen og krediteringskravene.", "muted"),
    el("p", "Dette endrer bare oppskriftsbildet og bildekrediteringen. Oppskriftens kilde og innhold forblir uendret.", "callout"),
  );
  const form = el("form");
  const query = field("Søk etter bilde", "search", recipe.title);
  query.input.maxLength = 200;
  query.input.required = true;
  const status = el("p", "Søket har ikke startet.", "muted");
  status.setAttribute("role", "status");
  const errors = el("p", "", "form-error");
  errors.setAttribute("role", "alert");
  const results = el("div", null, "recipe-image-results");
  const searchButton = button("Søk etter bilder", () => runSearch(false), "primary");
  const moreButton = button("Vis flere bilder", () => runSearch(true), "quiet");
  moreButton.hidden = true;
  let currentQuery = "";
  let continuation = null;
  let candidates = [];
  let requestSequence = 0;
  let busy = false;

  function drawCandidates() {
    results.replaceChildren();
    if (!candidates.length) return;
    add(results, el("h3", `Treff for «${currentQuery}»`));
    const grid = el("div", null, "grid recipe-image-results-grid");
    candidates.forEach(({ candidate, searchId }) => {
      const card = el("article", null, "card recipe-image-candidate");
      const image = el("img", null, "recipe-image-candidate-preview");
      image.src = candidate.thumbnail_url;
      image.alt = candidate.title;
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => image.replaceWith(el("p", "Forhåndsvisningen er ikke tilgjengelig.", "muted")));
      add(card, image, el("h3", candidate.title));
      if (candidate.artist || candidate.credit || candidate.attribution)
        add(card, el("p", candidate.artist || candidate.credit || candidate.attribution, "muted"));
      if (candidate.credit && candidate.artist) add(card, el("p", candidate.credit, "muted"));
      const license = el("a", candidate.license_name);
      license.href = candidate.license_url;
      license.target = "_blank";
      license.rel = "noopener noreferrer";
      const source = el("a", "Åpne kilde og bildeinformasjon");
      source.href = candidate.source_page_url;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      add(card, el("p", "Lisens: "), license, source);
      if (candidate.obligations?.length)
        add(card, add(el("ul", null, "image-license-obligations"), ...candidate.obligations.map((item) => el("li", item))));
      if (candidate.attribution_required && candidate.attribution)
        add(card, el("p", "Kreditering: " + candidate.attribution, "image-credit-required"));
      const review = el("label", null, "image-license-review");
      const reviewed = el("input");
      reviewed.type = "checkbox";
      add(review, reviewed, el("span", "Jeg har gjennomgått lisensen og krediteringen."));
      const candidateError = el("p", "", "form-error");
      candidateError.setAttribute("role", "alert");
      const choose = button("Bruk dette bildet", async () => {
        choose.disabled = true;
        candidateError.textContent = "";
        status.textContent = "Lagrer valgt bilde og kreditering …";
        try {
          if (!reviewed.checked) throw new Error("Bekreft at du har sett gjennom lisens og kreditering.");
          const updated = await api("/recipes/" + encodeURIComponent(recipe.id) + "/image", "POST", {
            search_id: searchId,
            provider_id: candidate.provider_id,
            reviewed: true,
          });
          state.recipes = state.recipes.map((item) => item.id === recipe.id ? updated : item);
          viewRecipe(updated);
          notify("Oppskriftsbildet og krediteringen er lagret.");
        } catch (error) {
          candidateError.textContent = error.message;
          status.textContent = "Oppskriften er uendret. Du kan gjennomgå bildet på nytt eller søke videre.";
          choose.disabled = !reviewed.checked;
        }
      }, "primary");
      choose.disabled = true;
      reviewed.addEventListener("change", () => { choose.disabled = !reviewed.checked || busy; });
      add(card, review, candidateError, choose);
      grid.append(card);
    });
    add(results, grid);
  }

  async function runSearch(loadMore) {
    if (busy) return;
    const term = query.input.value.trim();
    if (!term) {
      errors.textContent = "Skriv inn hva du vil finne bilde av.";
      return;
    }
    if (loadMore && (!continuation || term !== currentQuery)) return;
    const sequence = ++requestSequence;
    const priorQuery = currentQuery;
    busy = true;
    searchButton.disabled = true;
    moreButton.disabled = true;
    results.querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
    if (!loadMore) moreButton.hidden = true;
    errors.textContent = "";
    status.textContent = loadMore ? "Henter flere bilder …" : "Søker etter bilder …";
    try {
      const response = await api("/recipe-images/search", "POST", {
        query: term,
        limit: 10,
        ...(loadMore ? { continuation } : {}),
      });
      if (sequence !== requestSequence) return;
      const session = window.KitchenRecipeImages.normalizeSession(response);
      if (session.status !== "ok") {
        if (session.status === "no_usable_results" && !loadMore) {
          candidates = [];
          currentQuery = term;
          continuation = null;
          drawCandidates();
        }
        if (session.status === "invalid_continuation") continuation = null;
        status.textContent = window.KitchenRecipeImages.statusLabel(session.status) +
          (candidates.length && term !== currentQuery ? ` Viser fortsatt treff for «${currentQuery}».` : "");
        moreButton.hidden = !continuation;
        return;
      }
      if (!loadMore) {
        candidates = [];
        currentQuery = term;
      }
      const known = new Set(candidates.map(({ candidate }) => candidate.provider_id));
      session.results.forEach((candidate) => {
        if (!known.has(candidate.provider_id)) {
          candidates.push({ candidate, searchId: session.search_id });
          known.add(candidate.provider_id);
        }
      });
      continuation = session.continuation;
      status.textContent = session.results.length
        ? window.KitchenRecipeImages.statusLabel("ok")
        : "Fant ikke flere brukbare bilder.";
      drawCandidates();
      moreButton.hidden = !continuation;
      if (term !== currentQuery) moreButton.hidden = true;
    } catch (error) {
      if (sequence !== requestSequence) return;
      status.textContent = candidates.length
        ? `Søket kunne ikke fullføres. Viser fortsatt treff for «${priorQuery || currentQuery}».`
        : "Søket kunne ikke fullføres. Prøv igjen.";
      errors.textContent = error.message;
      moreButton.hidden = !continuation || term !== currentQuery;
    } finally {
      if (sequence === requestSequence) {
        busy = false;
        searchButton.disabled = false;
        moreButton.disabled = false;
        drawCandidates();
      }
    }
  }
  add(form, query.wrap, el("div", null, "actions"));
  const actions = form.lastElementChild;
  add(actions, searchButton, moreButton);
  form.addEventListener("submit", (event) => { event.preventDefault(); runSearch(false); });
  add(root, form, status, errors, results);
}
async function viewRecipePhoto(photoSource) {
  const modal = $("#private-photo-modal");
  const root = $("#private-photo-content");
  if (modal.open) modal.close();
  root.replaceChildren();
  const heading = el("h2", "Privat bilde av oppskriftssiden");
  heading.id = "private-photo-title";
  const status = el("p", "Henter originalbildet …", "muted");
  const close = button("Lukk", () => modal.close(), "quiet");
  close.ariaLabel = "Lukk originalbildet";
  add(root, add(el("div", null, "dialog-header"), heading, close), status);
  let objectUrl = null;
  const release = () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    modal.removeEventListener("close", release);
  };
  modal.addEventListener("close", release);
  modal.showModal();
  try {
    const response = await fetch("/api/recipe-photos/" + encodeURIComponent(photoSource.id), {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Det private originalbildet er ikke tilgjengelig akkurat nå.");
    const imageBlob = await response.blob();
    objectUrl = URL.createObjectURL(imageBlob);
    if (!modal.open || !status.isConnected) {
      release();
      return;
    }
    const image = el("img", null, "recipe-photo-original");
    image.src = objectUrl;
    image.alt = "Originalt bilde av oppskriftssiden";
    status.replaceWith(image);
  } catch (error) {
    if (modal.open && status.isConnected) status.textContent = error.message;
  }
}
function unmappedIngredientsDialog(options = {}) {
  const root = openDialog("Ukartlagte ingredienser");
  const modal = $("#modal");
  modal.dataset.kind = "unmapped-ingredients";
  return window.KitchenUnmappedIngredientsView.renderReviewDialog(root, {
    ...options,
    api,
    notify,
    locale: state.household?.language || "nb",
    onMapped: async () => {
      try {
        state.recipes = await api("/recipes?include_stats=true");
        if (state.view === "recipes") render();
      } catch {}
    },
    onClose: () => modal.close(),
  });
}
function addRecipeForm() {
  const root = openDialog("Legg til oppskrift");
  add(
    root,
    el("p", "Velg hvordan du vil starte. Oppskriften lagres først når du ser over og velger Lagre.", "muted"),
  );
  add(
    root,
    add(
      el("div", null, "actions recipe-acquisition-options"),
      button("Finn oppskrifter fra kilder", discoveryForm),
      button("Importer fra en lenke", importForm),
      button("Fotografer en oppskriftsside", recipePhotoForm),
      button("Importer oppskrifter fra Oda / Cookidoo", () => recipeCollectionForm()),
      button("Skriv inn selv", () => recipeForm(), "quiet"),
    ),
  );
}
function recipePhotoForm() {
  const root = openDialog("Fotografer en oppskriftsside");
  add(
    root,
    el(
      "p",
      "Bildet lagres privat i husholdningen. KI transkriberer siden til et redigerbart utkast; du kontrollerer det før du lagrer oppskriften.",
      "callout",
    ),
  );
  if (!recipeAssistantConfigured())
    add(
      root,
      add(
        el("section", null, "callout connection-prompt"),
        el("p", "KI-tilkobling er nødvendig for å lese teksten fra bildet."),
        connectionSettingsButton(),
      ),
    );
  const form = el("form");
  const formModal = $("#modal");
  const isCurrentDialog = () => form.isConnected && formModal.open;
  const image = field("Bilde av oppskriftssiden", "file", "", "JPEG, PNG eller WebP · maks 8 MiB");
  image.input.accept = "image/jpeg,image/png,image/webp";
  image.input.capture = "environment";
  image.input.required = true;
  const book = field("Kokebok (valgfritt)", "text", "");
  book.input.maxLength = 300;
  const page = field("Side (valgfritt)", "text", "");
  page.input.maxLength = 40;
  const status = el("p", "", "muted");
  status.setAttribute("role", "status");
  const retry = button("Prøv å lese bildet på nytt", async () => {
    if (!photoSource?.id) return;
    retry.disabled = true;
    status.textContent = "Prøver å lese siden på nytt …";
    try {
      await window.KitchenRecipePhoto.applyIfActive(
        api("/recipe-photos/" + encodeURIComponent(photoSource.id) + "/redraft", "POST", {}),
        isCurrentDialog,
        showPhotoDraft,
      );
    } catch (error) {
      status.textContent = error.message;
    } finally {
      retry.disabled = false;
    }
  }, "quiet");
  retry.hidden = true;
  let photoSource = null;
  function showPhotoDraft(result) {
    photoSource = result?.photo_source || photoSource;
    if (result?.status === "draft_ready" && result.draft) {
      const draft = { ...result.draft, photo_uncertainties: result.uncertainties || [] };
      recipeForm(draft, "ai");
      return;
    }
    status.textContent = "Teksten kunne ikke leses tydelig nok. Kontroller bildet, eller prøv på nytt. Bildet og eventuell tidligere kladd er beholdt.";
    retry.hidden = !photoSource?.id;
  }
  formSubmit(form, "Lag redigerbart utkast", async () => {
    if (!recipeAssistantConfigured()) throw new Error("Sett opp KI-tilkoblingen i innstillingene før du leser inn bildet.");
    status.textContent = "Laster opp bildet og leser oppskriften …";
    retry.hidden = true;
    const payload = await window.KitchenRecipePhoto.buildUpload(image.input.files?.[0], {
      book_title: book.input.value,
      page: page.input.value,
    });
    if (!isCurrentDialog()) return;
    await window.KitchenRecipePhoto.applyIfActive(
      api("/recipe-photos", "POST", payload),
      isCurrentDialog,
      showPhotoDraft,
    );
  });
  add(form, image.wrap, book.wrap, page.wrap, status, retry);
  add(root, form);
}
function recipeCollectionConnectionPrompt(provider = "oda", onConnected) {
  const isCookidoo = provider === "cookidoo";
  const providerName = isCookidoo ? "Cookidoo" : "Oda";
  const settingsLabel = isCookidoo ? "Åpne Cookidoo-innstillinger" : "Åpne Oda-innstillinger";
  const section = add(el("section", null, "callout connection-prompt"));
  const statusObj = isCookidoo
    ? (state.integrations || []).find((item) => item.id === "cookidoo")
    : state.oda;
  if (!statusObj) {
    add(section, el("p", `${providerName}-status kan ikke bekreftes akkurat nå. Prøv å oppdatere status eller åpne Innstillinger.`));
    add(section, connectionSettingsButton(settingsLabel));
    add(section, button(`Prøv å hente ${providerName}-status på nytt`, async () => {
      try {
        if (isCookidoo) {
          await loadConnectionStatus();
          const refreshed = (state.integrations || []).find((item) => item.id === "cookidoo");
          if (refreshed?.status === "connected") {
            if (onConnected) onConnected();
            else recipeCollectionForm(null, "cookidoo");
            return;
          }
        } else {
          state.oda = await api("/oda/status");
          if (state.oda?.connected) {
            if (onConnected) onConnected();
            else recipeCollectionForm(null, "oda");
            return;
          }
        }
        const replacement = recipeCollectionConnectionPrompt(provider, onConnected);
        section.replaceWith(replacement);
      } catch (error) {
        notify(error.message, true);
      }
    }, "quiet"));
    return section;
  }
  const guidance = window.KitchenRecipeCollections.connectionGuidance(statusObj) || "connect_required";
  const message = window.KitchenRecipeCollections.connectionMessage(guidance, provider);
  add(section, el("p", message), connectionSettingsButton(settingsLabel));
  return section;
}
function recipeCollectionForm(resumeJobId = null, initialProvider = null) {
  const activeResumeJobId = typeof resumeJobId === "string" && resumeJobId ? resumeJobId : null;
  let activeProvider = initialProvider === "cookidoo" ? "cookidoo" : "oda";
  if (activeResumeJobId && state.recipeCollectionJob?.job_id === activeResumeJobId && state.recipeCollectionJob?.provider === "cookidoo") {
    activeProvider = "cookidoo";
  }
  const root = openDialog("Importer oppskrifter");
  const collectionModal = $("#modal");
  collectionModal.dataset.recipeCollection = "true";
  const pollOwner = ++state.recipeCollectionPollGeneration;

  const providerTabs = el("div", null, "provider-tabs");
  providerTabs.setAttribute("role", "tablist");
  providerTabs.setAttribute("aria-label", "Velg oppskriftskilde");
  const odaTab = button("Oda", () => selectProvider("oda"), "provider-tab");
  odaTab.dataset.provider = "oda";
  odaTab.setAttribute("role", "tab");
  const cookidooTab = button("Cookidoo", () => selectProvider("cookidoo"), "provider-tab");
  cookidooTab.dataset.provider = "cookidoo";
  cookidooTab.setAttribute("role", "tab");
  add(providerTabs, odaTab, cookidooTab);

  const providerInfo = el("div", null, "collection-provider-info");
  const bodyContainer = el("div", null, "collection-provider-body");
  add(root, providerTabs, providerInfo, bodyContainer);

  let activePollTimer = null;
  function updateProviderTabs() {
    [
      [odaTab, "oda"],
      [cookidooTab, "cookidoo"],
    ].forEach(([tab, id]) => {
      const selected = activeProvider === id;
      tab.className = "provider-tab" + (selected ? " primary active" : " quiet");
      tab.setAttribute("aria-selected", String(selected));
    });
  }
  function updateProviderInfo() {
    providerInfo.replaceChildren();
    if (activeProvider === "cookidoo") {
      add(
        providerInfo,
        el(
          "p",
          "Velg samlingene du vil hente til gjennomgang fra din tilkoblede Cookidoo-konto.",
          "muted",
        ),
        el(
          "p",
          "Importen henter valgte oppskrifter fra Cookidoo og legger til nye oppskrifter i samlingen etter at du har bekreftet valget.",
          "muted",
        ),
      );
    } else {
      add(
        providerInfo,
        el(
          "p",
          "Velg samlingene du vil hente til gjennomgang. Oda kan vise likerklikk og oppskrifter med bestilte ingredienser; et kjøp betyr ikke at retten er laget.",
          "muted",
        ),
        el(
          "p",
          "Oda viser inntil 50 sider à 20 oppskrifter per samling (maks 1 000). Importen leser kildeoppskriften og legger til nye oppskrifter etter at du har valgt dem.",
          "muted",
        ),
      );
    }
    add(
      providerInfo,
      el(
        "p",
        "Ingenting importeres før du bekrefter valget. Etter bekreftelse fortsetter importen selv om du lukker dette vinduet. Du kan følge fremdriften i Oppskrifter og avbryte gjenstående arbeid; oppskrifter som allerede er importert, beholdes.",
        "callout",
      ),
    );
  }
  function isProviderConnected(provider) {
    if (provider === "cookidoo") {
      return (state.integrations || []).find((item) => item.id === "cookidoo")?.status === "connected";
    }
    return Boolean(state.oda?.connected);
  }
  function selectProvider(nextProvider) {
    if (nextProvider === activeProvider) return;
    clearTimeout(activePollTimer);
    activeProvider = nextProvider;
    renderProviderBody(false);
  }

  function renderProviderBody(shouldResume = false) {
    clearTimeout(activePollTimer);
    updateProviderTabs();
    updateProviderInfo();
    bodyContainer.replaceChildren();
    if (!isProviderConnected(activeProvider) && !(shouldResume && activeResumeJobId)) {
      add(
        bodyContainer,
        recipeCollectionConnectionPrompt(activeProvider, () => renderProviderBody(false)),
      );
      return;
    }

    const isCookidoo = activeProvider === "cookidoo";
    const providerName = isCookidoo ? "Cookidoo" : "Oda";
    const collectionChoices = isCookidoo
      ? [
          ["saved", "Lagrede oppskrifter"],
          ["created", "Egne oppskrifter"],
          ["bookmarks", "Bokmerker"],
        ]
      : [
          ["liked", "Oppskrifter jeg liker"],
          ["purchased", "Oppskrifter med ingredienser jeg har bestilt"],
        ];

    const form = el("form");
    const picker = el("fieldset", null, "collection-picker");
    add(picker, el("legend", `${providerName}-samlinger`));
    const selectedKinds = new Set(collectionChoices.map(([kind]) => kind));
    for (const [kind, label] of collectionChoices) {
      const option = el("label", null, "collection-option");
      const checkbox = el("input");
      checkbox.type = "checkbox";
      checkbox.value = kind;
      checkbox.checked = true;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selectedKinds.add(kind);
        else selectedKinds.delete(kind);
      });
      add(option, checkbox, el("span", label));
      picker.append(option);
    }
    const status = el("p", "Velg én eller begge samlinger, og hent en liste til gjennomgang.", "muted");
    status.setAttribute("role", "status");
    const connectionNotice = el("div");
    const errors = el("ul", null, "collection-errors");
    errors.setAttribute("role", "alert");
    const selectionToolbar = el("div", null, "selection-toolbar");
    selectionToolbar.hidden = true;
    const selectedCount = el("p", "0 valgte oppskrifter", "muted");
    const entries = el("div", null, "collection-entries");
    const actions = el("div", null, "actions");
    let job = null;
    let generation = 0;
    let previewStarting = false;
    const selectedIds = new Set();

    const importButton = button("Importer valgte oppskrifter", async () => {
      if (!job || !["ready", "completed"].includes(job.status)) return;
      let request;
      try {
        request = window.KitchenRecipeCollections.importPayload(job.job_id, [...selectedIds]);
      } catch (error) {
        status.textContent = error.message;
        return;
      }
      importButton.disabled = true;
      clearTimeout(activePollTimer);
      const importGeneration = ++generation;
      const importJobId = job.job_id;
      try {
        const nextJob = window.KitchenRecipeCollections.normalizeJob(await api(request.path, "POST", request.body));
        if (importGeneration !== generation || nextJob.job_id !== importJobId) return;
        job = nextJob;
        draw(job);
        schedulePoll();
      } catch (error) {
        if (importGeneration !== generation || job?.job_id !== importJobId) return;
        showCollectionError(error);
        syncSelectionControls();
      }
    }, "primary");
    importButton.textContent = "Bekreft og importer valgte oppskrifter";
    importButton.disabled = true;

    const toggleAllButton = button("Velg alle", () => {
      if (!job) return;
      const nextSelected = window.KitchenRecipeCollections.toggleAllSelection(selectedIds, job);
      selectedIds.clear();
      nextSelected.forEach((id) => selectedIds.add(id));
      entries.querySelectorAll('input[type="checkbox"][data-entry-id]').forEach((box) => {
        box.checked = selectedIds.has(box.dataset.entryId);
      });
      syncSelectionControls();
    }, "quiet small");
    toggleAllButton.dataset.collectionFocus = "toggle-all";
    add(selectionToolbar, toggleAllButton, selectedCount);

    function syncSelectionControls() {
      const selectableCount = job ? window.KitchenRecipeCollections.selectableIds(job).length : 0;
      selectionToolbar.hidden = selectableCount === 0;
      const allSelected = Boolean(job && window.KitchenRecipeCollections.isAllSelected(selectedIds, job));
      toggleAllButton.textContent = allSelected ? "Fjern alle" : "Velg alle";
      toggleAllButton.disabled = !job || ["previewing", "importing"].includes(job.status);
      selectedCount.textContent = `${selectedIds.size} valgte oppskrifter`;
      importButton.disabled = selectedIds.size === 0 || !job || !["ready", "completed"].includes(job.status);
    }

    const retryPreview = button("Hent samlingene på nytt", () => {
      if (job && ["previewing", "importing"].includes(job.status)) {
        schedulePoll();
        return;
      }
      startPreview().catch(showCollectionError);
    }, "quiet");
    retryPreview.hidden = true;
    const cancelJob = button("Avbryt innhenting/import", async () => {
      if (!job?.job_id) return;
      clearTimeout(activePollTimer);
      const cancelGeneration = ++generation;
      const cancelJobId = job.job_id;
      cancelJob.disabled = true;
      try {
        job = window.KitchenRecipeCollections.normalizeJob(
          await api("/recipe-collections/" + encodeURIComponent(cancelJobId) + "/cancel", "POST", {}),
        );
        if (cancelGeneration !== generation || job.job_id !== cancelJobId) return;
        draw(job);
        if (job.status === "cancelled") {
          state.recipes = await api("/recipes?include_stats=true");
          if (state.view === "recipes") await refreshView();
        }
      } catch (error) {
        showCollectionError(error);
      } finally {
        cancelJob.disabled = false;
      }
    }, "quiet");
    cancelJob.hidden = true;
    const previewButton = button(`Hent ${providerName}-samlinger`, () => startPreview().catch(showCollectionError), "primary");
    previewButton.dataset.collectionFocus = "preview";

    function draw(nextJob) {
      job = window.KitchenRecipeCollections.normalizeJob(nextJob);
      if (job.provider === "cookidoo" || job.provider === "oda") {
        activeProvider = job.provider;
        updateProviderTabs();
        updateProviderInfo();
      }
      const currentProviderName = activeProvider === "cookidoo" ? "Cookidoo" : "Oda";
      connectionNotice.replaceChildren();
      rememberRecipeCollectionJob(job);
      const entriesById = new Map(job.entries.map((entry) => [entry.id, entry]));
      for (const id of selectedIds) {
        const entry = entriesById.get(id);
        const importingSelection = job.status === "importing" && entry?.status === "importing";
        if (!entry || (!window.KitchenRecipeCollections.canSelect(entry) && !importingSelection)) selectedIds.delete(id);
      }
      const focusKey = document.activeElement?.dataset?.collectionFocus;
      status.textContent = window.KitchenRecipeCollections.progress(job);
      const messages = window.KitchenRecipeCollections.errorMessages
        ? window.KitchenRecipeCollections.errorMessages(job)
        : [...new Set(job.errors)];
      errors.replaceChildren(...messages.map((message) => el("li", message)));
      if (job.truncated)
        errors.append(el("li", `${currentProviderName}-resultatet nådde sidegrensen. Listen kan være ufullstendig; hent på nytt senere.`));
      entries.replaceChildren();
      if (["ready", "importing", "completed", "cancelled", "failed"].includes(job.status)) {
        if (!job.entries.length) {
          add(entries, empty(
            job.errors.length ? `${currentProviderName} kunne ikke hente oppskriftene` : "Ingen oppskrifter i samlingen",
            `Kontroller ${currentProviderName}-tilkoblingen, eller prøv å hente samlingen på nytt.`,
          ));
        }
        job.entries.forEach((entry) => {
          const card = el("article", null, "card collection-entry");
          const heading = el("div", null, "collection-entry-heading");
          if (window.KitchenRecipeCollections.canSelect(entry)) {
            const choice = el("label", null, "collection-choice");
            const checkbox = el("input");
            checkbox.type = "checkbox";
            checkbox.checked = selectedIds.has(entry.id);
            checkbox.dataset.entryId = entry.id;
            checkbox.dataset.collectionFocus = "entry-" + entry.id;
            checkbox.setAttribute("aria-label", "Velg oppskriften " + entry.title + " for import");
            checkbox.addEventListener("change", () => {
              if (checkbox.checked) selectedIds.add(entry.id);
              else selectedIds.delete(entry.id);
              syncSelectionControls();
            });
            add(choice, checkbox, el("span", entry.title));
            heading.append(choice);
          } else add(heading, el("strong", entry.title));
          const badges = entry.badges.map((badge) => badge === "Liked on Oda" ? "Likt på Oda" : badge === "Ingredients bought on Oda" ? "Ingredienser bestilt på Oda" : badge);
          badges.forEach((badge) => heading.append(el("span", badge, "badge")));
          const details = el("p", window.KitchenRecipeCollections.entryStatus(entry.status), "muted");
          if (typeof entry.metadata?.deliveriesCount === "number" && Number.isFinite(entry.metadata.deliveriesCount) && entry.metadata.deliveriesCount >= 0)
            details.textContent += " · Bestilt " + formatUiCount(entry.metadata.deliveriesCount) + " ganger";
          if (entry.status === "failed") details.textContent += " · Kontroller kildelenken eller prøv importen på nytt.";
          add(card, heading, details);
          if (entry.source_url) {
            try {
              const sourceUrl = new URL(entry.source_url);
              if (["https:", "http:"].includes(sourceUrl.protocol)) {
                const entryProviderName = entry.provider === "cookidoo" ? "Cookidoo" : currentProviderName;
                const link = el("a", `Vis oppskriften hos ${entryProviderName}`, "source-link");
                link.href = sourceUrl.href;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                add(card, link);
              }
            } catch {}
          }
          if (entry.image_url) add(card, recipeImage({ title: entry.title, image_url: entry.image_url }));
          entries.append(card);
        });
      }
      syncSelectionControls();
      retryPreview.hidden = !["failed", "cancelled", "completed", "ready"].includes(job.status);
      retryPreview.textContent = ["previewing", "importing"].includes(job.status) ? "Hent status på nytt" : "Hent samlingene på nytt";
      retryPreview.disabled = false;
      cancelJob.hidden = !["previewing", "importing"].includes(job.status);
      previewButton.hidden = Boolean(job);
      if (focusKey && focusKey !== "toggle-all") {
        entries.querySelector(`[data-collection-focus="${CSS.escape(focusKey)}"]`)?.focus();
      }
    }
    function showCollectionError(error) {
      connectionNotice.replaceChildren();
      const statusObj = activeProvider === "cookidoo"
        ? (state.integrations || []).find((item) => item.id === "cookidoo")
        : state.oda;
      const guidance = window.KitchenRecipeCollections.connectionGuidance(
        statusObj,
        error,
        activeProvider === "cookidoo" ? () => false : window.KitchenSettingsView.odaNeedsReconnect,
      );
      if (guidance) {
        add(connectionNotice, add(
          el("section", null, "callout connection-prompt"),
          el("p", window.KitchenRecipeCollections.connectionMessage(guidance, activeProvider)),
          connectionSettingsButton(activeProvider === "cookidoo" ? "Åpne Cookidoo-innstillinger" : "Åpne Oda-innstillinger"),
        ));
      } else {
        status.textContent = error.message + " Valgene dine er beholdt. Prøv igjen.";
      }
    }
    async function startPreview() {
      if (previewStarting) return;
      if (!selectedKinds.size) throw new Error(`Velg minst én ${providerName}-samling.`);
      const requestedKinds = [...selectedKinds];
      if (job && JSON.stringify([...job.kinds].sort()) !== JSON.stringify([...requestedKinds].sort()) && selectedIds.size) {
        selectedIds.clear();
        status.textContent = "Samlingene er endret, så tidligere valg er fjernet. Velg oppskrifter fra den nye listen.";
      }
      clearTimeout(activePollTimer);
      previewStarting = true;
      const requestGeneration = ++generation;
      previewButton.disabled = true;
      retryPreview.disabled = true;
      importButton.disabled = true;
      try {
        const previewBody = activeProvider === "cookidoo"
          ? { provider: "cookidoo", kinds: requestedKinds }
          : { kinds: requestedKinds };
        const nextJob = window.KitchenRecipeCollections.normalizeJob(
          await api("/recipe-collections", "POST", previewBody),
        );
        if (requestGeneration !== generation) return;
        job = nextJob;
        draw(job);
        schedulePoll();
      } catch (error) {
        if (requestGeneration !== generation) return;
        showCollectionError(error);
        retryPreview.hidden = false;
        retryPreview.disabled = false;
      } finally {
        previewStarting = false;
        previewButton.disabled = false;
      }
    }
    function schedulePoll() {
      clearTimeout(activePollTimer);
      if (!job || !["previewing", "importing"].includes(job.status)) return;
      const watchedGeneration = generation;
      const watchedJobId = job.job_id;
      activePollTimer = setTimeout(async () => {
        if (!form.isConnected || watchedGeneration !== generation || pollOwner !== state.recipeCollectionPollGeneration) return;
        try {
          const nextJob = window.KitchenRecipeCollections.normalizeJob(
            await api("/recipe-collections/" + encodeURIComponent(watchedJobId)),
          );
          if (watchedGeneration !== generation || job?.job_id !== watchedJobId || pollOwner !== state.recipeCollectionPollGeneration) return;
          job = nextJob;
          draw(job);
          if (job.status === "completed" || job.status === "cancelled") {
            state.recipes = await api("/recipes?include_stats=true");
            if (state.view === "recipes") await refreshView();
          }
        } catch (error) {
          if (watchedGeneration !== generation || job?.job_id !== watchedJobId) return;
          showCollectionError(error);
          retryPreview.hidden = false;
          retryPreview.disabled = false;
          return;
        }
        schedulePoll();
      }, 800);
    }
    add(
      form,
      picker,
      status,
      connectionNotice,
      errors,
      selectionToolbar,
      entries,
      el("p", "Oppskrifter som allerede finnes, mangler offentlig kilde eller ikke kan valideres, vises uten valg. Eksisterende oppskrifter blir aldri overskrevet.", "muted"),
      actions,
    );
    add(actions, retryPreview, cancelJob, importButton);
    add(actions, previewButton);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      startPreview().catch(showCollectionError);
    });
    add(bodyContainer, form);
    if (shouldResume && activeResumeJobId) {
      status.textContent = "Henter status for importen …";
      api("/recipe-collections/" + encodeURIComponent(activeResumeJobId))
        .then((response) => {
          const resumed = window.KitchenRecipeCollections.normalizeJob(response);
          if (pollOwner !== state.recipeCollectionPollGeneration || !form.isConnected) return;
          job = resumed;
          draw(job);
          schedulePoll();
        })
        .catch((error) => {
          if (pollOwner === state.recipeCollectionPollGeneration) status.textContent = error.message;
        });
    }
  }

  renderProviderBody(Boolean(activeResumeJobId));
}
function importForm() {
  const root = openDialog("Importer en oppskrift");
  add(
    root,
    el(
      "p",
      "Lim inn en støttet oppskriftslenke. KI lager et redigerbart utkast som du kan se over.",
      "muted",
    ),
  );
  if (!recipeAssistantConfigured())
    add(
      root,
      add(
        el("section", null, "callout connection-prompt"),
        el("p", "KI er nødvendig for å gjøre en oppskriftslenke om til et redigerbart utkast."),
        connectionSettingsButton(),
      ),
    );
  const form = el("form");
  const url = field("Oppskriftslenke", "url");
  url.input.required = true;
  url.input.placeholder = "https://…";
  add(form, url.wrap);
  formSubmit(form, "Lag utkast fra lenke", async () => {
    const response = await api("/recipe-drafts/ai", "POST", { url: url.input.value });
    const draft = response?.adapted || response?.imported;
    if (!draft)
      throw new Error("KI-en ga ikke et redigerbart oppskriftsutkast. Ingenting ble lagret.");
    recipeForm(draft, response?.adapted ? "ai" : "imported");
  }, { settingsOnFailure: true });
  add(root, form);
}
function discoveryForm() {
  const root = openDialog("Finn oppskrifter fra kilder");
  add(
    root,
    el(
      "p",
      "Søk hos Oda, Marmiton og Matprat. Velg et resultat for å lage et utkast fra den nøyaktige kildelenken.",
      "muted",
    ),
  );
  if (!recipeAssistantConfigured())
    add(
      root,
      add(
        el("section", null, "callout connection-prompt"),
        el("p", "Du kan søke i kildene, men KI er nødvendig for å gjøre et resultat om til et redigerbart utkast."),
        connectionSettingsButton(),
      ),
    );
  const form = el("form");
  const query = field(
    "Rett eller ingredienser",
    "search",
    "",
    "For eksempel: laks, dill og poteter.",
  );
  query.input.required = true;
  query.input.maxLength = 200;
  add(root, recentFeedbackPanel((preference) => {
    query.input.value = [query.input.value.trim(), preference].filter(Boolean).join(" ");
    query.input.focus();
    return query.input.value.length > 200
      ? "Matpreferansen er lagt til. Kort ned søket til høyst 200 tegn før du søker i kildene."
      : "Matpreferansen er lagt til. Se over søket og velg «Søk i kildene» når du er klar.";
  }).node);
  const sources = el("fieldset", null, "discovery-source-picker");
  add(sources, el("legend", "Søkekilder"));
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
  formSubmit(form, "Søk i kildene", async () => {
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
      add(statuses, el("h3", "Kildestatus"));
      const list = el("ul", null, "discovery-status-list");
      view.sources.forEach((source) => {
        const line = el("li");
        add(line, el("strong", source.label + ": "), el("span", source.status));
        if (source.detail) add(line, el("span", " — " + source.detail));
        if (source.status === "Søket er fullført")
          add(
            line,
            el(
              "span",
              source.resultCount
                ? " · " + formatUiCount(source.resultCount) + " treff"
                : " · Ingen treff",
            ),
          );
        if (source.indexUrl) {
          const link = el("a", "Besøk kilden");
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
        empty("Fant ingen oppskrifter", "Se kildestatusen ovenfor for informasjon om søket."),
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
        const facts = [
          result.duration
            ? window.KitchenBasketMatchView.formatDuration(result.duration)
            : "",
          result.difficulty,
        ]
          .filter(Boolean)
          .join(" · ");
        if (facts) add(card, el("p", facts, "muted"));
        if (result.detail) add(card, el("p", result.detail, "muted"));
        const actions = el("div", null, "actions");
        if (result.url) {
          const sourceLink = el("a", "Vis kilde");
          sourceLink.href = result.url;
          sourceLink.target = "_blank";
          sourceLink.rel = "noopener noreferrer";
          add(actions, sourceLink);
        }
        if (result.importable) {
          const error = el("p", "", "form-error discovery-import-error");
          error.setAttribute("role", "alert");
          const settingsLink = connectionSettingsButton("Se over innstillingene for oppskriftsassistenten");
          settingsLink.hidden = true;
          const importButton = button(
            "Importer for gjennomgang",
            async () => {
              importButton.disabled = true;
              importButton.textContent = "Lager utkast …";
              error.textContent = "";
              try {
                const draft = await api("/recipe-drafts/ai", "POST", {
                  url: result.url,
                });
                if (!draft?.adapted)
                  throw new Error(
                    "KI-en ga ikke et redigerbart utkast. Ingenting ble lagret.",
                  );
                if (!importButton.isConnected) return;
                recipeForm(draft.adapted, "ai");
              } catch (err) {
                error.textContent = err.message;
                settingsLink.hidden = false;
              } finally {
                if (importButton.isConnected) {
                  importButton.disabled = false;
                  importButton.textContent = "Importer for gjennomgang";
                }
              }
            },
            "primary small",
          );
          add(actions, importButton);
          add(card, actions, error, settingsLink);
        } else {
          add(actions, el("span", "Dette resultatet kan ikke importeres ennå.", "muted"));
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
  let shareFallback = null;
  add(
    body,
    recipeImage(recipe),
    el(
      "p",
      "Til " + formatUiCount(view.servings) + " porsjoner · den lagrede oppskriften er for " + formatUiCount(recipe.servings),
      "callout",
    ),
    el("h3", "Ingredienser"),
  );
  const ingredients = el("ul");
  view.ingredients.forEach((item) => {
    const presentation = window.KitchenPlannedCooking.formatPlannedIngredient(
      item,
      (quantity) => window.KitchenShoppingView.formatQuantityForDisplay(quantity),
    );
    const row = el("li");
    add(row, el("span", presentation.text));
    const estimate = purchaseEstimateText(item.purchase_estimate);
    row.setAttribute("aria-label", [presentation.accessibleLabel, estimate].filter(Boolean).join(". "));
    if (estimate) add(row, el("small", estimate, "muted purchase-estimate"));
    add(ingredients, row);
  });
  const provenance = recipe.provenance;
  if (provenance?.original_source)
    add(body, el("p", "Tilpasset fra " + recipeOriginLabel(provenance.original_source) + ".", "muted"));
  if (recipe.source_url) {
    const url = window.KitchenRecipeDiscovery.safeHttpsUrl(recipe.source_url);
    if (url) {
      const link = el("a", "Opprinnelig oppskrift", "source-link");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      add(body, link);
    }
  }
  const steps = el("ol");
  recipe.instructions.forEach((step) => add(steps, el("li", step)));
  add(body, ingredients, el("h3", "Fremgangsmåte"), steps);
  const shareIngredients = button("Del ingrediensene", async () => {
    const text = window.KitchenPlannedCooking.formatPlannedIngredientList(
      view,
      (quantity) => window.KitchenShoppingView.formatQuantityForDisplay(quantity),
    );
    if (typeof navigator.share === "function") {
      try {
        await navigator.share({ title: recipe.title, text });
        notify("Ingrediensene er delt.");
        return;
      } catch (error) {
        if (error?.name === "AbortError") return;
      }
    }
    if (typeof navigator.clipboard?.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        notify("Ingredienslisten er kopiert.");
        return;
      } catch {}
    }
    if (!shareFallback) {
      const content = el("textarea");
      content.setAttribute("aria-label", "Ingrediensliste som skal kopieres");
      content.readOnly = true;
      content.rows = Math.min(14, Math.max(5, text.split("\n").length));
      shareFallback = add(
        el("details", null, "shopping-info"),
        el("summary", "Kopier ingrediensene manuelt"),
        content,
      );
      shareFallback.open = true;
      add(body, shareFallback);
    }
    const content = shareFallback.querySelector("textarea");
    content.value = text;
    content.focus();
    content.select();
    notify("Marker og kopier ingredienslisten.");
  }, "quiet");
  add(
    body,
    add(
      el("div", null, "actions"),
      shareIngredients,
      button("Vis mengdene i den lagrede oppskriften", () => viewRecipe(recipe), "quiet"),
    ),
  );
  add(root, body);
}
function shoppingOrigins(origins = []) {
  const labels = new Set();
  origins.forEach((origin) => {
    if (origin.startsWith("meal:")) labels.add("Måltidsplan");
    else if (origin.startsWith("item:")) labels.add("Hurtigtillegg");
    else if (origin.startsWith("regular:")) labels.add("Fast vare");
  });
  return [...labels].join(", ");
}
function categoryLabel(category) {
  return window.KitchenShoppingView.categoryLabel(category);
}
function shoppingSections() {
  const nav = el("div", null, "section-tabs");
  if (state.shoppingPanel === "single") {
    const more = el("details", null, "shopping-more");
    add(more, el("summary", "Flere handlelistevalg"));
    const actions = add(el("div", null, "actions"));
    [
      ["needs", "Administrer handlebehov og hverdagsvarer"],
      ["oda", "Se over Oda-handlekurven"],
      ["pantry", "Administrer det du har hjemme"],
      ["in_store", "Del handlelisten eller se plukkstatus"],
    ].forEach(([key, label]) => actions.append(button(label, async () => {
      state.shoppingPanel = key;
      await refreshView();
    }, "quiet small")));
    add(more, actions);
    nav.append(more);
    nav.append(button("Registrer handletur", async () => {
      state.shoppingPanel = "complete_trip";
      await refreshView();
    }, "primary"));
  } else {
    [
      ["single", "Handleliste"],
      ["needs", "Behov og forslag"],
      ["oda", "Oda-handlekurv"],
      ["in_store", "Del og plukk"],
      ["pantry", "Hjemme"],
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
  }
  const refresh = button(
    state.shoppingRefreshInFlight ? "Oppdaterer …" : "Oppdater",
    () => refreshShoppingIfSafe(true),
    "quiet small",
  );
  refresh.id = "shopping-refresh";
  refresh.disabled = state.shoppingRefreshInFlight;
  refresh.setAttribute(
    "aria-label",
    state.shoppingRefreshInFlight
      ? "Oppdaterer delt handleliste"
      : "Oppdater delt handleliste",
  );
  const status = el("span", shoppingRefreshLabel(), "shopping-refresh-status");
  status.id = "shopping-refresh-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  add(nav, add(el("div", null, "shopping-refresh-controls"), refresh, status));
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
  const shareTitle = "Handleliste · " + rangeTitle;
  const sharedText = () =>
    window.KitchenShoppingShare.formatRemainingShoppingText(
      shareTitle,
      remaining,
      state.household.shopping_category_order || [],
    );
  const copyDialog = (text, explanation) => {
    const root = openDialog("Kopier handlelisten");
    add(root, el("p", explanation, "muted"));
    const content = el("textarea");
    content.setAttribute("aria-label", "Tekst for varer som gjenstår på handlelisten");
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
          ? "Deling og utklippstavle er ikke tilgjengelig her. Merk teksten og kopier den til en annen app."
          : "Utklippstavlen er ikke tilgjengelig her. Merk teksten og kopier den til en annen app.",
      );
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      notify(
        forShare
          ? "Deling er ikke tilgjengelig, så handlelisten er kopiert. Lim den inn i appen du ønsker."
          : "Varene som gjenstår, er kopiert.",
      );
    } catch {
      copyDialog(
        text,
        forShare
          ? "Deling og utklippstavle er ikke tilgjengelig her. Merk teksten og kopier den til en annen app."
          : "Tilgang til utklippstavlen ble blokkert. Merk teksten og kopier den til en annen app.",
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
      notify("Handlelisten er delt.");
    } catch (error) {
      if (error?.name === "AbortError") return;
      notify("Fikk ikke åpnet delingsmenyen. Bruk Kopier varer som gjenstår i stedet.", true);
    }
  };
  const share = button("Del varer som gjenstår", shareRemaining, "quiet");
  const copy = button("Kopier varer som gjenstår", () => copyRemaining(), "quiet");
  share.disabled = copy.disabled = remaining.length === 0;
  const progressData = window.KitchenShoppingView.pickupProgress(
    trip.checked_count,
    trip.total_count,
  );
  const progress = el("progress");
  progress.max = 100;
  progress.value = progressData.percent;
  progress.setAttribute("aria-label", "Fremdrift for plukking på handleturen");
  const progressSummary = add(
    el("div", null, "trip-progress"),
    el("strong", progressData.label),
    progress,
  );
  add(
    main,
    add(el("div", null, "trip-toolbar"), progressSummary, add(el("div", null, "actions shopping-share-actions"), share, copy)),
  );
  add(main, button("Registrer handletur", async () => {
    state.shoppingPanel = "complete_trip";
    await refreshView();
  }, "quiet"));
  const pickupInfo = el("details", null, "shopping-info");
  add(
    pickupInfo,
    el("summary", "Om plukkestatus"),
    el(
      "p",
      "Når du krysser av en vare, betyr det at du har plukket den på denne handleturen. Det registrerer ikke et kjøp og endrer ikke mengdene du har hjemme.",
    ),
  );
  if (!trip.lines.length) {
    add(
      main,
      empty(
        "Ingen varer å plukke",
        "Legg først til måltider eller handlebehov. Her vises bare mengdene du fortsatt trenger etter at det du har hjemme, er trukket fra.",
        button(
          "Se over handlebehov",
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
      el("h2", categoryLabel(category)),
    );
    const list = el("ul", null, "list");
    lines.forEach((line) => {
      const row = el("li", null, line.checked ? "completed" : "");
      const check = el("input");
      check.type = "checkbox";
      check.checked = Boolean(line.checked);
      check.setAttribute(
        "aria-label",
        (check.checked ? "Fjern avkryssing for " : "Merk som plukket: ") + line.name,
      );
      check.addEventListener("change", async () => {
        check.disabled = true;
        state.shoppingWriteInFlight += 1;
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
          state.shoppingWriteInFlight = Math.max(0, state.shoppingWriteInFlight - 1);
          check.disabled = false;
          queuePendingShoppingRefresh();
        }
      });
      const detail = add(el("div", null, "detail"), el("strong", line.name));
      const exactQuantity = String(line.to_buy_quantity ?? "0");
      const unit = line.unit || "";
      const displayUnitText = unit ? window.KitchenShoppingView.displayUnit(unit, false, exactQuantity) : "";
      const accessibleUnitText = unit ? window.KitchenShoppingView.displayUnit(unit, true, exactQuantity) : "";
      const quantity = el(
        "small",
        window.KitchenShoppingView.formatQuantityForDisplay(exactQuantity) +
          (displayUnitText ? " " + displayUnitText : "") +
          " å plukke" +
          (line.origins?.length ? " · " + shoppingOrigins(line.origins) : ""),
      );
      quantity.setAttribute(
        "aria-label",
        "Mengde: " + formatUiAccessibleQuantity(exactQuantity) + (accessibleUnitText ? " " + accessibleUnitText : "") + " å plukke",
      );
      quantity.title = "Mengde: " + formatUiAccessibleQuantity(exactQuantity) + (accessibleUnitText ? " " + accessibleUnitText : "");
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

function renderTripCompletion(main) {
  const review = state.tripCompletionReview;
  if (!review) {
    add(main, empty("Gjennomgangen er ikke lastet", "Oppdater for å hente dagens behov."));
    return;
  }
  add(main, el("p", "Planlagt behov vises som kontekst. Det blir aldri registrert som kjøpt mengde automatisk. Registrer bare det du faktisk kjøpte; la resten stå urørt." , "muted"));
  if (state.tripCompletionRetry?.token === review.review_token) add(main, el("p", "Forrige lagring kan ha nådd kjøkkenet. Feltene er låst og nytt forsøk sender nøyaktig samme registrering med samme idempotensnøkkel.", "muted"));
  const date = el("input"); date.type = "date"; date.value = iso(new Date()); date.required = true;
  date.max = iso(new Date()); date.setAttribute("aria-label", "Kjøpsdato");
  const controls = add(el("section", null, "panel"), el("h2", "Faktiske kjøp"), el("label", "Kjøpsdato"), date);
  const lineForms = [];
  (review.lines || []).forEach((reviewLine) => {
    const selectedProducts = Array.isArray(reviewLine.selected_products)
      ? reviewLine.selected_products.filter((selection) => {
          const product = selection?.product;
          return (
            product &&
            typeof product.provider === "string" &&
            product.provider.trim() &&
            typeof product.product_id === "string" &&
            product.product_id.trim() &&
            typeof product.name === "string" &&
            product.name.trim() &&
            Number.isFinite(Number(product.pack_quantity)) &&
            Number(product.pack_quantity) > 0 &&
            typeof product.pack_unit === "string" &&
            product.pack_unit.trim() &&
            Number.isInteger(selection.packages) &&
            selection.packages > 0
          );
        })
      : [];
    const line = { ...reviewLine, selected_products: selectedProducts };
    const section = add(el("section", null, "panel trip-completion-line"), el("h3", line.name));
    const plannedUnit = line.unit ? window.KitchenShoppingView.displayUnit(line.unit, false, line.to_buy_quantity) : "";
    add(section, el("p", `Planlagt behov: ${formatUiQuantity(line.to_buy_quantity)}${plannedUnit ? " " + plannedUnit : ""} · kontekst, ikke faktisk kjøp.`, "muted"));
    const bought = el("input"); bought.type = "checkbox"; bought.setAttribute("aria-label", `Registrer faktisk kjøp: ${line.name}`);
    add(section, add(el("label"), bought, el("span", "Jeg kjøpte denne varen")));
    const quantity = el("input"); quantity.type = "number"; quantity.min = "0.0000001"; quantity.step = "any"; quantity.placeholder = "Ukjent"; quantity.setAttribute("aria-label", `Faktisk mengde kjøpt: ${line.name}`);
    const unit = el("input"); unit.placeholder = line.unit || "Enhet"; unit.setAttribute("aria-label", `Enhet for faktisk kjøp: ${line.name}`);
    add(section, el("label", "Faktisk mengde (valgfritt)"), quantity, el("label", "Enhet (valgfritt)"), unit);
    const exact = el("input"); exact.type = "checkbox"; exact.setAttribute("aria-label", `Kjøp eksakt produkt for ${line.name}`);
    const productSelect = el("select");
    productSelect.setAttribute("aria-label", `Velg vist produkt for ${line.name}`);
    productSelect.append(new Option("Velg vist produkt", ""));
    (line.selected_products || []).forEach((entry, index) => {
      const p = entry.product;
      const option = new Option(`${p.name} · ${p.pack_quantity} ${p.pack_unit} per pakke · ${p.provider} / ${p.product_id}`, String(index));
      productSelect.append(option);
    });
    const packCount = el("input"); packCount.type = "number"; packCount.min = "1"; packCount.max = "99"; packCount.step = "1"; packCount.placeholder = "Antall pakker"; packCount.setAttribute("aria-label", `Antall pakker kjøpt: ${line.name}`);
    const productDetails = el("div");
    productSelect.addEventListener("change", () => {
      productDetails.replaceChildren();
      const entry = line.selected_products?.[Number(productSelect.value)];
      if (!entry) return;
      const p = entry.product;
      if (p.image_url) { const img = el("img"); img.src = p.image_url; img.alt = `Bilde av ${p.name}`; img.width = 72; productDetails.append(img); }
      add(productDetails, el("p", `${p.name} · ${p.provider} / ${p.product_id} · ${p.pack_quantity} ${p.pack_unit} per pakke`));
      if (p.url) { const link = el("a", "Åpne produktreferanse"); link.href = p.url; link.rel = "noopener noreferrer"; link.target = "_blank"; productDetails.append(link); }
    });
    const hasExactProduct = selectedProducts.length > 0;
    if (hasExactProduct) {
      add(section, el("p", "Dette er lagrede produktvalg for varen. Tilgjengeligheten hos Oda er ikke kontrollert nå.", "muted"));
      add(section, add(el("label"), exact, el("span", "Jeg kjøpte et av de valgte produktene")));
      add(section, productSelect, packCount, productDetails);
    } else {
      const explanation = !state.oda
        ? "Oda-status er ikke tilgjengelig. Oppgi faktisk mengde, eller la den stå ukjent."
        : !state.oda.connected
          ? "Oda er ikke tilkoblet. Oppgi faktisk mengde, eller la den stå ukjent."
          : "Ingen valgte Oda-produkter for denne varen. Oppgi faktisk mengde, eller la den stå ukjent.";
      add(section, el("p", explanation, "muted"));
    }
    const sourceChecks = [];
    const sources = line.source_quantities || [];
    if (sources.length) {
      const sourceDetails = el("details", null, "trip-source-details");
      add(sourceDetails, el("summary", `Behovskilder (${sources.length})`));
      add(sourceDetails, el("p", "Velg hvilke måltider eller tillegg som dekkes og lukkes nå. Uavkryssede bidrag forblir åpne på handlelisten.", "muted trip-source-help"));
      sources.forEach((source) => {
        const input = el("input"); input.type = "checkbox";
        const kindLabel = source.kind === "meal" ? "Lukk måltidsbehov" : source.kind === "capture" ? "Lukk hurtigtillegg" : "Lukk fast vare";
        let context = "";
        if (source.kind === "meal") {
          const meal = (state.meals || []).find((item) => item.id === source.source_id);
          const recipe = meal && (state.recipes || []).find((item) => item.id === meal.recipe_id);
          const mealLabel = meal?.label || recipe?.title;
          context = meal ? [mealLabel, meal.date].filter(Boolean).join(" · ") : `Måltid ${String(source.source_id || "").slice(0, 8)}`;
        } else if (source.kind === "capture") {
          const capture = (line.captures || []).find((item) => item.id === source.source_id);
          context = capture?.label || "Hurtigtillegg";
        } else {
          context = "Fast vare";
        }
        const sourceUnit = source.unit ? window.KitchenShoppingView.displayUnit(source.unit, false, source.quantity) : "";
        const formattedQty = `${formatUiQuantity(source.quantity)}${sourceUnit ? " " + sourceUnit : ""}`;
        const detailLabel = `${kindLabel} · ${context} · ${formattedQty}`;
        input.setAttribute("aria-label", `${kindLabel}: ${context} (${formattedQty})`);
        add(sourceDetails, add(el("label", null, "trip-source-choice"), input, el("span", detailLabel)));
        let regularPurchase = null;
        if (source.kind === "capture") {
          const capture = (line.captures || []).find((item) => item.id === source.source_id);
          if (capture?.regular_id) {
            regularPurchase = el("input"); regularPurchase.type = "checkbox";
            regularPurchase.setAttribute("aria-label", `Oppdater sist kjøpt for hverdagsvare: ${context}`);
            add(sourceDetails, add(el("label", null, "trip-source-choice"), regularPurchase, el("span", "Oppdater sist kjøpt for den tilknyttede hverdagsvaren")));
          }
        }
        sourceChecks.push({ source, input, regularPurchase });
      });
      add(section, sourceDetails);
    }
    const reason = el("select");
    reason.setAttribute("aria-label", "Lukk uten å registrere kjøp");
    [["", "Velg hvorfor dette behovet lukkes"], ["already_have", "Hadde den allerede"], ["not_needed", "Trengte den ikke"], ["unavailable", "Var utilgjengelig"], ["other", "Annet"]].forEach(([value, label]) => reason.append(new Option(label, value)));
    add(section, el("label", "Lukk uten å registrere kjøp"), reason);
    lineForms.push({ line, bought, quantity, unit, exact, productSelect, packCount, sourceChecks, reason });
    add(main, section);
  });
  const retry = state.tripCompletionRetry?.token === review.review_token
    ? state.tripCompletionRetry
    : null;
  if (retry) {
    date.value = retry.payload.purchased_on || date.value;
    lineForms.forEach(({ line, bought, quantity, unit, exact, productSelect, packCount, sourceChecks, reason }) => {
      const purchase = retry.payload.purchases.find((item) => item.need_id === line.id);
      if (purchase) {
        bought.checked = true;
        quantity.value = purchase.quantity == null ? "" : String(purchase.quantity);
        unit.value = purchase.unit == null ? "" : String(purchase.unit);
        if (purchase.product) {
          exact.checked = true;
          const selectedIndex = (line.selected_products || []).findIndex(({ product }) =>
            product.provider === purchase.product.provider && product.product_id === purchase.product.product_id,
          );
          if (selectedIndex >= 0) productSelect.value = String(selectedIndex);
          packCount.value = String(purchase.product.packages ?? "");
        }
      }
      sourceChecks.forEach(({ source, input, regularPurchase }) => {
        const sourceField = window.KitchenTripCompletion.purchaseSourceField(source.kind);
        input.checked = Boolean(purchase?.[sourceField]?.includes(source.source_id)) ||
          retry.payload.source_closures.some((closure) =>
            closure.need_id === line.id && closure.source_kind === source.kind && closure.source_id === source.source_id,
          );
        if (regularPurchase) {
          const capture = (line.captures || []).find((item) => item.id === source.source_id);
          regularPurchase.checked = Boolean(capture && purchase?.regular_ids?.includes(capture.regular_id));
        }
      });
      const closure = retry.payload.source_closures.find((item) => item.need_id === line.id);
      if (closure) reason.value = closure.resolution;
    });
  }
  add(main, controls);
  const submit = button("Lagre registrering", async () => {
    const actual = [], sourceClosures = [];
    if (!date.value) { notify("Oppgi datoen for handleturen.", true); return; }
    for (const form of lineForms) {
      const { line, bought, quantity, unit, exact, productSelect, packCount, sourceChecks, reason } = form;
      if (bought.checked && exact.checked && (quantity.value.trim() || unit.value.trim())) { notify(`Velg enten eksakt produkt eller faktisk mengde med enhet for ${line.name}.`, true); return; }
      if (bought.checked && reason.value) { notify(`Velg enten faktisk kjøp eller en avslutning uten kjøp for ${line.name}.`, true); return; }
      if (!bought.checked && sourceChecks.some(({ regularPurchase }) => regularPurchase?.checked)) { notify(`Oppdatering av sist kjøpt gjelder bare et faktisk kjøp av ${line.name}.`, true); return; }
      const selectedSources = sourceChecks.filter(({ input }) => input.checked).map(({ source }) => source);
      if (bought.checked) {
        const purchase = { need_id: line.id };
        if (exact.checked) {
          const product = window.KitchenTripCompletion.exactProductPurchase(line, productSelect.value, packCount.value);
          if (!product) { notify(`Velg et vist produkt og skriv inn antall pakker for ${line.name}.`, true); return; }
          purchase.product = product;
        } else if (quantity.value.trim() || unit.value.trim()) {
          if (!quantity.value.trim() || !unit.value.trim()) { notify(`Fyll inn både mengde og enhet for ${line.name}, eller la begge stå tomme.`, true); return; }
          purchase.quantity = quantity.value; purchase.unit = unit.value.trim();
        }
        sourceChecks.filter(({ input }) => input.checked).forEach(({ source, regularPurchase }) => {
          const field = window.KitchenTripCompletion.purchaseSourceField(source.kind);
          if (field) (purchase[field] ||= []).push(source.source_id);
          if (regularPurchase?.checked) {
            const capture = (line.captures || []).find((item) => item.id === source.source_id);
            if (capture?.regular_id) (purchase.regular_ids ||= []).push(capture.regular_id);
          }
        });
        actual.push(purchase);
      } else if (reason.value) {
        if (!selectedSources.length) { notify(`Velg hvilke bidrag som lukkes for ${line.name}.`, true); return; }
        selectedSources.forEach((source) => sourceClosures.push({ need_id: line.id, source_kind: source.kind, source_id: source.source_id, resolution: reason.value }));
      } else if (selectedSources.length) {
        notify(`Velg hvorfor valgte bidrag lukkes for ${line.name}, eller fjern avkrysningen.`, true); return;
      }
    }
    if (!actual.length && !sourceClosures.length) { notify("Registrer et faktisk kjøp eller velg bidrag som lukkes uten kjøp.", true); return; }
    const payload = state.tripCompletionRetry?.token === review.review_token
      ? state.tripCompletionRetry.payload
      : { start: review.start, end: review.end, review_token: review.review_token, reviewed: true, idempotency_key: crypto.randomUUID(), purchased_on: date.value, purchases: actual, source_closures: sourceClosures };
    state.tripCompletionRetry = { key: payload.idempotency_key, token: review.review_token, payload };
    submit.disabled = true;
    main.querySelectorAll("input, select").forEach((control) => { control.disabled = true; });
    try {
      await api("/store-trip/completions", "POST", payload);
      state.tripCompletionRetry = null;
      await refreshView();
      notify("Handleturen er registrert.");
    } catch (error) {
      if (/Shopping needs changed|need is no longer current|source is no longer current/i.test(error.message)) {
        state.tripCompletionRetry = null;
        await refreshView();
        notify("Behovene endret seg. Gjennomgangen er oppdatert; kontroller den før du lagrer.", true);
      } else if (error.status === 422) {
        state.tripCompletionRetry = null;
        main.querySelectorAll("input, select").forEach((control) => { control.disabled = false; });
        notify(error.message + " Rett opp feltene og prøv igjen.", true);
      } else notify(error.message + " Prøv igjen for å gjenta samme registrering.", true);
    } finally { submit.disabled = false; }
  }, "primary");
  if (state.tripCompletionRetry?.token === review.review_token) {
    submit.textContent = "Prøv lagring igjen";
    main.querySelectorAll("input, select").forEach((control) => { control.disabled = true; });
  }
  add(main, submit);
}
function inventoryForm(item = {}) {
  const root = openDialog(
    item.id ? "Oppdater det du har" : "Legg til i det du har hjemme",
  );
  const form = el("form");
  const name = field("Varenavn", "text", item.name || "");
  name.input.required = true;
  if (item.id) name.input.readOnly = true;
  const quantity = field("Mengde du har", "number", item.quantity ?? "0");
  quantity.input.min = "0";
  quantity.input.step = "any";
  quantity.input.required = true;
  const unit = field("Enhet", "text", item.unit || "item");
  unit.input.required = true;
  if (item.id) unit.input.readOnly = true;
  const category = field(
    "Kategori",
    "text",
    item.category || "Other",
    "Brukes til å sortere handlelisten for butikken.",
  );
  category.input.maxLength = 60;
  const note = field("Merknad (valgfritt)", "text", item.note || "");
  note.input.maxLength = 500;
  add(
    form,
    name.wrap,
    add(el("div", null, "form-grid"), quantity.wrap, unit.wrap),
    category.wrap,
    note.wrap,
  );
  formSubmit(form, item.id ? "Lagre mengde" : "Legg til hjemme", async () => {
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
    notify("Mengden du har hjemme, er lagret.");
  });
  add(root, form);
}
function renderInventory(main) {
  add(
    main,
    el(
      "p",
      "Hold oversikt over hvor mye du har hjemme. Kjøkkenet trekker mengden fra handlebehovet. Avkryssing av en vare i butikken endrer ikke beholdningen.",
      "muted",
    ),
  );
  if (!state.inventory.length) {
    add(
      main,
      empty(
        "Du har ikke lagt til noe hjemme ennå",
        "Legg til det du allerede har, så viser handlelisten hva du fortsatt trenger.",
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
          formatUiQuantity(item.quantity) +
            " " +
            item.unit +
            " · " +
            categoryLabel(item.category) +
            (item.note ? " · " + item.note : ""),
        ),
      ),
      button("Rediger", () => inventoryForm(item), "quiet small"),
      button(
        "Fjern",
        async () => {
          try {
            await api("/inventory/" + encodeURIComponent(item.id), "DELETE");
            await refreshView();
            notify("Varen er fjernet fra det du har hjemme.");
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
function toggleBasketBatchChoice(candidate, need, matchJobId, confirmIdentity = false) {
  if (state.basketBatchSaving) return;
  const modalContent = $("#modal-content");
  const activeElement = document.activeElement;
  const focusKey = $("#modal").open && modalContent.contains(activeElement)
    ? activeElement?.dataset?.focusKey
    : null;
  if (!matchJobId || matchJobId !== state.basketBatchReviewJobId) {
    notify("Produktforslagene er endret. Oppdater listen før du godkjenner dem.", true);
    return;
  }
  const key = String(need.id);
  const { start, end } = currentBasketMatchDates();
  const existing = state.basketBatchChoices[key];
  if (existing?.candidate_id === candidate.id && existing?.match_job_id === matchJobId &&
      existing?.start === start && existing?.end === end)
    delete state.basketBatchChoices[key];
  else {
    state.basketBatchChoices[key] = {
      need_id: key,
      candidate_id: candidate.id,
      name: candidate.name,
      packages: candidate.recommended_packages,
      fulfillment: String(candidate.recommended_fulfillment_quantity),
      unit: need.unit,
      match_job_id: matchJobId,
      confirm_identity: confirmIdentity === true,
      start,
      end,
    };
  }
  state.basketBatchSaveError = "";
  state.basketBatchNeedsCorrection = false;
  openBasketBatchReview();
  if (focusKey?.startsWith("basket-batch-candidate:"))
    [...$("#modal-content").querySelectorAll("[data-focus-key]")]
      .find((node) => node.dataset.focusKey === focusKey)
      ?.focus({ preventScroll: true });
}
function basketBatchRecommendation(candidate, unit) {
  if (typeof candidate?.id !== "string" || candidate.id.length > 200) return null;
  const recommendation = window.KitchenBasketMatchView.selectionRecommendation(candidate, unit);
  return recommendation?.packages <= 99 ? recommendation : null;
}
function basketBatchIdentityStatus(candidate) {
  return window.KitchenBasketMatchView.identityStatus(candidate);
}
function basketBatchSelectableCandidate(candidate, unit) {
  return candidate?.available !== false && Boolean(basketBatchRecommendation(candidate, unit));
}
function clearStaleBasketBatchIdentityConfirmations() {
  const { start, end } = currentBasketMatchDates();
  Object.values(state.basketBatchChoices).forEach((choice) => {
    if (choice.confirm_identity !== true) return;
    const result = state.basketMatchPages[String(choice.need_id)]?.[1];
    const candidate = (result?.candidates || []).find((value) => value.id === choice.candidate_id);
    if (choice.match_job_id !== state.basketBatchReviewJobId || choice.start !== start ||
        choice.end !== end || result?.match_job_id !== state.basketBatchReviewJobId ||
        basketBatchIdentityStatus(candidate) !== "unverified" ||
        !basketBatchSelectableCandidate(candidate, choice.unit))
      choice.confirm_identity = false;
  });
}
function basketBatchSkipReason(need, result) {
  if (state.basketBatchNeedsRefresh) return "Treffene er utdaterte og må oppdateres før ny gjennomgang.";
  if (state.basketMatchBusy[String(need.id)]) return "Produktsøket pågår.";
  if (state.basketMatchErrors[String(need.id)]) return "Produktsøket må prøves på nytt.";
  if (!result) return "Ingen ferske produktforslag ennå.";
  if (result.match_job_id !== state.basketBatchReviewJobId)
    return "Oppdaterte forslag mangler for denne varen.";
  const candidates = result.candidates || [];
  if (!candidates.length)
    return result.status === "no_results"
      ? "Ingen produkter ble funnet. Oppdater søket eller hopp over varen."
      : window.KitchenBasketMatchView.statusLabel(result.status);
  if (candidates.every((candidate) => candidate.available === false))
    return "Oda oppgir at de viste produktene ikke er tilgjengelige.";
  if (candidates.some((candidate) =>
    basketBatchIdentityStatus(candidate) === "compatible" &&
    basketBatchSelectableCandidate(candidate, need.unit),
  )) return "";
  if (candidates.some((candidate) =>
    basketBatchIdentityStatus(candidate) === "unverified" &&
    basketBatchSelectableCandidate(candidate, need.unit),
  )) return "Treffene må bekreftes enkeltvis som riktig produkt før de kan velges.";
  if (!candidates.some((candidate) => basketBatchSelectableCandidate(candidate, need.unit)))
    return "Pakningsstørrelse, måleenhet eller anbefalt antall kan ikke brukes samlet. Oppdater søket eller hopp over varen.";
  return "";
}
function openBasketBatchReview(targetNeedId = null) {
  clearStaleBasketBatchIdentityConfirmations();
  const root = openDialog("Se over produktforslag");
  $("#modal").dataset.kind = "basket-batch-review";
  const choices = Object.values(state.basketBatchChoices);
  const { start, end } = currentBasketMatchDates();
  const activeChoices = choices.filter((choice) => {
    if (choice.match_job_id !== state.basketBatchReviewJobId || choice.start !== start || choice.end !== end)
      return false;
    const result = state.basketMatchPages[String(choice.need_id)]?.[1];
    const candidate = (result?.candidates || []).find((value) => value.id === choice.candidate_id);
    const status = basketBatchIdentityStatus(candidate);
    return result?.match_job_id === state.basketBatchReviewJobId &&
      basketBatchSelectableCandidate(candidate, choice.unit) &&
      (status === "compatible" || (status === "unverified" && choice.confirm_identity === true));
  });
  const needs = currentBasketMatchNeeds();
  add(
    root,
    el(
      "p",
      "Dette steget lagrer ingenting. Velg høyst ett beregnbart forslag per vare. Uavklarte varer kan stå over. Tidligere lagrede valg beholdes med mindre du velger å erstatte dem.",
      "muted",
    ),
  );
  const list = el("div", null, "basket-batch-needs");
  let targetSection = null;
  needs.forEach((need) => {
    const result = state.basketMatchPages[String(need.id)]?.[1];
    const entry = (state.basket?.needs || []).find(
      (value) => String(value.need?.id) === String(need.id),
    );
    const saved = entry?.selections || [];
    const staged = state.basketBatchChoices[String(need.id)];
    const section = add(
      el("section", null, "basket-batch-need"),
      el("h3", need.name),
      el("p", formatUiNeedQuantity(need) + " å dekke", "muted"),
    );
    section.dataset.needId = String(need.id);
    if (targetNeedId && String(need.id) === String(targetNeedId)) {
      targetSection = section;
    }
    if (saved.length) {
      const current = el("details", null, "basket-batch-existing");
      current.open = Boolean(staged);
      add(current, el("summary", formatUiCount(saved.length) + " lagrede produktvalg beholdes"));
      saved.forEach((selection) => {
        if (selection.product) {
          const chosen = add(el("div", null, "selected-product"), basketProduct(selection.product, selection.packages));
          add(chosen, button("Fjern produktvalg", async () => {
            try {
              const urlRange = typeof range === "function" ? range() : "";
              await api("/basket/selections/" + encodeURIComponent(need.id) + "/" + encodeURIComponent(selection.product.product_id) + urlRange, "DELETE");
              state.cartProposalHandled = false;
              if (typeof refreshView === "function") await refreshView();
              openBasketBatchReview(need.id);
            } catch (error) {
              if (typeof notify === "function") notify(error.message, true);
            }
          }, "quiet small"));
          current.append(chosen);
        }
      });
      add(section, current);
    }
    if (staged) {
      const change = el("p", null, "callout");
      change.textContent = staged.match_job_id !== state.basketBatchReviewJobId ||
        staged.start !== start || staged.end !== end
        ? "Tidligere valgt: " + staged.name + " · " + formatUiCount(staged.packages) + " pakker. Velg det på nytt hvis du fortsatt ønsker det."
        : staged.confirm_identity === true
          ? "Du har bekreftet at dette er riktig produkt for akkurat denne varen. Pakningsforslaget må fortsatt kontrolleres."
        : saved.length
          ? "Valget under vil erstatte " + formatUiCount(saved.length) + " lagrede valg for denne varen."
          : "Dette forslaget legges til i den samlede godkjenningen.";
      add(section, change);
    }
    const reason = basketBatchSkipReason(need, result);
    if (reason)
      add(section, el("p", "Kan stå over: " + reason, "muted"));
    const candidates = !state.basketBatchNeedsRefresh && result?.match_job_id === state.basketBatchReviewJobId
      ? result.candidates || []
      : [];
    const selectable = candidates.filter((candidate) =>
      basketBatchSelectableCandidate(candidate, need.unit),
    );
    const compatible = selectable.filter((candidate) =>
      basketBatchIdentityStatus(candidate) === "compatible",
    );
    const primary = candidates.find((candidate) =>
      staged?.candidate_id === candidate.id && staged?.match_job_id === state.basketBatchReviewJobId &&
        staged?.start === start && staged?.end === end,
    ) || compatible[0] || selectable[0];
    const renderOption = (candidate) => {
      const card = el("article", null, "basket-batch-option");
      add(card, renderBasketMatchCandidate(candidate, need));
      const identityUnverified = basketBatchIdentityStatus(candidate) === "unverified";
      if (identityUnverified)
        add(card, el("p", "Produktidentiteten er ikke bekreftet automatisk.", "muted"));
      if (basketBatchSelectableCandidate(candidate, need.unit)) {
        const selected = staged?.candidate_id === candidate.id &&
          staged?.match_job_id === state.basketBatchReviewJobId &&
          staged?.start === start && staged?.end === end;
        const select = identityUnverified
          ? button(
              selected && staged?.confirm_identity === true
                ? "Bekreftelsen er valgt"
                : "Dette er riktig produkt",
              () => toggleBasketBatchChoice(candidate, need, result.match_job_id, true),
              selected && staged?.confirm_identity === true ? "primary small" : "quiet small",
            )
          : button(
              selected ? "Valgt for godkjenning" : saved.length ? "Erstatt lagrede valg med dette" : "Velg dette for varen",
              () => toggleBasketBatchChoice(candidate, need, result.match_job_id, false),
              selected ? "primary small" : "quiet small",
            );
        select.setAttribute("aria-pressed", String(
          selected && (!identityUnverified || staged?.confirm_identity === true),
        ));
        select.dataset.focusKey = `basket-batch-candidate:${need.id}:${candidate.id}`;
        select.disabled = state.basketBatchSaving || state.basketBatchNeedsRefresh;
        add(card, select);
      }
      return card;
    };
    if (primary) add(section, renderOption(primary));
    const alternatives = candidates.filter((candidate) => candidate !== primary);
    if (alternatives.length) {
      const disclosure = el("details", null, "basket-batch-alternatives");
      add(
        disclosure,
        el(
          "summary",
          (primary ? "Flere treff (" : "Vis uavklarte treff (") +
            formatUiCount(alternatives.length) +
            ")",
        ),
      );
      alternatives.forEach((candidate) => add(disclosure, renderOption(candidate)));
      add(section, disclosure);
    }
    if (staged) {
      const remove = button("Fjern dette forslaget", () => {
        delete state.basketBatchChoices[String(need.id)];
        state.basketBatchNeedsCorrection = false;
        openBasketBatchReview();
      }, "quiet small");
      remove.disabled = state.basketBatchSaving;
      add(section, remove);
    }
    list.append(section);
  });
  add(root, list);
  if (targetSection) {
    if (typeof targetSection.scrollIntoView === "function") {
      targetSection.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    const findFocusable = (element) => {
      if (!element) return null;
      if (typeof element.querySelector === "function") {
        return element.querySelector("button, input, summary");
      }
      if (typeof element.find === "function") {
        return element.find((node) => ["button", "input", "summary"].includes(node.tagName?.toLowerCase()));
      }
      for (const child of element.children || []) {
        if (["button", "input", "summary"].includes(child.tagName?.toLowerCase())) return child;
        const nested = findFocusable(child);
        if (nested) return nested;
      }
      return null;
    };
    const focusable = findFocusable(targetSection);
    if (focusable && typeof focusable.focus === "function") {
      focusable.focus();
    }
  }
  const total = el("p", activeChoices.length
    ? formatUiCount(activeChoices.length) + " forslag er valgt. Kontroller varer som erstatter lagrede valg før du fortsetter."
    : choices.length
      ? "De tidligere valgene må gjennomgås på nytt mot de oppdaterte forslagene."
      : "Ingen forslag er valgt. Du kan lukke vinduet uten å endre lagrede valg.", "muted");
  add(root, total);
  if (state.basketBatchSaveError) {
    const error = el("p", state.basketBatchSaveError, "form-error");
    error.setAttribute("role", "alert");
    add(root, error);
  }
  if (state.basketBatchNeedsCorrection)
    add(root, el("p", "Rett eller fjern valgene før du prøver igjen. Ingen valg ble lagret.", "callout"));
  if (state.basketBatchNeedsRefresh) {
    add(root, el("p", "Treffene er utdaterte. Oppdater dem og gjennomgå forslagene på nytt før du lagrer.", "callout"));
    add(root, button(
      state.basketMatchGlobalRefreshing ? "Oppdaterer produkttreff …" : "Oppdater treff for ny gjennomgang",
      () => {
        $("#modal").close();
        void startBasketMatchJob({ forceRefresh: true });
      },
      "quiet",
    ));
  }
  const apply = button(
    state.basketBatchSaving ? "Bruker produktforslag …" : "Bruk " + formatUiCount(activeChoices.length) + " produktforslag",
    () => void saveBasketBatchChoices(),
    "primary",
  );
  apply.disabled = !activeChoices.length || state.basketBatchSaving || state.basketBatchNeedsRefresh || state.basketBatchNeedsCorrection;
  add(root, apply);
}
function renderBasketBatchReview(main) {
  clearStaleBasketBatchIdentityConfirmations();
  if (!state.oda?.connected || !state.basketBatchReviewJobId) return;
  const eligibility = state.basketBatchNeedsRefresh
    ? { safe: 0, alternatives: 0, identityConfirmation: 0, skipped: currentBasketMatchNeeds().length }
    : currentBasketMatchNeeds().reduce((summary, need) => {
      const result = state.basketMatchPages[String(need.id)]?.[1];
      const candidates = result?.match_job_id === state.basketBatchReviewJobId &&
        !state.basketBatchNeedsRefresh &&
        !state.basketMatchBusy[String(need.id)] &&
        !state.basketMatchErrors[String(need.id)]
        ? (result.candidates || []).filter((candidate) => basketBatchSelectableCandidate(candidate, need.unit))
        : [];
      const compatible = candidates.filter((candidate) => basketBatchIdentityStatus(candidate) === "compatible");
      const unverified = candidates.filter((candidate) => basketBatchIdentityStatus(candidate) === "unverified");
      if (compatible.length) {
        summary.safe += 1;
        summary.alternatives += compatible.length - 1;
      }
      if (unverified.length) summary.identityConfirmation += 1;
      if (!compatible.length && !unverified.length) summary.skipped += 1;
      return summary;
    }, { safe: 0, alternatives: 0, identityConfirmation: 0, skipped: 0 });
  const panel = add(
    el("section", null, "panel basket-batch-review"),
    el("h2", "Samlet Oda-gjennomgang"),
    el(
      "p",
      "Se over forslagene samlet før du lagrer valg. Tidligere valg står urørt fram til du bevisst erstatter dem. Oda-handlekurven endres ikke her.",
      "muted",
    ),
  );
  add(panel, el("p", formatUiCount(eligibility.safe) + " varer har forslag som kan velges direkte · " + formatUiCount(eligibility.skipped) + " varer må stå over eller avklares.", "muted"));
  if (eligibility.identityConfirmation)
    add(panel, el("p", formatUiCount(eligibility.identityConfirmation) + " varer har treff som må bekreftes enkeltvis som riktig produkt.", "muted"));
  if (eligibility.alternatives)
    add(panel, el("p", formatUiCount(eligibility.alternatives) + " ekstra valgbare alternativer finnes under «Flere treff».", "muted"));
  if (state.basketBatchSaveError) {
    const error = el("p", state.basketBatchSaveError, "form-error");
    error.setAttribute("role", "alert");
    add(panel, error);
  }
  if (state.basketBatchNeedsRefresh) {
    add(panel, el("p", "Treffene er utdaterte. Oppdater dem og gjennomgå valgene på nytt.", "muted"));
    add(panel, button(
      state.basketMatchGlobalRefreshing ? "Oppdaterer produkttreff …" : "Oppdater treff for ny gjennomgang",
      () => void startBasketMatchJob({ forceRefresh: true }),
      "primary",
    ));
  } else {
    add(panel, button(
      "Se over produktforslag",
      () => openBasketBatchReview(),
      "primary",
    ));
  }
  if (!eligibility.safe && !eligibility.identityConfirmation) {
    add(panel, el("p", "Ingen varer har forslag som kan velges ennå. Oppdater produkttreffene eller hopp over uavklarte varer.", "muted"));
    if (!state.basketBatchNeedsRefresh)
      add(panel, button(
        state.basketMatchGlobalRefreshing ? "Oppdaterer produkttreff …" : "Oppdater produkttreff",
        () => void startBasketMatchJob({ forceRefresh: true }),
        "quiet small",
      ));
  }
  add(main, panel);
}
async function saveBasketBatchChoices() {
  clearStaleBasketBatchIdentityConfirmations();
  if (
    state.basketBatchSaving ||
    !state.basketBatchReviewJobId ||
    state.basketBatchNeedsRefresh ||
    state.basketBatchNeedsCorrection
  ) return;
  const { start, end } = currentBasketMatchDates();
  const planEpoch = state.basketMatchPlanEpoch;
  const jobId = state.basketBatchReviewJobId;
  const choices = Object.values(state.basketBatchChoices)
    .filter((choice) => choice.match_job_id === jobId && choice.start === start && choice.end === end)
    .filter((choice) => {
      const result = state.basketMatchPages[String(choice.need_id)]?.[1];
      if (result?.match_job_id !== jobId) return false;
      const candidate = (result.candidates || []).find((value) => value.id === choice.candidate_id);
      if (!candidate || !basketBatchSelectableCandidate(candidate, choice.unit)) return false;
      const status = basketBatchIdentityStatus(candidate);
      return status === "compatible" || (status === "unverified" && choice.confirm_identity === true);
    })
    .map(({ need_id, candidate_id, packages, confirm_identity }) => ({
      need_id,
      candidate_id,
      packages,
      confirm_identity: confirm_identity === true,
    }));
  if (!choices.length) return;
  state.basketBatchSaving = true;
  state.basketBatchSaveError = "";
  render();
  openBasketBatchReview();
  try {
    await api("/basket/selections/batch", "PUT", {
      start,
      end,
      job_id: jobId,
      selections: choices,
    });
    if (planEpoch !== state.basketMatchPlanEpoch) {
      notify("Produktvalgene for perioden " + start + "–" + end + " er lagret. Oda-handlekurven er ikke endret.");
      return;
    }
    state.basketBatchChoices = {};
    state.cartProposalHandled = false;
    $("#modal").close();
    notify("De valgte Oda-produktene er lagret for gjennomgang. Handlekurven er ikke endret.");
    await refreshView();
  } catch (error) {
    if (planEpoch === state.basketMatchPlanEpoch) {
      const invalidRow = error.status === 422
        ? /selections\.(\d+)/u.exec(error.detail || "")
        : null;
      const invalidChoice = invalidRow ? choices[Number(invalidRow[1])] : null;
      const invalidName = invalidChoice
        ? state.basketBatchChoices[invalidChoice.need_id]?.name
        : null;
      state.basketBatchSaveError = invalidName
        ? invalidName + ": " + error.message
        : error.message;
      if ([404, 409].includes(error.status)) state.basketBatchNeedsRefresh = true;
      if (error.status === 422) state.basketBatchNeedsCorrection = true;
      render();
      if ($("#modal").open) openBasketBatchReview();
    }
  } finally {
    if (planEpoch === state.basketMatchPlanEpoch) {
      state.basketBatchSaving = false;
      render();
      if ($("#modal").open) openBasketBatchReview();
    }
  }
}
function renderCompactShoppingMatch(line, match) {
  const container = el("div", null, "inline-basket-match compact-basket-match");
  const need = match?.need;
  const needId = need?.id;
  const selections = (match?.selections || []).filter((s) => s.product);

  let badgeText = "";
  let badgeClass = "badge compact-match-badge";
  let buttonLabel = "Se over";
  let buttonAria = "Se over produktforslag for " + line.name;

  if (selections.length > 0) {
    const first = selections[0];
    const p = first.product;
    const packText = first.packages ? " · " + formatUiCount(first.packages) + " pk" : "";
    badgeText = selections.length > 1
      ? formatUiCount(selections.length) + " Oda-valg"
      : (p.name || "Valgt produkt") + packText;
    badgeClass += " match-selected";
    buttonLabel = "Endre";
    buttonAria = "Endre produktvalg for " + line.name;
  } else if (needId) {
    const id = String(needId);
    const staged = state.basketBatchChoices?.[id];
    if (staged?.name) {
      const packText = staged.packages ? " · " + formatUiCount(staged.packages) + " pk" : "";
      badgeText = "Klargjort: " + staged.name + packText;
      badgeClass += " match-staged";
      buttonLabel = "Endre";
      buttonAria = "Endre klargjort produktforslag for " + line.name;
    } else {
      const busy = state.basketMatchBusy?.[id];
      const error = state.basketMatchErrors?.[id];
      const page = state.basketMatchCurrentPage?.[id] || 1;
      const result = state.basketMatchPages?.[id]?.[page] || state.basketMatchPages?.[id]?.[1];
      if (busy) {
        badgeText = "Søker hos Oda …";
        badgeClass += " match-busy";
      } else if (error) {
        badgeText = "Søk feilet";
        badgeClass += " match-error";
      } else if (result?.candidates?.length) {
        const candidates = result.candidates;
        const selectable = typeof basketBatchSelectableCandidate === "function"
          ? candidates.filter((c) => basketBatchSelectableCandidate(c, need?.unit))
          : candidates;
        const compatible = typeof basketBatchIdentityStatus === "function"
          ? selectable.filter((c) => basketBatchIdentityStatus(c) === "compatible")
          : selectable;
        const unverified = typeof basketBatchIdentityStatus === "function"
          ? selectable.filter((c) => basketBatchIdentityStatus(c) === "unverified")
          : [];
        if (compatible.length) {
          const primary = compatible[0];
          const packText = primary.recommended_packages ? " · " + formatUiCount(primary.recommended_packages) + " pk" : "";
          badgeText = "Forslag: " + primary.name + packText;
          badgeClass += " match-candidate";
          buttonLabel = "Se forslag";
          buttonAria = "Se over produktforslag for " + line.name;
        } else if (unverified.length) {
          badgeText = "Må bekreftes";
          badgeClass += " match-unverified";
          buttonLabel = "Se forslag";
          buttonAria = "Se over uavklart produktforslag for " + line.name;
        } else {
          badgeText = "Uavklart treff";
          badgeClass += " match-unresolved";
        }
      } else if (result && result.status === "no_results") {
        badgeText = "Ingen treff";
        badgeClass += " match-unresolved";
      } else {
        badgeText = "Venter på treff";
        badgeClass += " match-pending";
      }
    }
  }

  const badge = el("span", badgeText || "Oda", badgeClass);
  const actionBtn = button(buttonLabel, () => {
    if (typeof openBasketBatchReview === "function") {
      openBasketBatchReview(needId);
    }
  }, "quiet small compact-match-action");
  actionBtn.setAttribute("aria-label", buttonAria);

  add(container, badge, actionBtn);
  return container;
}
function renderSingleShoppingList(main) {
  const trip = state.storeTrip || { lines: [], checked_count: 0, total_count: 0, unquantified: [] };
  const lines = trip.lines || [];
  const progressData = window.KitchenShoppingView.pickupProgress(trip.checked_count, trip.total_count);
  const progress = el("progress");
  progress.max = 100;
  progress.value = progressData.percent;
  progress.setAttribute("aria-label", "Fremdrift for plukking på handleturen");
  const toolbar = add(el("div", null, "trip-toolbar"), add(el("div", null, "trip-progress"), el("strong", progressData.label), progress));
  const remaining = lines.filter(window.KitchenShoppingShare.isRemainingShoppingLine);
  const copy = button("Kopier varer som gjenstår", async () => {
    const text = window.KitchenShoppingShare.formatRemainingShoppingText(
      "Handleliste · " + dateLabel(day(0), { month: "short", day: "numeric" }) + " – " + dateLabel(day(6), { month: "short", day: "numeric", year: "numeric" }),
      lines,
      state.household.shopping_category_order || [],
    );
    try {
      if (typeof navigator.clipboard?.writeText !== "function") throw new Error("clipboard-unavailable");
      await navigator.clipboard.writeText(text);
      notify("Varene som gjenstår, er kopiert.");
    } catch {
      const root = openDialog("Kopier handlelisten");
      const content = el("textarea");
      content.setAttribute("aria-label", "Varer som gjenstår på handlelisten");
      content.readOnly = true;
      content.rows = Math.min(16, Math.max(6, text.split("\n").length));
      content.value = text;
      add(root, el("p", "Merk teksten og kopier den til en annen app.", "muted"), content);
      content.focus();
      content.select();
    }
  }, "quiet small");
  copy.disabled = remaining.length === 0;
  add(toolbar, add(el("div", null, "actions shopping-share-actions"), copy));
  add(main, toolbar);
  renderShoppingConnectionPrompt(main);

  if (state.oda?.connected && lines.length) {
    const status = renderBasketMatchSummary();
    status.classList.add("inline-matching-summary");
    add(main, status);
    renderBasketBatchReview(main);
  }
  if (trip.unquantified?.length) {
    const unquantified = el("details", null, "shopping-info");
    add(unquantified, el("summary", formatUiCount(trip.unquantified.length) + " behov må avklares før de kan vises som mengder"));
    const list = el("ul");
    trip.unquantified.forEach((need) => add(list, el("li", [need.name, need.unit, categoryLabel(need.category)].filter(Boolean).join(" · "))));
    add(unquantified, list);
    add(main, unquantified);
  }
  if (!lines.length) {
    add(main, empty(
      "Ingen varer gjenstår for denne perioden",
      "Planlegg et måltid eller legg til et handlebehov for å fylle på listen.",
      button("Planlegg et måltid", () => navigate("week"), "primary"),
    ));
    return;
  }

  const grouping = add(el("div", null, "shopping-grouping"), el("span", "Sorter etter", "muted"));
  [["category", "Kategori"], ["meal", "Måltid"]].forEach(([key, label]) => {
    const option = button(label, () => {
      state.shoppingGrouping = key;
      render();
    }, state.shoppingGrouping === key ? "primary small" : "quiet small");
    option.setAttribute("aria-pressed", String(state.shoppingGrouping === key));
    grouping.append(option);
  });
  add(main, grouping);

  const mergedLines = window.KitchenShoppingView.mergeActionableLines(lines, state.basket?.needs || []);
  const groups = window.KitchenShoppingView.actionableGroups(
    mergedLines,
    state.household.shopping_category_order || [],
    state.meals,
    state.shoppingGrouping,
    state.recipes,
  );
  for (const [groupName, entries] of groups) {
    const section = add(el("section", null, "panel category-section actionable-shopping-group"), el("h2", categoryLabel(groupName)));
    const list = el("ul", null, "list single-shopping-list");
    entries.forEach(({ line, match }) => {
      const row = el("li", null, line.checked ? "completed actionable-shopping-item" : "actionable-shopping-item");
      const check = el("input");
      check.type = "checkbox";
      check.checked = Boolean(line.checked);
      check.setAttribute("aria-label", (check.checked ? "Fjern avkryssing for " : "Merk som plukket: ") + line.name);
      check.addEventListener("change", async () => {
        check.disabled = true;
        state.shoppingWriteInFlight += 1;
        try {
          await api("/store-trip/items/" + encodeURIComponent(line.id) + range(), "PUT", { checked: check.checked });
          await refreshView();
        } catch (error) {
          check.checked = Boolean(line.checked);
          notify(error.message, true);
        } finally {
          state.shoppingWriteInFlight = Math.max(0, state.shoppingWriteInFlight - 1);
          check.disabled = false;
          queuePendingShoppingRefresh();
        }
      });
      const detail = add(el("div", null, "detail"), el("strong", line.name));
      const amount = formatUiNeedQuantity({
        quantity: line.to_buy_quantity,
        unit: line.unit,
        approximate: line.approximate === true,
      });
      const summary = el("small", amount + " å kjøpe" + (line.category ? " · " + categoryLabel(line.category) : ""));
      summary.setAttribute(
        "aria-label",
        (line.approximate ? "Omtrentlig mengde: " : "Nøyaktig mengde: ") +
          formatUiNeedQuantity({ quantity: line.to_buy_quantity, unit: line.unit, approximate: line.approximate }, true) +
          " å kjøpe",
      );
      add(detail, summary);
      const mealOrigins = window.KitchenShoppingView.mealOriginNames(line.origins, state.meals, state.recipes);
      const extraOrigins = (line.origins || []).filter((origin) => typeof origin === "string" && !origin.startsWith("meal:"));
      const originLabels = [...mealOrigins, ...(extraOrigins.some((origin) => origin.startsWith("item:")) ? ["Hurtigtillegg"] : []), ...(extraOrigins.some((origin) => origin.startsWith("regular:")) ? ["Hverdagsvare"] : [])];
      if (originLabels.length) add(detail, el("small", "Til: " + [...new Set(originLabels)].join(" · "), "muted"));
      const evidence = renderPurchaseEvidence(line);
      if (evidence) add(detail, evidence);
      add(row, check, detail);
      if (state.oda?.connected && match?.need) {
        const matchView = renderCompactShoppingMatch(line, match);
        add(row, matchView);
      }
      list.append(row);
    });
    add(section, list);
    main.append(section);
  }
  const basket = state.basket || { needs: [] };
  const proposalRequest = odaCartProposalRequest(basket);
  if (state.oda?.connected && basket.ready_for_review && proposalRequest.additions.length && !proposalRequest.issue) {
    add(main, add(el("section", null, "panel oda-cart-panel"),
      el("h2", "Oda-handlekurven er klar for gjennomgang"),
      el("p", "Dette åpner en eksplisitt kontroll av varene. Kjøkken gjennomfører ikke betaling eller bestilling.", "muted"),
      button("Se over valgte produkter", () => { state.shoppingPanel = "oda"; render(); }, "primary"),
    ));
  }
}
function renderShopping(main) {
  const single = state.shoppingPanel === "single";
  const inStore = state.shoppingPanel === "in_store";
  const completeTrip = state.shoppingPanel === "complete_trip";
  const isOda = state.shoppingPanel === "oda";
  const isPantry = state.shoppingPanel === "pantry";
  const heading = title(
    "Handleliste",
    single
      ? "Handlelisten"
      : inStore
      ? "Handletur i butikk"
      : completeTrip
        ? "Registrer handletur"
      : isOda
        ? "Oda-produkter"
        : isPantry
          ? "Hjemme"
          : "Handlelisten.",
    single
      ? "Varer som gjenstår etter det dere har hjemme."
      : inStore
      ? "Plukk varene som gjenstår denne uken."
      : completeTrip
        ? "Registrer faktiske kjøp og valgte behovsavslutninger."
      : isOda
        ? "Se over produktvalgene for denne uken."
        : isPantry
          ? "Mengdene du har her, trekkes fra handlebehovet."
          : "Listen samler planlagte måltider, hverdagsvarer og det du legger til.",
    inStore || completeTrip || isOda
      ? []
      : [
          isPantry
            ? button("+ Legg til vare hjemme", () => inventoryForm(), "primary")
            : button("+ Legg til handlebehov", () => itemForm(), "primary"),
        ],
  );
  if (inStore || completeTrip || single) heading.classList.add("shopping-compact-header");
  add(main, heading, weekControls(), shoppingSections());
  if (single) return renderSingleShoppingList(main);
  if (state.shoppingPanel === "in_store") return renderInStore(main);
  if (completeTrip) return renderTripCompletion(main);
  if (state.shoppingPanel === "oda") return renderBasket(main);
  if (state.shoppingPanel === "pantry") return renderInventory(main);
  const columns = el("div", null, "two-columns");
  const left = el("div");
  const planned = add(
    el("section", null, "panel"),
    el("h2", "Handleoversikt"),
    el(
      "p",
      "Nødvendig mengde, det du har hjemme, og det som gjenstår å kjøpe.",
      "muted",
    ),
  );
  const planLines = state.purchasePlan.lines || [];
  if (!planLines.length)
    add(
      planned,
      el("p", "Planlegg et måltid for å se ingrediensene her.", "muted"),
      button("Gå til måltidsplanen", () => navigate("week"), "quiet"),
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
        formatUiNeedQuantity({ quantity: required, unit, approximate: item.approximate === true }) +
          (available != null
            ? " · " + formatUiQuantity(available) + " " + window.KitchenShoppingView.displayUnit(unit, false, available) + " hjemme"
            : "") +
          " · " +
          (window.KitchenShoppingView.isPositiveDecimal(toBuy)
            ? formatUiNeedQuantity({ quantity: toBuy, unit, approximate: item.approximate === true }) + " å kjøpe"
            : "dekket hjemme") +
          " · " +
          categoryLabel(item.category) +
          (item.origins?.length ? " · " + shoppingOrigins(item.origins) : ""),
      );
      summary.setAttribute(
        "aria-label",
        formatUiNeedQuantity({ quantity: required, unit, approximate: item.approximate === true }, true) +
          " nødvendig" +
          (available != null ? "; " + formatUiAccessibleQuantity(available) + " " + window.KitchenShoppingView.displayUnit(unit, true, available) + " hjemme" : "") +
          "; " +
          formatUiNeedQuantity({ quantity: toBuy, unit, approximate: item.approximate === true }, true) +
          " å kjøpe",
      );
      const detail = add(el("div", null, "detail"), el("strong", item.name), summary);
      const evidence = renderPurchaseEvidence(item);
      if (evidence) add(detail, evidence);
      add(list, add(el("li"), detail));
    });
    add(planned, list);
  }
  const extras = add(
    el("section", null, "panel"),
    el("h2", "Administrer hurtigtillegg"),
    el(
      "p",
      "Varer du legger til her, tas med i handleoversikten. Du kan utelate en vare fra planen. Plukkestatus finner du under Handletur i butikk.",
      "muted",
    ),
  );
  if (!state.items.length)
    add(extras, el("p", "Begynner det å bli tomt for noe? Legg det til her.", "muted"));
  const itemList = el("ul", null, "list");
  state.items.forEach((item) => {
    const row = el("li", null, item.completed ? "completed" : "");
    const check = el("input");
    check.type = "checkbox";
    check.checked = item.completed;
    check.ariaLabel = "Utelat " + item.name + " fra planen";
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
      el("span", "Utelat " + item.name + " fra planen"),
    );
    add(
      row,
      exclude,
      add(
        el("div", null, "detail"),
        el("strong", item.name),
        el(
          "small",
          formatUiQuantity(item.quantity) +
            (item.unit ? " " + window.KitchenShoppingView.displayUnit(item.unit, false, item.quantity) : ""),
        ),
      ),
      button(
        "Fjern",
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
    el("h2", "Hverdagsvarer"),
    el(
      "p",
      "Få en påminnelse om varer du kjøper ofte. Legg dem til på handlelisten når du trenger dem.",
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
            el(
              "small",
              formatUiQuantity(item.quantity) +
                (item.unit ? " " + window.KitchenShoppingView.displayUnit(item.unit, false, item.quantity) : "") +
                " · Forslag",
            ),
          ),
          button(
            "Legg til",
            async () => {
              try {
                await mutate(
                  "/regular-items/" + encodeURIComponent(item.id) + "/confirm",
                  "POST",
                  {},
                  "Lagt til på handlelisten.",
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
              "Hver " +
                formatUiCount(item.interval_days) +
                (Number(item.interval_days) === 1 ? " dag" : " dager") +
                (item.last_purchased
                  ? " · Sist kjøpt " + item.last_purchased
                  : ""),
            ),
          ),
          button(
            "Kjøpt i dag",
            async () => {
              try {
                await mutate(
                  "/regular-items/" + encodeURIComponent(item.id) + "/purchase",
                  "POST",
                  { purchased_on: iso(new Date()) },
                  "Kjøpet er registrert.",
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
    button("+ Legg til fast vare", () => itemForm(true), "quiet"),
    button("Gi tilbakemelding", () => feedbackForm(), "quiet"),
  );
  const oda = add(
    el("section", null, "panel"),
    el("h2", "Oda"),
    el(
      "p",
      "Koble til Oda-kontoen din under Oda-fanen. Handlelisten for butikken fungerer også uten Oda.",
      "muted",
    ),
  );
  add(
    oda,
    button("Sett opp Oda i Kjøkken", () => navigate("basket"), "quiet"),
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
      image.replaceWith(el("span", "Bildet er ikke tilgjengelig", "image-fallback")),
    );
    card.append(image);
  }
  const details = el("div", null, "product-details");
  add(
    details,
    el("h4", product.name),
    product.brand ? el("p", product.brand, "muted") : null,
    product.provider ? el("p", "Kilde: " + product.provider, "muted") : null,
    (product.product_id ?? product.id) != null
      ? el("small", "Oda-produkt-ID: " + (product.product_id ?? product.id), "muted")
      : null,
  );
  const pack =
    product.pack_quantity != null
      ? product.pack_quantity +
        (product.pack_unit ? " " + product.pack_unit : "") +
        " per pakke"
      : "Pakningsstørrelse er ikke tilgjengelig";
  add(
    details,
    el(
      "p",
      pack +
        (packages != null
          ? " · " + formatUiCount(packages) + (Number(packages) === 1 ? " pakke" : " pakker")
          : ""),
      "muted",
    ),
  );
  add(
    details,
    el(
      "p",
      product.price == null
        ? "Ukjent pris"
        : product.price + (product.currency ? " " + product.currency : ""),
      product.price == null ? "muted" : "",
    ),
  );
  add(
    details,
    el(
      "p",
      product.available === true
        ? "Tilgjengelig"
        : product.available === false
          ? "Ikke tilgjengelig"
          : "Tilgjengelighet er ukjent",
      "muted",
    ),
  );
  add(
    details,
    el(
      "p",
      "Grunnlag: " +
        ({ verified: "verifisert", unverified: "ikke verifisert", unavailable: "ikke tilgjengelig" }[product.evidence_status] || "status ukjent") +
        (product.observed_at
          ? " · Kontrollert " +
            (Number.isNaN(Date.parse(product.observed_at))
              ? product.observed_at
              : new Date(product.observed_at).toLocaleString("nb-NO"))
          : ""),
      "muted",
    ),
  );
  const reference = httpsUrl(product.url);
  if (reference) {
    const link = el("a", "Vis produktreferanse ↗");
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
    notify("Oda oppga ingen gyldig produkt-ID for dette resultatet.", true);
    return;
  }
  const root = openDialog("Bekreft produkt og pakningsstørrelse");
  const unitText = need.unit ? window.KitchenShoppingView.displayUnit(need.unit, false) : "";
  add(
    root,
    el(
      "p",
      "Oppgi hvor mange " +
        (unitText || need.unit) +
        " det er i én pakke. Kjøkkenet utleder ikke pakningsstørrelsen fra Oda-oppføringen.",
      "callout",
    ),
    el("h3", product.name || "Oda-produkt"),
    product.brand ? el("p", product.brand, "muted") : null,
  );
  const form = el("form");
  const packages = field("Antall pakker", "number", "");
  packages.input.min = "1";
  packages.input.max = "999";
  packages.input.step = "1";
  packages.input.required = true;
  const packQuantity = field(
    "Mengde " + (unitText || need.unit) + " per pakke",
    "number",
    "",
  );
  packQuantity.input.min = "0.000001";
  packQuantity.input.step = "any";
  packQuantity.input.required = true;
  add(form, packages.wrap, packQuantity.wrap);
  formSubmit(form, "Lagre produktvalg", async () => {
    const packageCount = Number(packages.input.value);
    const quantityText = packQuantity.input.value.trim();
    if (!Number.isSafeInteger(packageCount) || packageCount < 1)
      throw new Error("Oppgi et helt antall pakker større enn null.");
    if (
      !/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(quantityText) ||
      !Number.isFinite(Number(quantityText)) ||
      Number(quantityText) <= 0
    )
      throw new Error("Oppgi en mengde per pakke som er større enn null.");
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
    notify("Oda-produktvalget er lagret for gjennomgang. Det er ikke verifisert.");
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
          ? "Produktbildet kunne ikke lastes inn"
          : "Bildet er ikke tilgjengelig",
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
    add(details, el("p", "Pris hos Oda: " + product.price, "product-price"));
  else add(details, el("p", "Pris er ikke tilgjengelig", "muted"));
  if (product.unitPrice || product.unitName)
    add(
      details,
      el(
        "p",
        "Enhetspris: " +
          (product.unitPrice || "ikke oppgitt") +
          (product.unitName ? " per " + product.unitName : ""),
        "muted",
      ),
    );
  if (product.availability?.description)
    add(
      details,
      el("p", "Tilgjengelighet: " + product.availability.description, "muted"),
    );
  else if (product.availability?.isAvailable === true)
    add(details, el("p", "Oda oppgir at varen er tilgjengelig.", "muted"));
  else if (product.availability?.isAvailable === false)
    add(details, el("p", "Oda oppgir at varen ikke er tilgjengelig.", "muted"));
  else add(details, el("p", "Tilgjengelighet er ikke oppgitt", "muted"));
  if (productUrl) {
    const link = el("a", "Vis Oda-oppføring ↗");
    link.href = productUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(details, link);
  }
  if (media.pack_description)
    add(details, el("p", "Pakningsdetaljer fra Oda: " + media.pack_description));
  else
    add(
      details,
      el(
        "p",
        "Pakningsstørrelsen er ukjent. Enhetsprisen hos Oda sier ikke hvor stor pakken er.",
        "muted",
      ),
    );
  if (media.detail) add(details, el("p", media.detail, "muted"));
  if (productUrl && !imageUrls.length) {
    const loadPhoto = button(
      media.loading
        ? "Laster produktdetaljer …"
        : retryWait
          ? "Prøv igjen om " + formatUiCount(Math.ceil(retryWait / 1000)) + " sek"
          : media.requested
            ? "Prøv å hente produktbildet igjen"
            : "Hent produktbilde",
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
        "Velg produkt",
        () => chooseOdaProduct(product, need),
        "primary small",
      ),
    );
  add(
    details,
    el("small", "Forslag til treff · kontroller produkt og pakningsstørrelse", "muted"),
  );
  add(card, details);
  return card;
}
function odaSearchForNeed(need) {
  if (!state.oda?.connected) return null;
  const current = state.odaSearch[need.id] || {};
  const form = el("form", null, "oda-search-form");
  const query = field(
    "Søk etter Oda-produkter",
    "search",
    current.query || need.name,
  );
  query.input.required = true;
  query.input.maxLength = 300;
  query.input.dataset.focusKey = "oda-search-query-" + need.id;
  query.input.addEventListener("input", () => {
    const latest = state.odaSearch[need.id] || {};
    state.odaSearch[need.id] = { ...latest, query: query.input.value };
  });
  const submit = el(
    "button",
    current.loading ? "Søker …" : "Søk hos Oda",
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
    el("h3", "Finn produkter hos Oda"),
    form,
  );
  if (!state.oda?.connected) {
    add(
      root,
      el("p", "Koble Oda-kontoen til Kjøkken før du søker.", "muted"),
    );
  } else if (current.loading) {
    add(root, el("p", "Søker hos Oda …", "muted"));
  } else if (current.error) {
    add(root, el("p", current.error, "form-error"));
  } else if (current.results) {
    const products = current.results.flatMap((result) => result.products || []);
    if (products.length)
      products.forEach((product) =>
        add(root, odaSearchResultCard(product, need)),
      );
    else add(root, el("p", "Ingen produkter ble funnet for dette søket.", "muted"));
    if (current.results.some((result) => result.hasMore))
      add(
        root,
        el(
          "p",
          "Flere treff finnes hos Oda. Kjøkken viser de første treffene her.",
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
      ? "Oda oppgir at varen er tilgjengelig"
      : product.availability?.isAvailable === false ||
          product.available === false
        ? "Oda oppgir at varen ikke er tilgjengelig"
        : "Tilgjengelighet er ikke oppgitt");
  add(
    details,
    el(
      "p",
      product.price == null
        ? "Oda-prisen er ikke oppdatert ennå"
        : "Oppdatert Oda-pris: " +
            product.price +
            (product.currency ? " " + product.currency : ""),
      product.price == null ? "muted" : "product-price",
    ),
    el("p", availability, "muted"),
  );
  if (productUrl) {
    const link = el("a", "Vis dette Oda-produktet ↗");
    link.href = productUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(details, link);
  } else {
    add(details, el("p", "Oda oppga ingen lenke til produktsiden.", "muted"));
  }
  return details;
}
function proposalWarningText(warning) {
  const code = typeof warning === "string" ? warning : warning?.code;
  if (code === "mapping_unverified")
    return "Oda oppdaterte de valgte produktene, men samsvaret med hvert handlebehov er ikke kontrollert uavhengig. Se over produkt- og mengdedetaljene ovenfor.";
  if (code === "payment_at_oda")
    return "Kontroller og fullfør bestillingen og betalingen hos Oda.";
  return typeof warning === "string"
    ? warning.replaceAll("_", " ")
    : warning?.message || warning?.detail || "Oda sendte en advarsel.";
}
function selectedMappingDetails(productId) {
  const details = [];
  (state.basket?.needs || []).forEach((entry) => {
    const need = entry.need || {};
    (entry.selections || []).forEach((selection) => {
      if (String(selection.product?.product_id) === String(productId))
        details.push(
          "Du oppga " +
            selection.packages +
            " packages for " +
            (need.name || "dette behovet") +
            " · " +
            selection.product.pack_quantity +
            " " +
            selection.product.pack_unit +
            " per pakke",
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
    : "Forslaget utløper " + new Date(timestamp).toLocaleString("nb-NO") + ".";
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
  const root = openDialog("Status for oppdatering av Oda-handlekurven");
  const messages = {
    success:
      "Oda godtok de gjennomgåtte varene. Dette endret bare Oda-handlekurven. Ingen bestilling eller betaling ble gjennomført.",
    stale:
      "Oda-handlekurven ble endret etter gjennomgangen. Kjøkken la ikke til varene. Oppdater og se over på nytt før du velger hva som skal legges til.",
    uncertain:
      "Kjøkken kan ikke bekrefte om Oda la til varene. Ikke bekreft på nytt. Kontroller Oda-handlekurven først.",
    attempted:
      "Oda meldte om et forsøk på oppdatering, men Kjøkken kan ikke bekrefte den endelige handlekurven. Ikke bekreft på nytt. Kontroller Oda-handlekurven først.",
  };
  add(root, el("p", "Status: " + ({ success: "fullført", stale: "utdatert", uncertain: "ukjent", attempted: "forsøkt" }[known]), "eyebrow"));
  add(
    root,
    el(
      "p",
      detail || messages[known],
      known === "success" ? "callout" : "callout offline",
    ),
  );
  add(root, el("p", messages[known], "muted"));
  const cartLink = el("a", "Se over Oda-handlekurven ↗");
  cartLink.href = "https://oda.com/no/cart/";
  cartLink.target = "_blank";
  cartLink.rel = "noopener noreferrer";
  add(root, cartLink);
  const close = button("Lukk", () => $("#modal").close(), "quiet");
  add(root, add(el("div", null, "actions"), close));
  return root;
}
function renderOdaProposal(proposal, request) {
  const root = openDialog("Se over foreslåtte varer til Oda-handlekurven");
  add(
    root,
    el(
      "p",
      "Disse pakkene legges til i Oda-handlekurven. Se over hele handlekurven og betal hos Oda. Kjøkken legger ikke inn bestillinger eller håndterer betaling.",
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
        "Pakker lagt til: " +
          addition.quantity +
          " · Antall i Oda-handlekurven: " +
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
      el("strong", "Se over disse merknadene"),
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
      "Bare varene som er oppført, foreslås lagt til. Andre varer i Oda-handlekurven forblir uendret.",
      "muted",
    ),
  );
  const expiry = Date.parse(proposal.expires_at || "");
  const expired = Number.isFinite(expiry) && expiry <= Date.now();
  const confirm = button(
    expired ? "Forslaget er utløpt" : "Legg disse pakkene i Oda-handlekurven",
    async () => confirmOdaCartProposal(proposal, request),
    "primary",
  );
  confirm.disabled =
    proposal.status !== "pending" ||
    !proposal.proposal_id ||
    !proposal.additions?.length ||
    expired;
  const close = button("Avbryt", () => $("#modal").close(), "quiet");
  add(root, add(el("div", null, "actions"), close, confirm));
  if (confirm.disabled)
    add(
      root,
      el(
        "p",
        "Forslaget kan ikke bekreftes. Be om en ny gjennomgang før du fortsetter.",
        "form-error",
      ),
    );
  else
    add(
      root,
      el(
        "p",
        "Bekreftelse legger bare til pakkeantallene ovenfor. Ingen bestilling eller betaling blir gjennomført.",
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
    el("h2", "Bekrefter varer i Oda-handlekurven"),
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
    trigger.textContent = "Kontrollerer Oda-handlekurven …";
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
      trigger.textContent = "Se over varer til Oda-handlekurven";
    }
  }
}
function renderBasketMatchSummary() {
  const needs = currentBasketMatchNeeds();
  const busyCount = needs.filter((need) => state.basketMatchBusy[String(need.id)]).length;
  const foundCount = needs.filter((need) => {
    const page = state.basketMatchCurrentPage[String(need.id)] || 1;
    return (state.basketMatchPages[String(need.id)]?.[page]?.candidates || []).length > 0;
  }).length;
  const summary = busyCount
    ? "Søker etter produkter for " + formatUiCount(busyCount) + " av " + formatUiCount(needs.length) + " varer …"
    : formatUiCount(foundCount) + " av " + formatUiCount(needs.length) + " varer har produktforslag.";
  const panel = add(
    el("section", null, "panel basket-match-summary"),
    el("h2", "Produktforslag"),
    el("p", summary, "muted"),
  );
  if (state.basketMatchGlobalError) {
    const error = el("p", state.basketMatchGlobalError, "form-error");
    error.setAttribute("role", "alert");
    add(panel, error);
  }
  if (state.basketMatchSkippedNeedCount)
    add(panel, el("p", formatUiCount(state.basketMatchSkippedNeedCount) + " flere varer ble utelatt på grunn av søkegrensen.", "muted"));
  const refresh = button(
    state.basketMatchGlobalRefreshing ? "Oppdaterer treff …" : "Oppdater alle produkttreff",
    () => void startBasketMatchJob({ forceRefresh: true }),
    "quiet small",
  );
  refresh.disabled =
    state.basketMatchGlobalRefreshing || busyCount > 0 || !state.oda?.connected;
  add(panel, refresh);
  return panel;
}
function showBasketMatchPage(needId, page) {
  state.basketMatchCurrentPage[String(needId)] = page;
  render();
}
function renderBasketMatchNeed(need) {
  const id = String(need.id);
  const pages = state.basketMatchPages[id] || {};
  const currentPage = state.basketMatchCurrentPage[id] || 1;
  const result = pages[currentPage] || pages[1];
  const busy = state.basketMatchBusy[id];
  const error = state.basketMatchErrors[id];
  const panel = add(
    el("section", null, "basket-match-need"),
    el("h3", "Foreslåtte produkter"),
  );
  const observedAt = state.basketMatchObservedAt[id]?.[result?.page || currentPage];
  if (observedAt) {
    add(panel, el("p", "Tilgjengelighet sist observert: " + dateLabel(new Date(observedAt), {
      year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    }) + ". Tilgjengeligheten kan endre seg. Oppdater treffene før du går gjennom og godkjenner produktvalg.", "muted"));
  }

  if (busy) {
    add(panel, el("p", busy.page > currentPage ? "Laster side " + formatUiCount(busy.page) + " …" : "Søker hos Oda …", "muted"));
  } else if (error) {
    const errorText = el("p", error, "form-error");
    errorText.setAttribute("role", "alert");
    add(panel, errorText);
  } else if (result) {
    add(panel, el("p", window.KitchenBasketMatchView.statusLabel(result.status), "muted"));
    if (result.detail)
      add(
        panel,
        el(
          "p",
          result.detail === "More provider results are available on the next page."
            ? "Flere treff fra leverandøren er tilgjengelige på neste side."
            : result.detail,
          "muted",
        ),
      );
  } else {
    add(panel, el("p", "Venter på automatisk produktsøk …", "muted"));
  }

  if (result?.candidates?.length) {
    add(panel, renderBasketMatchCandidate(result.candidates[0], need));
    if (result.candidates.length > 1) {
      const alternatives = el("details", null, "basket-match-alternatives");
      add(alternatives, el("summary", "Flere treff (" + formatUiCount(result.candidates.length - 1) + ")"));
      const list = el("div", null, "basket-match-candidates");
      result.candidates.slice(1).forEach((candidate) =>
        list.append(renderBasketMatchCandidate(candidate, need)),
      );
      add(alternatives, list);
      add(panel, alternatives);
    }
  }
  const actions = el("div", null, "actions basket-match-actions");
  if (error || ["search_failed", "search_unavailable"].includes(result?.status)) {
    const retry = button(
      busy ? "Søker …" : "Prøv varetreffet på nytt",
      () => void startBasketMatchJob({ needIds: [id], forceRefresh: true, page: 1 }),
      "quiet small",
    );
    retry.disabled = Boolean(busy) || !state.oda?.connected;
    add(actions, retry);
  }
  if (currentPage > 1 && pages[currentPage - 1])
    add(actions, button("Forrige treff", () => showBasketMatchPage(id, currentPage - 1), "quiet small"));
  if (result?.has_more && currentPage < 50) {
    const nextPage = currentPage + 1;
    const cached = pages[nextPage];
    const more = button(
      busy?.page === nextPage ? "Laster flere …" : cached ? "Neste treff" : "Flere treff",
      () => {
        if (cached) showBasketMatchPage(id, nextPage);
        else void startBasketMatchJob({ needIds: [id], page: nextPage });
      },
      "quiet small",
    );
    more.disabled = Boolean(busy);
    add(actions, more);
  }
  add(panel, actions);
  return panel;
}
function renderBasketMatchCandidate(candidate, need) {
  const card = el("article", null, "basket-match-candidate");
  const media = candidate.media || {};
  const imageUrl = safeImageUrl(media.image_url);
  if (imageUrl) {
    card.classList.add("has-image");
    add(card, recipeImage({ title: candidate.name, image_url: imageUrl }, "product-image"));
  }
  const summary = el("div", null, "basket-match-candidate-main");
  const details = add(el("div", null, "product-details"), el("strong", candidate.name));
  if (candidate.brand) add(details, el("small", candidate.brand, "muted"));
  if (candidate.recommended_packages != null && candidate.recommended_fulfillment_quantity != null) {
    add(
      details,
      el(
        "small",
        "Foreslått " + formatUiCount(candidate.recommended_packages) + " pakker · " +
          window.KitchenShoppingView.formatQuantityForDisplay(String(candidate.recommended_fulfillment_quantity)) +
          " " + window.KitchenShoppingView.displayUnit(need.unit, false, candidate.recommended_fulfillment_quantity) + " totalt",
        "muted",
      ),
    );
  } else {
    const packUnit = candidate.pack_unit
      ? window.KitchenShoppingView.displayUnit(candidate.pack_unit, false, candidate.pack_quantity)
      : "";
    const pack = candidate.pack_quantity != null && candidate.pack_unit
      ? "Pakke " + formatUiQuantity(candidate.pack_quantity) + (packUnit ? " " + packUnit : "") + " · antall er ikke beregnet"
      : "Pakningsstørrelsen er uklar · kontroller før du velger";
    add(details, el("small", pack, "muted"));
  }
  if (candidate.price) add(details, el("small", "Pris hos Oda: " + candidate.price, "muted"));
  add(summary, details);
  const productUrl = officialOdaProductUrl(candidate.product_url);
  const more = el("details", null, "basket-match-candidate-details");
  add(more, el("summary", "Detaljer"));
  if (candidate.description) add(more, el("p", candidate.description, "muted"));
  if (candidate.pack_description)
    add(more, el("p", "Pack details from Oda: " + candidate.pack_description + " · unverified", "muted"));
  else if (media.pack_description)
    add(more, el("p", "Pack details from Oda: " + media.pack_description + " · unverified", "muted"));
  if (candidate.available === true) add(more, el("p", "Oda oppgir at varen er tilgjengelig.", "muted"));
  else if (candidate.available === false) add(more, el("p", "Oda oppgir at varen ikke er tilgjengelig.", "muted"));
  else add(more, el("p", "Tilgjengeligheten er ikke bekreftet i dette søket.", "muted"));
  if (media.detail) add(more, el("p", media.detail, "muted"));
  if (productUrl) {
    const link = el("a", "Se Oda-produktet ↗");
    link.href = productUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    add(more, link);
  }
  add(card, summary, more);
  return card;
}
function chooseMatchedCandidate(candidate, need) {
  if (candidate.provider !== "oda" || !candidate.product_id) {
    notify("Dette resultatet har ingen gyldig Oda-produktreferanse.", true);
    return;
  }
  const recommendation = window.KitchenBasketMatchView.selectionRecommendation(
    candidate,
    need.unit,
  );
  const recommendedPackages = recommendation?.packages;
  const recommendedFulfillment = recommendation?.fulfillment;
  const hasRecommendation = Boolean(recommendation);
  const root = openDialog("Bekreft produkt og pakningsstørrelse");
  add(
    root,
    el("p", "Dette lagrer produktvalget bare for handlekurvgjennomgangen. Det legger ingenting i Oda-handlekurven. Pakningsdetaljene fra Oda er ikke verifisert; kontroller dem før du lagrer.", "callout"),
    el("h3", candidate.name),
    candidate.brand ? el("p", candidate.brand, "muted") : null,
    candidate.pack_description ? el("p", "Oda pack description: " + candidate.pack_description, "muted") : null,
    hasRecommendation
      ? el("p", "Suggested count: " + recommendedPackages + " packs for " + recommendedFulfillment + " " + need.unit + ". Review or change the values below.", "muted")
      : el("p", "Det finnes ikke et pålitelig antall per pakke. Oppgi mengden per pakke i " + need.unit + " og velg antall pakker.", "muted"),
  );
  const form = el("form");
  const packages = field("Antall pakker", "number", hasRecommendation ? recommendedPackages : "");
  packages.input.min = "1";
  packages.input.max = "999";
  packages.input.step = "1";
  packages.input.required = true;
  const amount = field("Mengde per pakke (" + need.unit + ")", "number", recommendation?.packQuantity || "");
  amount.input.min = "0.000001";
  amount.input.step = "any";
  amount.input.required = true;
  add(form, packages.wrap, amount.wrap);
  formSubmit(form, "Lagre produktvalg", async () => {
    const packageCount = Number(packages.input.value);
    if (!Number.isSafeInteger(packageCount) || packageCount < 1)
      throw new Error("Oppgi et helt antall pakker større enn null.");
    const quantityText = amount.input.value.trim();
    if (!/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(quantityText) || !Number.isFinite(Number(quantityText)) || Number(quantityText) <= 0)
      throw new Error("Oppgi en mengde per pakke som er større enn null.");
    await api("/basket/selections", "PUT", {
      start: iso(day(0)),
      end: iso(day(6)),
      selection: {
        need_id: need.id,
        product: {
          provider: "oda",
          product_id: String(candidate.product_id),
          name: candidate.name,
          brand: candidate.brand || null,
          url: officialOdaProductUrl(candidate.product_url) || null,
          image_url: safeImageUrl(candidate.media?.image_url) || null,
          pack_quantity: quantityText,
          pack_unit: need.unit,
          price: providerDecimalPrice(candidate.price),
          currency: "NOK",
          available: typeof candidate.available === "boolean" ? candidate.available : null,
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
    notify("Oda-produktvalget er lagret for gjennomgang, men det er ikke verifisert.");
  });
  add(root, form);
}
async function connectOdaAccount() {
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
      throw new Error("Kjøkken mottok ikke en gyldig autorisasjonslenke fra Oda.");
    window.location.assign(authorization.href);
  } catch (error) {
    notify(error.message, true);
  }
}
async function disconnectOdaAccount() {
  try {
    await api("/oda/connection", "DELETE");
    resetBasketMatching();
    state.basketMatchConnected = false;
    await refreshView();
    notify("Oda er koblet fra Kjøkken.");
  } catch (error) {
    notify(error.message, true);
  }
}
async function disconnectConnector(providerId, providerName) {
  try {
    await api(`/integrations/${providerId}/connection`, "DELETE");
    await loadConnectionStatus();
    await refreshView();
    notify(`${providerName || providerId} er koblet fra.`);
  } catch (error) {
    notify(error.message, true);
  }
}
async function disconnectConnectorAccount(providerId, accountId, accountName) {
  try {
    await api(`/integrations/${providerId}/accounts/${encodeURIComponent(accountId)}`, "DELETE");
    await loadConnectionStatus();
    await refreshView();
    notify(`${accountName || accountId} er koblet fra.`);
  } catch (error) {
    notify(error.message, true);
  }
}
async function refreshConnectorAccount(integration, account) {
  try {
    await api(
      `/integrations/${integration.id}/refresh?account_id=${encodeURIComponent(account.account_id)}`,
      "POST",
    );
    await loadConnectionStatus();
    await refreshView();
    const label = window.KitchenSettingsView?.accountDisplayName
      ? window.KitchenSettingsView.accountDisplayName(account)
      : (account.account_label || account.account_id);
    notify(`${integration.name || integration.id} (${label}) er fornyet.`);
  } catch (error) {
    notify(error.message, true);
    openConnectorModal(integration, {
      accountId: account.account_id,
      accountLabel: account.account_label || "",
    });
  }
}
function openConnectorModal(integration, options = {}) {
  const modalOptions = options && typeof options === "object" ? options : {};
  const dialogTitle = modalOptions.addAccount
    ? `Koble til en konto til (${integration.name || integration.id})`
    : modalOptions.accountId
      ? `Oppdater ${modalOptions.accountLabel || modalOptions.accountId} (${integration.name || integration.id})`
      : `Koble til ${integration.name || integration.id}`;
  const root = openDialog(dialogTitle);
  const container = el("div", null, "stack connector-modal-content");
  const accountLabelField = field(
    "Kontoetikett (valgfritt, f.eks. Malo)",
    "text",
    modalOptions.accountLabel || "",
    "F.eks. Malo eller felles konto",
  );
  function accountMetaPayload() {
    const meta = {};
    const label = accountLabelField.input.value.trim();
    if (label) meta.account_label = label;
    if (modalOptions.accountId) meta.account_id = modalOptions.accountId;
    if (modalOptions.addAccount) meta.add_account = true;
    return meta;
  }

  if (integration.id === "rema1000" || integration.id === "coop") {
    const introText = integration.id === "rema1000"
      ? "Logg inn med din Rema 1000 Æ-konto for å hente kvitteringer og priskutt automatisk."
      : "Logg inn med din Coop-konto for automatisk kvitterings- og kjøpshistorikk.";
    add(container, el("p", introText, "muted"), accountLabelField.wrap);

    const flow = el("div", null, "connector-guided-flow stack");

    // Step 1: Open login
    const step1 = el("div", null, "connector-step connector-step-1 stack");
    add(step1, el("h3", "Trinn 1: Åpne innlogging"));
    add(
      step1,
      el(
        "p",
        "Trykk på knappen under for å åpne innloggingen i en ny fane. Logg inn som vanlig hos leverandøren.",
        "muted",
      ),
    );
    const step1Error = el("p", "", "form-error");
    step1Error.setAttribute("role", "alert");
    const openLoginBtn = button("Åpne innlogging", async () => {
      step1Error.textContent = "";
      openLoginBtn.disabled = true;
      try {
        const startMeta = accountMetaPayload();
        const data = Object.keys(startMeta).length
          ? await api(`/integrations/${integration.id}/pkce/start`, "POST", startMeta)
          : await api(`/integrations/${integration.id}/pkce/start`, "POST");
        if (!data || !data.authorize_url) {
          throw new Error("Mottok ingen innloggingslenke fra tjeneren.");
        }
        window.open(data.authorize_url, "_blank", "noopener,noreferrer");
        step1Notice.textContent =
          "Innloggingssiden er åpnet i en ny fane. Fullfør innloggingen der, og gå deretter til trinn 2.";
        step1Notice.hidden = false;
        step2.hidden = false;
        step2.style.display = "";
        pasteField.input.focus();
      } catch (err) {
        step1Error.textContent = err.message;
      } finally {
        openLoginBtn.disabled = false;
      }
    }, "primary");
    const step1Notice = el("p", "", "callout info");
    step1Notice.hidden = true;
    add(step1, step1Error, add(el("div", null, "actions"), openLoginBtn), step1Notice);
    add(flow, step1);

    // Step 2: Complete connection
    const step2 = el("form", null, "connector-step connector-step-2 stack");
    step2.autocomplete = "off";
    step2.hidden = true;
    step2.style.display = "none";
    add(step2, el("h3", "Trinn 2: Fullfør tilkobling"));
    const step2Instruction = integration.id === "coop"
      ? "Etter innlogging blir du sendt videre til no.coop.members://auth/callback?... (eller tilbake hit). Kopier hele denne omdirigerings-URL-en (eller autorisasjonskoden) og lim den inn her."
      : "Kopier hele nettadressen (URL) fra adressefeltet eller autorisasjonskoden etter fullført innlogging, og lim den inn her.";
    add(
      step2,
      el(
        "p",
        step2Instruction,
        "muted",
      ),
    );
    const pasteField = field(
      "Lim inn nettadressen eller koden etter innlogging",
      "text",
      "",
      "Lim inn hele nettadressen eller koden fra adressefeltet etter innlogging",
    );
    add(step2, pasteField.wrap);

    formSubmit(step2, "Fullfør tilkobling", async () => {
      const val = pasteField.input.value.trim();
      if (!val) {
        throw new Error("Vennligst lim inn nettadressen eller koden etter innlogging.");
      }
      const basePayload = (typeof window.KitchenSettingsView?.parsePkceCompletionPayload === "function")
        ? window.KitchenSettingsView.parsePkceCompletionPayload(val)
        : (/^https?:\/\//i.test(val) ? { redirect_url: val } : { code: val });
      const payload = { ...basePayload, ...accountMetaPayload() };
      await api(`/integrations/${integration.id}/pkce/complete`, "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(flow, step2);
    add(container, flow);

    // Manual fallback <details>
    const details = el("details", null, "connector-manual-details");
    const summary = el("summary", "Avansert: lim inn sesjonstoken manuelt");
    add(details, summary);

    const manualForm = el("form", null, "stack");
    manualForm.autocomplete = "off";
    add(
      manualForm,
      el(
        "p",
        "Hvis du allerede har hentet et sesjonstoken, kan du lime det inn direkte her. Hemmeligheten krypteres og vises aldri.",
        "muted",
      ),
    );
    const manualTokenField = field(
      "Sesjonstoken (token)",
      "password",
      "",
      "Lim inn gyldig sesjonstoken",
    );
    const manualExpiryField = field("Utløpsdato (valgfritt)", "date", "");
    add(manualForm, manualTokenField.wrap, manualExpiryField.wrap);

    formSubmit(manualForm, "Lagre og koble til", async () => {
      const tokenVal = manualTokenField.input.value.trim();
      if (!tokenVal) {
        throw new Error("Vennligst oppgi et gyldig sesjonstoken.");
      }
      const payload = {
        auth_mode: "session_token",
        session_token: tokenVal,
        ...accountMetaPayload(),
      };
      if (manualExpiryField.input.value) {
        payload.expires_at = new Date(manualExpiryField.input.value + "T23:59:59Z").toISOString();
      }
      await api(`/integrations/${integration.id}/connect`, "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(details, manualForm);
    add(container, details);

  } else if (integration.id === "cookidoo") {
    add(
      container,
      el(
        "p",
        "Koble til Cookidoo / Thermomix TM6 for oppskriftsimport og ukeplansynkronisering.",
        "muted",
      ),
    );
    add(
      container,
      el(
        "p",
        "Passordet lagres aldri – kun det resulterende OAuth-tokenet lagres trygt kryptert.",
        "callout connector-security-note",
      ),
      accountLabelField.wrap,
    );

    const loginForm = el("form", null, "stack");
    loginForm.autocomplete = "off";

    const emailField = field("E-post", "email", "", "E-postadressen til din Cookidoo-konto");
    const passwordField = field("Passord", "password", "", "Passordet til din Cookidoo-konto");
    const countryField = field("Land", "text", "no", "Landskode (standard: 'no')");
    add(loginForm, emailField.wrap, passwordField.wrap, countryField.wrap);

    formSubmit(loginForm, "Koble til Cookidoo", async () => {
      const email = emailField.input.value.trim();
      const password = passwordField.input.value.trim();
      const country = countryField.input.value.trim() || "no";
      if (!email || !password) {
        throw new Error("Vennligst oppgi både e-post og passord for Cookidoo.");
      }
      const basePayload = (typeof window.KitchenSettingsView?.buildCookidooLoginPayload === "function")
        ? window.KitchenSettingsView.buildCookidooLoginPayload(email, password, country)
        : { email, password, country };
      const payload = { ...basePayload, ...accountMetaPayload() };
      await api("/integrations/cookidoo/login", "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(container, loginForm);

    // Manual fallback <details>
    const details = el("details", null, "connector-manual-details");
    const summary = el("summary", "Avansert: lim inn informasjonskapsel manuelt");
    add(details, summary);

    const manualForm = el("form", null, "stack");
    manualForm.autocomplete = "off";
    add(
      manualForm,
      el(
        "p",
        "Hvis du foretrekker å oppgi en sesjonsinformasjonskapsel (cookie) fra Cookidoo, kan du lime den inn her. Hemmeligheten krypteres og vises aldri.",
        "muted",
      ),
    );
    const cookieField = field(
      "Informasjonskapsel (cookie)",
      "password",
      "",
      "Lim inn Cookidoo-informasjonskapsel",
    );
    const manualExpiryField = field("Utløpsdato (valgfritt)", "date", "");
    add(manualForm, cookieField.wrap, manualExpiryField.wrap);

    formSubmit(manualForm, "Lagre og koble til", async () => {
      const cookieVal = cookieField.input.value.trim();
      if (!cookieVal) {
        throw new Error("Vennligst oppgi informasjonskapsel for Cookidoo.");
      }
      const payload = {
        auth_mode: "session_cookie",
        session_cookie: cookieVal,
        ...accountMetaPayload(),
      };
      if (manualExpiryField.input.value) {
        payload.expires_at = new Date(manualExpiryField.input.value + "T23:59:59Z").toISOString();
      }
      await api(`/integrations/${integration.id}/connect`, "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(details, manualForm);
    add(container, details);

  } else if (integration.id === "trumf") {
    add(
      container,
      el(
        "p",
        "Koble til Trumf med sesjonsinformasjonskapsel for automatisk synkronisering og fornyelse av kvitteringer og kjøpshistorikk.",
        "muted",
      ),
      accountLabelField.wrap,
    );

    const primaryStep = el("div", null, "connector-step connector-trumf-cookie stack");
    add(primaryStep, el("h3", "Sesjonsinformasjonskapsel"));
    add(
      primaryStep,
      el(
        "p",
        "Åpne Trumf i en ny fane og logg inn med BankID eller SMS. Kopier deretter enten hele Cookie-headeren fra Nettverk-fanen, eller fyll inn begge delene (__Secure-authjs.session-token.0 og __Secure-authjs.session-token.1) fra Applikasjon → Informasjonskapsler slik at automatisk fornyelse via /api/auth/session fungerer.",
        "muted",
      ),
    );
    const openBtn = button("Åpne Trumf-innlogging", () => {
      window.open("https://www.trumf.no/", "_blank", "noopener,noreferrer");
    }, "quiet");
    add(primaryStep, add(el("div", null, "actions"), openBtn));

    const form = el("form", null, "stack");
    form.autocomplete = "off";
    const cookieField = field(
      "Informasjonskapsel (cookie)",
      "password",
      "",
      "Lim inn full Cookie-header (inkludert alle __Secure-authjs.session-token-deler)",
    );
    const token0Field = field(
      "__Secure-authjs.session-token.0 (valgfritt)",
      "password",
      "",
      "Lim inn del 0 fra DevTools → Applikasjon → Cookies",
    );
    const token1Field = field(
      "__Secure-authjs.session-token.1 (valgfritt)",
      "password",
      "",
      "Lim inn del 1 fra DevTools → Applikasjon → Cookies",
    );
    const expiryField = field("Utløpsdato (valgfritt)", "date", "");
    add(form, cookieField.wrap, token0Field.wrap, token1Field.wrap, expiryField.wrap);

    formSubmit(form, "Lagre og koble til", async () => {
      const cookieVal = cookieField.input.value.trim();
      const token0Val = token0Field.input.value.trim();
      const token1Val = token1Field.input.value.trim();
      let basePayload = null;
      if (typeof window.KitchenSettingsView?.buildTrumfConnectPayload === "function") {
        basePayload = window.KitchenSettingsView.buildTrumfConnectPayload(
          cookieVal,
          expiryField.input.value,
          token0Val,
          token1Val,
        );
      } else {
        const parts = [];
        if (cookieVal) parts.push(cookieVal);
        if (token0Val) parts.push(`__Secure-authjs.session-token.0=${token0Val}`);
        if (token1Val) parts.push(`__Secure-authjs.session-token.1=${token1Val}`);
        const combined = parts.join("; ").trim();
        if (combined) {
          basePayload = {
            auth_mode: "session_cookie",
            session_cookie: combined,
          };
        }
      }
      if (!basePayload) {
        throw new Error("Vennligst oppgi informasjonskapsel for Trumf.");
      }
      const payload = { ...basePayload, ...accountMetaPayload() };
      if (expiryField.input.value && !payload.expires_at) {
        payload.expires_at = new Date(expiryField.input.value + "T23:59:59Z").toISOString();
      }
      await api(`/integrations/${integration.id}/connect`, "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(primaryStep, form);
    add(container, primaryStep);

    const supportsOtp =
      !Array.isArray(integration.supported_auth_modes) ||
      integration.supported_auth_modes.includes("interactive_otp");
    if (supportsOtp) {
      const otpDetails = el("details", null, "connector-manual-details");
      add(otpDetails, el("summary", "Bekreft med SMS-kode"));

      const otpBody = el("div", null, "stack");
      add(
        otpBody,
        el(
          "p",
          "Alternativt kan du logge inn med telefonnummer og engangskode på SMS hvis Trumf støtter det.",
          "muted",
        ),
      );
      let activeOtpSessionId = null;
      const phoneForm = el("form", null, "stack connector-trumf-otp-start");
      phoneForm.autocomplete = "off";
      const phoneField = field("Telefonnummer", "tel", "", "F.eks. +4790000000");
      add(phoneForm, phoneField.wrap);

      const verifyForm = el("form", null, "stack connector-trumf-otp-verify");
      verifyForm.autocomplete = "off";
      verifyForm.hidden = true;
      verifyForm.style.display = "none";
      const otpField = field("Engangskode fra SMS", "text", "", "6-sifret kode");
      const otpNotice = el("p", "", "callout info");
      otpNotice.hidden = true;
      add(verifyForm, otpNotice, otpField.wrap);

      formSubmit(phoneForm, "Send SMS-kode", async () => {
        const phone = phoneField.input.value.trim();
        if (!phone) {
          throw new Error("Vennligst oppgi telefonnummer for Trumf.");
        }
        const res = await api("/integrations/trumf/login/start", "POST", {
          phone,
          ...accountMetaPayload(),
        });
        if (!res?.session_id) {
          throw new Error("Mottok ingen sesjons-ID fra Trumf.");
        }
        activeOtpSessionId = res.session_id;
        otpNotice.textContent = res.detail || "Engangskode er sendt på SMS til ditt telefonnummer.";
        otpNotice.hidden = false;
        verifyForm.hidden = false;
        verifyForm.style.display = "";
        otpField.input.focus();
      });

      formSubmit(verifyForm, "Bekreft med SMS-kode", async () => {
        const otp = otpField.input.value.trim();
        if (!activeOtpSessionId || !otp) {
          throw new Error("Vennligst oppgi engangskoden fra SMS.");
        }
        const basePayload =
          typeof window.KitchenSettingsView?.buildTrumfVerifyPayload === "function"
            ? window.KitchenSettingsView.buildTrumfVerifyPayload(activeOtpSessionId, otp)
            : { session_id: activeOtpSessionId, otp };
        if (!basePayload) {
          throw new Error("Vennligst oppgi engangskoden fra SMS.");
        }
        await api("/integrations/trumf/login/verify", "POST", {
          ...basePayload,
          ...accountMetaPayload(),
        });
        const modal = $("#modal");
        if (modal && modal.open) modal.close();
        await loadConnectionStatus();
        await refreshView();
        notify(`${integration.name || integration.id} er tilkoblet.`);
      });

      add(otpBody, phoneForm, verifyForm);
      add(otpDetails, otpBody);
      add(container, otpDetails);
    }

  } else {
    add(container, accountLabelField.wrap);
    const form = el("form", null, "stack");
    form.autocomplete = "off";
    const tokenField = field("Sesjonstoken (token)", "password", "");
    const expiryField = field("Utløpsdato (valgfritt)", "date", "");
    add(form, tokenField.wrap, expiryField.wrap);
    formSubmit(form, "Lagre og koble til", async () => {
      const val = tokenField.input.value.trim();
      if (!val) throw new Error("Vennligst oppgi sesjonstoken.");
      const payload = {
        session_token: val,
        auth_mode: "session_token",
        ...accountMetaPayload(),
      };
      if (expiryField.input.value) {
        payload.expires_at = new Date(expiryField.input.value + "T23:59:59Z").toISOString();
      }
      await api(`/integrations/${integration.id}/connect`, "POST", payload);
      const modal = $("#modal");
      if (modal && modal.open) modal.close();
      await loadConnectionStatus();
      await refreshView();
      notify(`${integration.name || integration.id} er tilkoblet.`);
    });
    add(container, form);
  }

  add(root, container);
  if (typeof window !== "undefined" && window.KitchenSettingsView) {
    window.KitchenSettingsView.openConnectorModal = openConnectorModal;
  }
}
function odaConnectionIssue() {
  const messages = [
    state.basketMatchGlobalError,
    ...Object.values(state.basketMatchErrors),
    ...Object.values(state.basketMatchPages).flatMap((pages) =>
      Object.values(pages || {}).map((page) => page.detail || ""),
    ),
  ];
  return messages.find((message) =>
    window.KitchenSettingsView.odaNeedsReconnect(message),
  );
}
function renderShoppingConnectionPrompt(main) {
  const connectionIssue = state.oda?.connected ? odaConnectionIssue() : null;
  if (connectionIssue) {
    add(
      main,
      add(
        el("section", null, "callout connection-prompt"),
        el("strong", "Oda-tilkoblingen trenger oppfølging"),
        el("p", connectionIssue),
        connectionSettingsButton("Koble til Oda på nytt i innstillingene"),
      ),
    );
    return;
  }
  if (state.oda?.connected) {
    add(
      main,
      add(
        el("details", null, "shopping-info basket-oda-info"),
        el("summary", "Om produktvalg fra Oda"),
        el("p", "Produktreff er forslag. Kontroller produktet og pakningsmengden før du lagrer et valg. Kjøkken legger ikke inn bestillinger eller håndterer betaling."),
      ),
    );
    return;
  }
  const configured = Boolean(state.oda?.configured);
  add(
    main,
    add(
      el("section", null, "callout connection-prompt"),
      el("strong", configured ? "Oda er ikke tilkoblet" : "Oda må settes opp"),
      el(
        "p",
        configured
          ? "Koble til kontoen din i innstillingene for å søke etter Oda-produkter og se over varer i handlekurven."
          : "Oda-produkttreff krever oppsett. Du kan fortsatt bruke handlelisten i butikken.",
      ),
      connectionSettingsButton(),
    ),
  );
}
function renderBasket(main) {
  const basket = state.basket || { needs: [], issues: [] };
  renderShoppingConnectionPrompt(main);
  if (basket.needs?.length) {
    const hasSelections = window.KitchenShoppingView.hasSelectedProducts(basket);
    const status = el(
      "p",
      basket.ready_for_review
        ? "Klar for gjennomgang av handlekurven"
        : hasSelections
          ? "Se over valgte produkter"
          : "Velg et produkt for hvert behov",
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
    add(issues, el("summary", "Se over detaljer (" + formatUiCount(basket.issues.length) + ")"));
    const list = el("ul");
    basket.issues.forEach((issue) =>
      (() => {
        const exact =
          typeof issue === "string" ? issue : issue.message || JSON.stringify(issue);
        const item = el("li", window.KitchenShoppingView.formatBasketIssue(exact));
        item.title = exact;
        return add(list, item);
      })(),
    );
    add(issues, list);
    add(main, issues);
  }
  if (state.oda?.connected && basket.needs?.length) {
    add(main, renderBasketMatchSummary());
    renderBasketBatchReview(main);
  }
  if (!basket.needs?.length) {
    add(
      main,
      empty(
        "Ingen handlebehov å finne produkter til ennå",
        "Planlegg måltider eller legg til handlebehov først.",
        button(
          "Åpne handlebehov",
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
    const needQuantity = el("p", formatUiNeedQuantity(need), "need-quantity");
    needQuantity.setAttribute(
      "aria-label",
      (need.approximate === true ? "Omtrentlig mengde: " : "Nøyaktig mengde: ") +
        formatUiNeedQuantity(need, true),
    );
    add(
      heading,
      add(
        el("div"),
        el("h2", need.name || "Handlebehov"),
        needQuantity,
      ),
    );
    if (need.origins?.length)
      add(heading, el("p", "Fra " + shoppingOrigins(need.origins), "muted"));
    add(section, heading);
    const evidence = renderPurchaseEvidence(need);
    if (evidence) add(section, evidence);
    if (
      entry.allocated_quantity != null ||
      entry.remaining_quantity != null ||
      entry.overbuy_quantity != null
    ) {
      const allocated = String(entry.allocated_quantity ?? "0");
      const remaining = String(entry.remaining_quantity ?? "unknown");
      const overbuy = String(entry.overbuy_quantity ?? "unknown");
      const unit = need.unit || "";
      const allocatedDisplayUnit = unit ? window.KitchenShoppingView.displayUnit(unit, false, allocated) : "";
      const remainingDisplayUnit = unit ? window.KitchenShoppingView.displayUnit(unit, false, remaining) : "";
      const overbuyDisplayUnit = unit ? window.KitchenShoppingView.displayUnit(unit, false, overbuy) : "";
      const allocatedA11yUnit = unit ? window.KitchenShoppingView.displayUnit(unit, true, allocated) : "";
      const remainingA11yUnit = unit ? window.KitchenShoppingView.displayUnit(unit, true, remaining) : "";
      const overbuyA11yUnit = unit ? window.KitchenShoppingView.displayUnit(unit, true, overbuy) : "";
      const allocation = el(
        "p",
        "Dekket av valgte pakker: " +
          window.KitchenShoppingView.formatQuantityForDisplay(allocated) +
          (allocatedDisplayUnit ? " " + allocatedDisplayUnit : "") +
          " · Gjenstår: " +
          window.KitchenShoppingView.formatQuantityForDisplay(remaining) +
          (remainingDisplayUnit ? " " + remainingDisplayUnit : "") +
          " · Til overs fra valgte pakker: " +
          window.KitchenShoppingView.formatQuantityForDisplay(overbuy) +
          (overbuyDisplayUnit ? " " + overbuyDisplayUnit : ""),
        "muted",
      );
      allocation.setAttribute(
        "aria-label",
        "Nøyaktige mengder: dekket av valgte pakker " +
          window.KitchenShoppingView.formatQuantityForAccessibility(allocated) +
          (allocatedA11yUnit ? " " + allocatedA11yUnit : "") +
          "; gjenstår " +
          window.KitchenShoppingView.formatQuantityForAccessibility(remaining) +
          (remainingA11yUnit ? " " + remainingA11yUnit : "") +
          "; til overs fra valgte pakker " +
          window.KitchenShoppingView.formatQuantityForAccessibility(overbuy) +
          (overbuyA11yUnit ? " " + overbuyA11yUnit : ""),
      );
      add(section, allocation);
    }
    const selections = entry.selections || [];
    if (state.oda?.connected) {
      add(section, renderBasketMatchNeed(need));
      const manualSearch = odaSearchForNeed(need);
      const manualDetails = el("details", null, "oda-manual-search");
      manualDetails.open = Boolean(state.odaManualSearchOpen[String(need.id)]);
      add(manualDetails, el("summary", "Søk manuelt hos Oda"), manualSearch);
      manualDetails.addEventListener("toggle", () => {
        state.odaManualSearchOpen[String(need.id)] = manualDetails.open;
      });
      add(section, manualDetails);
    }
    selections.forEach((selection) => {
      if (selection.product) {
        const selected = el("div", null, "selected-product");
        add(selected, basketProduct(selection.product, selection.packages));
        add(
          selected,
          button(
            "Fjern fra gjennomgangen",
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
        add(section, el("h3", "Alternativer"));
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
      el("h2", "Legg valgte produkter til i Oda"),
      el(
        "p",
        "Kontroller de nøyaktige varene mot den gjeldende Oda-handlekurven før du bekrefter. Varer som allerede ligger i handlekurven, beholdes.",
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
          "Den siste oppdateringen kan ha blitt utført i Oda-handlekurven. Ikke prøv på nytt fra Kjøkken. Kontroller Oda-handlekurven først.",
          "callout offline",
        ),
      );
    else if (state.cartProposalHandled)
      add(
        cartPanel,
        el(
          "p",
          "De gjennomgåtte varene ble godtatt og fjernet fra denne gjennomgangen i Kjøkken.",
          "callout",
        ),
      );
    else {
      let review;
      review = button(
        state.cartProposalLoading
          ? "Kontrollerer Oda-handlekurven …"
          : "Se over varer til Oda-handlekurven",
        () => createOdaCartProposal(proposalRequest, review),
        "primary",
      );
      review.disabled = state.cartProposalLoading;
      add(cartPanel, review);
    }
    add(main, cartPanel);
  }
}
function recentFeedbackPanel(onPreference = null) {
  const node = el("section", null, "callout");
  const status = el("p", "", "muted");
  status.setAttribute("role", "status");
  const list = el("ul");
  let sequence = 0;
  const reload = button("Oppdater lagrede tilbakemeldinger", () => refresh(), "quiet small");
  add(
    node,
    el("h3", "Lagrede tilbakemeldinger og matpreferanser"),
    el("p", "Dette er det dere uttrykkelig har lagret. Det kan gi kontekst til KI-forslag, men visningen bekrefter ikke at et bestemt forslag brukte opplysningene. Ingen kjøp eller matlaging utledes av notatene.", "muted"),
    el("p", "Viser opptil 10 tilbakemeldinger og matpreferanser blant de siste 100 historikkoppføringene. Eksterne chatsamtaler vises ikke.", "muted"),
    status,
    list,
    reload,
  );
  async function refresh() {
    const current = ++sequence;
    reload.disabled = true;
    status.textContent = "Henter lagrede tilbakemeldinger …";
    try {
      const entries = await api("/interactions?limit=100");
      if (current !== sequence) return;
      if (!Array.isArray(entries)) throw new Error("Ugyldig historikksvar");
      const kinds = {
        feedback: "Tilbakemelding",
        preference: "Matpreferanse",
        recipe_rating: "Oppskriftsvurdering",
      };
      const recent = entries.filter((entry) => Object.hasOwn(kinds, entry.kind)).slice(0, 10);
      list.replaceChildren();
      recent.forEach((entry) => {
        const row = el("li");
        const recipe = state.recipes.find((item) => item.id === entry.recipe_id);
        add(row, el("strong", kinds[entry.kind] + (recipe ? " · " + recipe.title : entry.recipe_id ? " · Tidligere oppskrift" : "")));
        const data = entry.structured || {};
        const typedRating = entry.kind === "recipe_rating" &&
          (Number.isInteger(data.rating) || typeof data.would_make_again === "boolean" || typeof data.notes === "string");
        if (typedRating) {
          if (Number.isInteger(data.rating)) add(row, el("p", "Vurdering: " + data.rating + " av 5"));
          if (typeof data.would_make_again === "boolean")
            add(row, el("p", "Vil lage igjen: " + (data.would_make_again ? "Ja" : "Nei")));
          if (data.notes) add(row, el("p", data.notes));
        } else {
          add(row, el("p", entry.message));
        }
        if (entry.kind === "preference" && onPreference) {
          add(row, button("Legg matpreferansen til i søket", () => {
            status.textContent = onPreference(entry.message);
          }, "quiet small"));
        }
        const recorded = new Date(entry.occurred_at);
        const channel = { app: "appen", api: "API", mcp: "MCP" }[entry.channel] || "ukjent kanal";
        add(row, el("small", "Lagret fra " + channel + (Number.isNaN(recorded.valueOf()) ? "" : " · " + dateLabel(recorded, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }))));
        list.append(row);
      });
      status.textContent = recent.length
        ? "Lagrede opplysninger er hentet."
        : "Ingen tilbakemeldinger eller matpreferanser blant de siste 100 historikkoppføringene.";
    } catch {
      if (current === sequence)
        status.textContent = "Kunne ikke hente lagrede tilbakemeldinger. Eventuelle tidligere viste opplysninger kan være utdaterte. Prøv å oppdatere igjen.";
    } finally {
      if (current === sequence) reload.disabled = false;
    }
  }
  void refresh();
  return { node, refresh };
}
function feedbackForm(recipe) {
  const root = openDialog(recipe ? "Vurder oppskriften" : "Gi tilbakemelding");
  const history = recentFeedbackPanel();
  add(root, history.node);
  if (recipe) {
    add(
      root,
      el(
        "p",
        "Vurderingen eller notatet lagres som husholdningskontekst for framtidige forslag. Bare det du skriver inn her, blir lagret.",
        "muted",
      ),
    );
    const form = el("form");
    const rating = field("Vurdering (valgfritt)", "number", "", "Velg et heltall fra 1 til 5.");
    rating.input.min = "1";
    rating.input.max = "5";
    rating.input.step = "1";
    const wouldMakeAgain = field("Vil dere lage dette igjen? (valgfritt)", "select");
    [
      ["", "Ikke besvart"],
      ["true", "Ja"],
      ["false", "Nei"],
    ].forEach(([value, label]) => {
      const option = el("option", label);
      option.value = value;
      add(wouldMakeAgain.input, option);
    });
    const notes = field("Notater (valgfritt)", "textarea", "", "Hva fungerte bra, og hva bør endres neste gang?");
    notes.input.maxLength = 2000;
    notes.input.rows = 4;
    add(form, rating.wrap, wouldMakeAgain.wrap, notes.wrap);
    formSubmit(form, "Lagre tilbakemeldingen", async () => {
      const payload = {};
      if (rating.input.value.trim()) payload.rating = Number(rating.input.value);
      if (wouldMakeAgain.input.value) {
        payload.would_make_again = wouldMakeAgain.input.value === "true";
      }
      if (notes.input.value.trim()) payload.notes = notes.input.value.trim();
      await api(`/recipes/${recipe.id}/feedback`, "POST", payload);
      rating.input.value = "";
      wouldMakeAgain.input.value = "";
      notes.input.value = "";
      notify("Tilbakemeldingen er lagret for framtidige forslag.");
      await history.refresh();
    });
    add(root, form);
    return;
  }
  add(
    root,
    el(
      "p",
      "Notatet lagres som husholdningskontekst for framtidige forslag. Bare det du skriver inn her, blir lagret.",
      "muted",
    ),
  );
  const form = el("form");
  const kind = field("Type", "select");
  [
    ["feedback", "Generell tilbakemelding"],
    ["preference", "Matpreferanse"],
    ["recipe_rating", "Oppskriftsvurdering"],
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
    recipe ? "Vurderingen eller notatet ditt" : "Notatet ditt",
    "textarea",
    "",
    recipe
      ? "Hva fungerte bra, og hva bør endres neste gang?"
      : "Et kort notat som kan brukes i framtidige anbefalinger.",
  );
  message.input.required = true;
  message.input.maxLength = 2000;
  message.input.rows = 4;
  add(form, kind.wrap, message.wrap);
  formSubmit(form, "Lagre tilbakemeldingen", async () => {
    await api("/interactions", "POST", {
      channel: "app",
      kind: kind.input.value,
      message: message.input.value.trim(),
      structured: {},
      ...(recipe ? { recipe_id: recipe.id } : {}),
    });
    message.input.value = "";
    notify("Tilbakemeldingen er lagret for framtidige forslag.");
    await history.refresh();
  });
  add(root, form);
}
function itemForm(regular = false) {
  const root = openDialog(
    regular ? "Legg til en hverdagsvare" : "Legg til på handlelisten",
  );
  const form = el("form");
  const name = field("Varenavn");
  name.input.required = true;
  const quantity = field("Mengde", "number", "1");
  quantity.input.min = "0.001";
  quantity.input.step = "any";
  quantity.input.required = true;
  const unit = field("Enhet", "text", "stk.");
  unit.input.required = true;
  const category = field(
    "Kategori",
    "text",
    "Other",
    "Brukes til å sortere handlelisten i butikken.",
  );
  category.input.maxLength = 60;
  add(
    form,
    name.wrap,
    add(el("div", null, "form-grid"), quantity.wrap, unit.wrap),
    category.wrap,
  );
  const interval = field("Kjøpes vanligvis hver (dag)", "number", 7);
  interval.input.min = 1;
  interval.input.required = true;
  if (regular) add(form, interval.wrap);
  formSubmit(form, regular ? "Lagre hverdagsvare" : "Legg til vare", () =>
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
      regular ? "Hverdagsvaren er lagret." : "Varen er lagt til.",
    ),
  );
  add(root, form);
}
function renderConnectionSettings() {
  const section = add(
    el("section", null, "panel connection-settings"),
    add(
      el("div", null, "connection-settings-heading"),
      el("h2", "Tilkoblinger"),
      button(
        state.settingsRefreshInFlight ? "Oppdaterer …" : "Oppdater status",
        refreshConnectionSettings,
        "quiet small",
      ),
    ),
    el("p", "Administrer leverandørtilgang her. Hemmeligheter vises aldri i Kjøkken.", "muted"),
  );
  const refresh = section.querySelector("button");
  refresh.id = "settings-connection-refresh";
  refresh.dataset.focusKey = "settings-connection-refresh";
  refresh.disabled = state.settingsRefreshInFlight;
  refresh.setAttribute(
    "aria-label",
    state.settingsRefreshInFlight ? "Oppdaterer tilkoblingsstatus" : "Oppdater tilkoblingsstatus",
  );
  const integrations = Array.isArray(state.integrations) ? [...state.integrations] : [];
  if (state.oda && !integrations.some((integration) => integration.id === "oda"))
    integrations.unshift({
      id: "oda",
      name: "Oda",
      status: state.oda.connected ? "connected" : state.oda.configured ? "configured" : "not_configured",
      supported_auth_modes: [],
      detail: state.oda.detail || "Oda-tilkoblingsstatus ble hentet separat.",
    });
  integrations.forEach((integration) => {
    const status = integration.id === "oda" && state.oda
      ? state.oda?.connected
        ? "connected"
        : state.oda?.configured
          ? "configured"
          : "not_configured"
      : integration.status;
    const row = add(
      el("article", null, "connection-card"),
      add(
        el("div", null, "connection-card-heading"),
        el("h3", integration.name || integration.id),
        el(
          "span",
          window.KitchenSettingsView.integrationStatusLabel(status),
          "connection-status",
        ),
      ),
    );
    const activeMode = integration.id === "oda" && !state.oda?.connected
      ? null
      : integration.auth_mode;
    const supportedMethods = (integration.supported_auth_modes || [])
      .map((mode) => window.KitchenSettingsView.integrationAuthLabel(mode))
      .join(", ");
    const authentication = activeMode
      ? "Autentisering: " + window.KitchenSettingsView.integrationAuthLabel(activeMode, true)
      : "Støttede metoder: " + (supportedMethods || "Ikke oppgitt");
    add(
      row,
      el(
        "p",
        integration.id === "oda" && state.oda
          ? state.oda?.detail || integration.detail || "Ingen tilkoblingsdetaljer for Oda er tilgjengelige."
          : integration.detail || "Ingen tilkoblingsdetaljer er tilgjengelige.",
        "muted",
      ),
      el(
        "p",
        authentication,
        "connection-auth-mode muted",
      ),
    );
    const expiryNotice = window.KitchenSettingsView.integrationExpiryNotice(integration);
    if (expiryNotice) {
      add(
        row,
        el(
          "p",
          expiryNotice,
          status === "expired" ? "callout danger connection-expiry" : "callout connection-expiry",
        ),
      );
    }
    if (integration.id === "oda" && state.oda) {
      const actions = add(el("div", null, "actions connection-actions"));
      if (state.oda?.connected) {
        add(
          actions,
          button("Koble til Oda på nytt", connectOdaAccount, "quiet"),
          button("Koble fra Oda", disconnectOdaAccount, "quiet danger"),
        );
      } else if (state.oda?.configured) {
        add(actions, button("Koble til Oda-konto", connectOdaAccount, "primary"));
      } else {
        const setup = button("Oppsett kreves", () => {}, "quiet");
        setup.disabled = true;
        add(actions, setup);
      }
      add(row, actions);
    } else if (["rema1000", "coop", "trumf", "cookidoo"].includes(integration.id)) {
      const multiAccountProvider = ["rema1000", "coop", "trumf"].includes(integration.id);
      const accounts = Array.isArray(integration.accounts) ? integration.accounts : [];
      if (accounts.length > 0 && multiAccountProvider) {
        const list = add(el("div", null, "connection-accounts-list stack"));
        accounts.forEach((account) => {
          const accName = window.KitchenSettingsView?.accountDisplayName
            ? window.KitchenSettingsView.accountDisplayName(account)
            : (account.account_label || account.account_id || "Standardkonto");
          const accRow = add(el("div", null, "connection-account-row"));
          add(
            accRow,
            add(
              el("div", null, "connection-account-heading"),
              el("strong", accName),
              el(
                "span",
                window.KitchenSettingsView.integrationStatusLabel(account.status || status),
                "connection-status",
              ),
            ),
          );
          const metaParts = [
            window.KitchenSettingsView.integrationAuthLabel(account.auth_mode || account.auth_type, true),
          ];
          if (account.connected_by_actor_id) {
            metaParts.push(`Koblet til av ${account.connected_by_actor_id}`);
          }
          const formattedExpiry = window.KitchenSettingsView?.formatAccountExpiry
            ? window.KitchenSettingsView.formatAccountExpiry(account)
            : null;
          if (formattedExpiry) {
            metaParts.push(`Utløper: ${formattedExpiry}`);
          }
          add(accRow, el("p", metaParts.join(" · "), "muted connection-account-meta"));
          const accNotice = window.KitchenSettingsView.integrationExpiryNotice(account);
          if (accNotice) {
            add(
              accRow,
              el(
                "p",
                accNotice,
                account.status === "expired" ? "callout danger connection-expiry" : "callout connection-expiry",
              ),
            );
          }
          if (account.last_error) {
            add(accRow, el("p", account.last_error, "callout danger connection-expiry"));
          }
          const accActions = add(el("div", null, "actions connection-account-actions"));
          add(
            accActions,
            button(
              "Forny",
              () => {
                if (account.refresh_available) {
                  return refreshConnectorAccount(integration, account);
                }
                openConnectorModal(integration, {
                  accountId: account.account_id,
                  accountLabel: account.account_label || "",
                });
              },
              account.status === "expired" ? "primary" : "quiet",
            ),
            button(
              "Koble fra",
              () => disconnectConnectorAccount(integration.id, account.account_id, accName),
              "quiet danger",
            ),
          );
          add(accRow, accActions);
          add(list, accRow);
        });
        add(row, list);
        const actions = add(el("div", null, "actions connection-actions"));
        add(
          actions,
          button(
            "Koble til en konto til",
            () => openConnectorModal(integration, { addAccount: true }),
            "quiet",
          ),
        );
        add(row, actions);
      } else {
        const actions = add(el("div", null, "actions connection-actions"));
        if (status === "connected") {
          add(
            actions,
            button("Oppdater tilkobling", () => openConnectorModal(integration), "quiet"),
            button("Koble fra", () => disconnectConnector(integration.id, integration.name), "quiet danger"),
          );
        } else if (status === "expired") {
          add(
            actions,
            button("Forny tilkobling", () => openConnectorModal(integration), "primary"),
            button("Koble fra", () => disconnectConnector(integration.id, integration.name), "quiet danger"),
          );
        } else {
          add(
            actions,
            button("Koble til", () => openConnectorModal(integration), "primary"),
          );
        }
        add(row, actions);
      }
    }
    section.append(row);
  });
  if (state.integrationsError)
    add(section, el("p", state.integrationsError, "callout"));
  if (state.odaStatusError)
    add(section, el("p", state.odaStatusError, "callout"));
  if (!integrations.length && !state.integrationsError && !state.odaStatusError)
    add(section, el("p", "Tilkoblingsstatus er ikke tilgjengelig ennå. Oppdater for å prøve igjen.", "muted"));
  add(
    section,
    el("p", "På Android kan du installere Kjøkken fra nettlesermenyen. Appen åpnes uten nett, men delte oppskrifter og endringer krever tilkobling.", "muted"),
  );
  return section;
}
function renderSettings(main) {
  const household = state.household;
  const first = !household.name;
  add(
    main,
    title(
      "Gjør kjøkkenet til ditt eget",
      first ? "Velkommen til kjøkkenet." : "Husholdningen din.",
      first
        ? "Noen få opplysninger gjør måltidsplanleggingen mer relevant. Du kan endre dem når som helst."
        : "Personene, preferansene og porsjonene som planen bygger på.",
    ),
  );
  const form = el("form", null, "panel");
  const name = field(
    "Navn på husholdningen",
    "text",
    household.name || "",
    "Velg et navn på kjøkkenet dere deler.",
  );
  name.input.required = true;
  name.input.maxLength = 100;
  const adults = field("Voksne", "number", household.adults ?? 1);
  adults.input.min = 0;
  adults.input.max = 30;
  adults.input.required = true;
  const children = field(
    "Barnas alder",
    "text",
    (household.children_ages || []).join(", "),
    "Valgfritt. Skill alderen med komma, for eksempel: 4, 8.",
  );
  const servings = field(
    "Standard antall porsjoner",
    "number",
    household.default_servings || 2,
  );
  servings.input.min = 1;
  servings.input.max = 100;
  servings.input.required = true;
  const language = field("Foretrukket språk", "select");
  [
    ["en", "Engelsk"],
    ["nb", "Norsk (bokmål)"],
    ["fr", "Fransk"],
  ].forEach(([value, label]) => {
    const option = el("option", label);
    option.value = value;
    add(language.input, option);
  });
  language.input.value = household.language || "nb";
  const preferences = field(
    "Matpreferanser",
    "textarea",
    (household.dietary_preferences || []).join("\n"),
    "Valgfritt. Én per linje, for eksempel allergier eller ingredienser dere vil unngå. Kontroller alltid oppskrifter og produktetiketter.",
  );
  preferences.input.rows = 3;
  let nextCategoryKey = 0;
  let categoryRows = (household.shopping_category_order || []).map((value) => ({
    ...window.KitchenSettingsView.createShoppingCategoryRow(value, categoryLabel),
    key: String(nextCategoryKey++),
  }));
  const categoryEditor = add(
    el("section", null, "category-order-settings"),
    el("h2", "Rekkefølge i butikken"),
    el(
      "p",
      "Velg rekkefølgen på kategoriene i butikklisten og den delte handlelisten. Kategorier som ikke er oppført, sorteres alfabetisk. Dette er husholdningens preferanse, ikke et butikkart.",
      "muted",
    ),
  );
  const categoryList = el("ol", null, "category-order-list");
  const renderCategoryRows = (focusKey = null) => {
    categoryList.replaceChildren();
    if (!categoryRows.length)
      add(categoryList, el("li", "Alle kategorier sorteres alfabetisk.", "muted category-order-empty"));
    categoryRows.forEach((row, index) => {
      const item = el("li", null, "category-order-row");
      const input = el("input");
      input.type = "text";
      input.maxLength = 100;
      input.value = row.displayValue;
      input.setAttribute("aria-label", "Kategori på plass " + formatUiCount(index + 1));
      input.dataset.focusKey = "shopping-category-" + row.key;
      input.addEventListener("input", () => {
        Object.assign(
          row,
          window.KitchenSettingsView.editShoppingCategoryRow(row, input.value, categoryLabel),
        );
      });
      const moveUp = button("Flytt opp", () => {
        categoryRows = window.KitchenSettingsView.moveShoppingCategory(categoryRows, index, index - 1);
        renderCategoryRows("shopping-category-" + row.key);
      }, "quiet category-order-move");
      moveUp.disabled = index === 0;
      moveUp.setAttribute("aria-label", "Flytt " + row.displayValue + " opp");
      const moveDown = button("Flytt ned", () => {
        categoryRows = window.KitchenSettingsView.moveShoppingCategory(categoryRows, index, index + 1);
        renderCategoryRows("shopping-category-" + row.key);
      }, "quiet category-order-move");
      moveDown.disabled = index === categoryRows.length - 1;
      moveDown.setAttribute("aria-label", "Flytt " + row.displayValue + " ned");
      const remove = button("Fjern", () => {
        categoryRows = categoryRows.filter((category) => category.key !== row.key);
        const next = categoryRows[Math.min(index, categoryRows.length - 1)];
        renderCategoryRows(next ? "shopping-category-" + next.key : null);
        if (!next) categoryEntry.input.focus();
      }, "quiet category-order-remove");
      remove.setAttribute("aria-label", "Fjern " + row.displayValue);
      add(item, input, moveUp, moveDown, remove);
      categoryList.append(item);
    });
    if (focusKey)
      categoryList.querySelector('[data-focus-key="' + focusKey + '"]')?.focus({ preventScroll: true });
  };
  renderCategoryRows();
  const categoryEntry = field("Legg til en kategori", "text", "", "For eksempel: Bakeri eller Husholdning.");
  categoryEntry.input.maxLength = 100;
  categoryEntry.input.dataset.focusKey = "new-shopping-category";
  const categoryError = el("p", "", "form-error");
  categoryError.setAttribute("role", "alert");
  const addCategory = button("Legg til kategori", () => {
    const value = categoryEntry.input.value.trim();
    const normalized = window.KitchenSettingsView.normalizeShoppingCategoryOrder(
      categoryRows.map((row) => row.value),
    );
    if (!value) {
      categoryError.textContent = "Skriv inn et kategorinavn.";
      categoryEntry.input.focus();
      return;
    }
    if (value.length > 100) {
      categoryError.textContent = "Kategorinavn kan være opptil 100 tegn.";
      categoryEntry.input.focus();
      return;
    }
    if (categoryRows.length >= 64) {
      categoryError.textContent = "Du kan legge til opptil 64 kategorier.";
      return;
    }
    if (
      normalized.error === "duplicate" ||
      categoryRows.some((row) => row.displayValue.toLowerCase() === value.toLowerCase()) ||
      normalized.value?.some((category) => category.toLowerCase() === value.toLowerCase())
    ) {
      categoryError.textContent = "Hver kategori må ha et unikt navn.";
      categoryEntry.input.focus();
      return;
    }
    categoryRows.push({
      ...window.KitchenSettingsView.createShoppingCategoryRow(value, (entry) => entry),
      key: String(nextCategoryKey++),
    });
    categoryEntry.input.value = "";
    categoryError.textContent = "";
    renderCategoryRows();
    categoryEntry.input.focus();
  }, "quiet");
  categoryEntry.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addCategory.click();
    }
  });
  const restoreDefaults = button("Gjenopprett anbefalt rekkefølge", () => {
    categoryRows = window.KitchenSettingsView.DEFAULT_SHOPPING_CATEGORY_ORDER.map((value) => ({
      ...window.KitchenSettingsView.createShoppingCategoryRow(value, categoryLabel),
      key: String(nextCategoryKey++),
    }));
    categoryError.textContent = "";
    renderCategoryRows();
  }, "quiet");
  const alphabetical = button("Sorter alfabetisk", () => {
    categoryRows = [];
    categoryError.textContent = "";
    renderCategoryRows();
  }, "quiet");
  add(
    categoryEditor,
    categoryList,
    add(el("div", null, "category-order-add"), categoryEntry.wrap, addCategory),
    categoryError,
    add(el("div", null, "actions category-order-presets"), restoreDefaults, alphabetical),
  );
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
      "Grensesnittet vises på bokmål. Språkvalget brukes som kontekst for oppskrifter og husholdningen.",
      "muted",
    ),
    categoryEditor,
  );
  formSubmit(form, first ? "Sett opp kjøkkenet" : "Lagre husholdningsinnstillinger", async () => {
    const values = children.input.value
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    if (values.some((value) => !/^\d+$/.test(value) || Number(value) > 17))
      throw new Error(
        "Oppgi barnas alder som hele tall fra 0 til 17, adskilt med komma.",
      );
    const order = window.KitchenSettingsView.normalizeShoppingCategoryOrder(
      categoryRows.map((row) => row.value),
    );
    if (order.error === "duplicate")
      throw new Error("Kategorinavn må være unike, uavhengig av store og små bokstaver.");
    if (order.error === "too_many")
      throw new Error("Du kan lagre opptil 64 handlekategorier.");
    if (order.error)
      throw new Error("Hver handlekategori må ha et navn på høyst 100 tegn.");
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
      shopping_category_order: order.value,
    });
    notify("Husholdningsinnstillingene er lagret.");
    if (first) navigate("week");
  });
  add(main, form);
  const unmappedSection = el("section", null, "panel unmapped-ingredients-settings");
  add(
    unmappedSection,
    el("h2", "Råvarer og måleenheter"),
    el(
      "p",
      "Koble ingredienser i oppskriftene dine til kanoniske råvarer og måleenheter. Dette gjør det mulig å gjenkjenne råvarer på tvers av oppskrifter og språk, uten å endre den opprinnelige oppskriftsteksten.",
      "muted",
    ),
    button("Gjennomgå ukartlagte råvarer", () => unmappedIngredientsDialog(), "quiet"),
    button("Åpne råvareoversikt og preferanser", () => navigate("catalogue"), "quiet"),
  );
  add(main, unmappedSection);
  main.append(renderConnectionSettings());
}
function renderCatalogue(main) {
  if (window.KitchenProductOverviewView) {
    window.KitchenProductOverviewView.renderProductOverview(main, {
      api,
      notify,
      locale: state.household?.language || "nb",
    });
  }
}
$("#modal").addEventListener("cancel", (event) => {
  if (state.cartConfirming) event.preventDefault();
});
$("#modal").addEventListener("close", () => {
  const modal = $("#modal");
  if (modal.dataset.recipeCollection !== "true") return;
  delete modal.dataset.recipeCollection;
  state.view = "recipes";
  location.hash = "recipes";
  render();
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
  if (nextView === "shopping" && view !== "basket" && nextView !== state.view)
    state.shoppingPanel = "single";
  if (
    ["week", "recipes", "shopping", "settings", "catalogue"].includes(nextView) &&
    (nextView !== state.view || panelChanged)
  ) {
    state.view = nextView;
    window.scrollTo(0, 0);
    refreshView();
  }
});
window.addEventListener("offline", () => {
  notify(
    "Du er frakoblet. Koble til igjen før du lagrer endringene. Det åpne skjemaet blir liggende her.",
    true,
  );
});
window.addEventListener("online", () => {
  notify("Tilkoblingen er gjenopprettet.");
  if (state.view === "shopping") void refreshShoppingIfSafe(true);
  else if (!$("#modal").open && !$("#main form")) refreshView();
});
function refreshShoppingOnReturn() {
  if (document.visibilityState !== "hidden") void refreshShoppingIfSafe();
}
window.addEventListener("focus", refreshShoppingOnReturn);
window.addEventListener("pageshow", refreshShoppingOnReturn);
document.addEventListener("visibilitychange", refreshShoppingOnReturn);
async function refreshTrackedRecipeCollectionJob() {
  const jobId = state.recipeCollectionJob?.job_id;
  if (!jobId || !["previewing", "importing"].includes(state.recipeCollectionJob.status)) return;
  try {
    const job = window.KitchenRecipeCollections.normalizeJob(
      await api("/recipe-collections/" + encodeURIComponent(jobId)),
    );
    if (state.recipeCollectionJob?.job_id !== jobId) return;
    rememberRecipeCollectionJob(job);
    if (["completed", "cancelled"].includes(job.status)) {
      state.recipes = await api("/recipes?include_stats=true");
      if (state.view === "recipes") render();
    }
  } catch {}
}
window.addEventListener("focus", refreshTrackedRecipeCollectionJob);
window.addEventListener("pageshow", refreshTrackedRecipeCollectionJob);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void refreshTrackedRecipeCollectionJob();
});
$("#modal").addEventListener("close", queuePendingShoppingRefresh);
async function start() {
  try {
    let rememberedCollectionId = null;
    try { rememberedCollectionId = sessionStorage.getItem("recipeCollectionJobId"); } catch {}
    const odaResult = new URLSearchParams(location.search).get("oda");
    const params = new URLSearchParams(location.search);
    const requestedDate = parseDate(params.get("date"));
    if (requestedDate) {
      state.selectedDate = new Date(requestedDate);
      state.week = startOfWeek(requestedDate);
    }
    const requestedDay = parseDate(params.get("day"));
    if (
      requestedDay &&
      requestedDay >= state.week &&
      requestedDay <= day(6)
    ) state.selectedDate = requestedDay;
    [state.household, state.recipes] = await Promise.all([
      api("/household"),
      api("/recipes?include_stats=true"),
    ]);
    await loadConnectionStatus();
    if (rememberedCollectionId) {
      try {
        rememberRecipeCollectionJob(window.KitchenRecipeCollections.normalizeJob(
          await api("/recipe-collections/" + encodeURIComponent(rememberedCollectionId)),
        ));
      } catch {
        rememberRecipeCollectionJob(null);
      }
    }
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
    if (state.view === "settings") render();
    else await refreshView();
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
            ? "Oda er koblet til Kjøkken."
            : "Kjøkken kunne ikke bekrefte Oda-tilkoblingen.",
          !state.oda?.connected,
        );
      } else {
        notify("Oda-tilkoblingen ble ikke fullført. Prøv igjen.", true);
      }
    }
  } catch (error) {
    const main = $("#main");
    main.replaceChildren(
      title(
        "Velkommen til Kjøkken",
        "La oss koble til.",
        "Kjøkkenet dere deler, trenger tilkobling for å lastes inn.",
      ),
    );
    add(
      main,
      empty(
        "Kjøkkenet er ikke tilgjengelig",
        error.message,
        button("Prøv igjen", start, "primary"),
      ),
    );
  }
}
if ("serviceWorker" in navigator)
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
fetch("/api/version")
  .then((response) => response.json())
  .then((data) => {
    const element = $("#app-version");
    if (element) element.textContent = data.version + " · " + data.channel;
  })
  .catch(() => {});
start();
