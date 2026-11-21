(function (root) {
  "use strict";

  const LANGUAGE_NAMES = {
    nb: "Norsk (bokmål)",
    en: "Engelsk",
    da: "Dansk",
    sv: "Svensk",
    fr: "Fransk",
    und: "Ukjent språk",
  };

  const TARGET_LOCALES = ["nb", "en", "da", "sv", "fr"];

  function formatLanguageName(code) {
    if (!code) return "Ukjent språk";
    const key = String(code).trim().toLowerCase();
    return LANGUAGE_NAMES[key] || code;
  }

  function renderLanguageBadge(sourceLanguage) {
    const badge = document.createElement("span");
    badge.className = "badge recipe-language-badge";
    const lang = sourceLanguage || "und";
    badge.dataset.language = lang;
    badge.textContent = `Kildespråk: ${formatLanguageName(lang)}`;
    badge.setAttribute("aria-label", `Oppskriftens kildespråk er ${formatLanguageName(lang)}`);
    return badge;
  }

  function getTranslationErrorMessage(error, action = "translate") {
    const msg = String(error?.message || error || "");
    const status = error?.status || (error?.response && error.response.status);
    if (action === "save") {
      if (status === 404) {
        return "Oppskriften ble ikke funnet.";
      }
      if (status === 400 || status === 422) {
        return "Ugyldige oversettelsesdata: " + (msg || "Kontroller feltene og prøv igjen.");
      }
      return "Kunne ikke lagre oversettelsen: " + (msg || "Ukjent feil oppstod.");
    }
    if (
      status === 503 ||
      msg.includes("Configure an AI model") ||
      msg.includes("AI-modell") ||
      msg.includes("AI model")
    ) {
      return "AI-modell er ikke konfigurert. Konfigurer en AI-modell under Innstillinger for å aktivere oversettelse.";
    }
    if (status === 404) {
      return "Oppskriften eller oversettelsen ble ikke funnet.";
    }
    return "Kunne ikke oversette oppskriften: " + (msg || "Ukjent feil oppstod.");
  }

  function normalizeTranslationQuantity(value) {
    if (value == null) return { value: null, valid: true };
    if (typeof value === "number") {
      if (!Number.isFinite(value) || value <= 0) return { value: null, valid: false };
      return { value: String(value), valid: true };
    }
    const raw = String(value).trim();
    if (!raw) return { value: null, valid: true };
    if (!/^(?:[0-9]+(?:[.,][0-9]+)?|[.,][0-9]+)$/u.test(raw)) {
      return { value: null, valid: false };
    }
    const normalized = raw.replace(",", ".");
    if (!/[1-9]/u.test(normalized.replace(".", ""))) {
      return { value: null, valid: false };
    }
    return { value: normalized, valid: true };
  }

  function buildManualTranslationPayload(values) {
    const title = String(values?.title ?? "").trim();
    if (!title) {
      throw new Error("Oppgi en tittel for oversettelsen.");
    }

    const rawInstructions = Array.isArray(values?.instructions)
      ? values.instructions
      : String(values?.instructions ?? "").split("\n");
    const instructions = rawInstructions.map((line) => String(line ?? "").trim()).filter(Boolean);
    if (!instructions.length) {
      throw new Error("Legg til minst ett steg i fremgangsmåten.");
    }

    const rawIngredients = Array.isArray(values?.ingredients) ? values.ingredients : [];
    if (!rawIngredients.length) {
      throw new Error("Legg til minst én ingrediens i oversettelsen.");
    }

    const ingredients = rawIngredients.map((item) => {
      const name = String(item?.name ?? "").trim();
      if (!name) {
        throw new Error("Oppgi navn på alle ingrediensene i oversettelsen.");
      }
      const { value: quantity, valid } = normalizeTranslationQuantity(item?.quantity);
      if (!valid) {
        throw new Error(
          "Oppgi en gyldig positiv mengde med komma eller punktum, eller la mengdefeltet stå tomt.",
        );
      }
      const unit = String(item?.unit ?? "").trim() || null;
      if (quantity !== null && !unit) {
        throw new Error("Oppgi enhet for alle ingredienser som har mengde.");
      }
      const category = String(item?.category ?? "Other").trim() || "Other";
      const built = {
        name,
        quantity,
        unit,
        category,
        source_indices: Array.isArray(item?.source_indices) ? [...item.source_indices] : [],
      };
      if (item?.purchase_estimate && typeof item.purchase_estimate === "object") {
        built.purchase_estimate = { ...item.purchase_estimate };
      }
      return built;
    });

    return { title, ingredients, instructions };
  }

  async function fetchTranslation(recipeId, locale, api) {
    if (!recipeId) throw new Error("Oppskrifts-ID mangler.");
    if (!locale) throw new Error("Målspråk mangler.");
    return await api(`/recipes/${encodeURIComponent(recipeId)}/translations/${encodeURIComponent(locale)}`);
  }

  async function fetchTranslationsList(recipeId, api) {
    if (!recipeId) throw new Error("Oppskrifts-ID mangler.");
    return await api(`/recipes/${encodeURIComponent(recipeId)}/translations`);
  }

  async function requestTranslation(recipeId, targetLocale, options = {}) {
    if (!recipeId) throw new Error("Oppskrifts-ID mangler.");
    if (!targetLocale) throw new Error("Målspråk mangler.");
    const api = options.api;
    if (!api) throw new Error("API-funksjon er påkrevd.");
    const payload = {
      target_locale: targetLocale,
      force: Boolean(options.force),
    };
    return await api(`/recipes/${encodeURIComponent(recipeId)}/translate`, "POST", payload);
  }

  async function saveManualTranslation(recipeId, targetLocale, payload, options = {}) {
    if (!recipeId) throw new Error("Oppskrifts-ID mangler.");
    if (!targetLocale) throw new Error("Målspråk mangler.");
    const api = typeof options === "function" ? options : options.api;
    if (!api) throw new Error("API-funksjon er påkrevd.");
    const locale = String(targetLocale).trim().toLowerCase();
    if (!locale) throw new Error("Målspråk mangler.");
    const cleanPayload = buildManualTranslationPayload(payload);
    return await api(
      `/recipes/${encodeURIComponent(recipeId)}/translations/${encodeURIComponent(locale)}`,
      "PUT",
      cleanPayload,
    );
  }

  let translationFieldCounter = 0;

  function createLabeledField(labelText, inputType, className) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const span = document.createElement("span");
    span.textContent = labelText;
    const input = document.createElement(inputType === "textarea" ? "textarea" : "input");
    const fieldId = `translation-field-${++translationFieldCounter}`;
    input.id = fieldId;
    wrap.setAttribute("for", fieldId);
    if (inputType !== "textarea") {
      input.type = inputType;
    }
    if (className) input.className = className;
    wrap.append(span, input);
    return { wrap, input };
  }

  function attachRecipeTranslation(container, recipe, options = {}) {
    if (!container || !recipe) return null;

    const api = options.api;
    const notify = options.notify || (() => {});
    const onContentChange = options.onContentChange || (() => {});
    const rawLocale = String(options.locale ?? "").trim();
    const lowerLocale = rawLocale.toLowerCase();
    const intlLocale =
      !lowerLocale || lowerLocale === "nb" || lowerLocale === "nb-no" || lowerLocale === "und"
        ? "nb-NO"
        : lowerLocale === "en" || lowerLocale === "fr"
          ? lowerLocale
          : rawLocale;

    const translationsByLocale = new Map();
    let currentTranslation = null;
    let isViewingTranslation = false;
    let isEditingTranslation = false;
    let isLoading = false;
    let isSavingEdit = false;

    // Default target locale: if source is 'nb', translate to 'en'; otherwise 'nb'
    const sourceLang = (recipe.source_language || "und").toLowerCase();
    let targetLocale = sourceLang === "nb" ? "en" : "nb";

    const section = document.createElement("section");
    section.className = "recipe-translation-panel";
    section.setAttribute("aria-label", "Oppskriftsspråk og oversettelse");

    // Header with detected source badge and current view status
    const headerRow = document.createElement("div");
    headerRow.className = "recipe-translation-header";

    const badge = renderLanguageBadge(recipe.source_language);
    headerRow.append(badge);

    const statusIndicator = document.createElement("span");
    statusIndicator.className = "translation-mode-indicator";
    statusIndicator.textContent = "Viser original";
    headerRow.append(statusIndicator);

    section.append(headerRow);

    // Controls row
    const controlsRow = document.createElement("div");
    controlsRow.className = "recipe-translation-controls actions";

    // Target locale selector
    const localeSelect = document.createElement("select");
    localeSelect.className = "translation-locale-select";
    localeSelect.setAttribute("aria-label", "Velg målspråk for oversettelse");

    function ensureLocaleOption(code) {
      const normalized = String(code || "").trim().toLowerCase();
      if (!normalized) return;
      const exists = Array.from(localeSelect.children).some((opt) => opt.value === normalized);
      if (exists) return;
      const opt = document.createElement("option");
      opt.value = normalized;
      opt.textContent = formatLanguageName(normalized);
      localeSelect.append(opt);
    }

    TARGET_LOCALES.forEach(ensureLocaleOption);
    ensureLocaleOption(targetLocale);
    localeSelect.value = targetLocale;
    localeSelect.addEventListener("change", (e) => {
      targetLocale = String(e.target.value || "nb").trim().toLowerCase();
      currentTranslation = translationsByLocale.get(targetLocale) || null;
      clearError();
      clearStatus();
      updateControls();
    });
    controlsRow.append(localeSelect);

    // Translate button
    const translateBtn = document.createElement("button");
    translateBtn.type = "button";
    translateBtn.className = "translate-btn primary";
    translateBtn.textContent = `Oversett til ${formatLanguageName(targetLocale)}`;
    controlsRow.append(translateBtn);

    // Toggle back to original button (one-click toggle)
    const viewOriginalBtn = document.createElement("button");
    viewOriginalBtn.type = "button";
    viewOriginalBtn.className = "view-original-btn quiet";
    viewOriginalBtn.textContent = "Vis originaloppskrift";
    viewOriginalBtn.hidden = true;
    controlsRow.append(viewOriginalBtn);

    // Toggle to translation button (when already loaded and looking at original)
    const viewTranslationBtn = document.createElement("button");
    viewTranslationBtn.type = "button";
    viewTranslationBtn.className = "view-translation-btn quiet";
    viewTranslationBtn.textContent = "Vis oversettelse";
    viewTranslationBtn.hidden = true;
    controlsRow.append(viewTranslationBtn);

    // Refresh translation button (when stale or force requested)
    const refreshBtn = document.createElement("button");
    refreshBtn.type = "button";
    refreshBtn.className = "refresh-translation-btn quiet";
    refreshBtn.textContent = "Oppdater oversettelse";
    refreshBtn.hidden = true;
    controlsRow.append(refreshBtn);

    // Manual edit translation button
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "edit-translation-btn quiet";
    editBtn.textContent = "Rediger oversettelse";
    controlsRow.append(editBtn);

    section.append(controlsRow);

    // Meta details container (shows provider, model, version, date)
    const metaContainer = document.createElement("div");
    metaContainer.className = "recipe-translation-meta";
    metaContainer.hidden = true;
    section.append(metaContainer);

    // Manual translation edit form
    const editForm = document.createElement("form");
    editForm.className = "translation-edit-form";
    editForm.setAttribute("aria-label", "Rediger oversettelse manuelt");
    editForm.hidden = true;

    const editHeading = document.createElement("h4");
    editHeading.className = "translation-edit-heading";
    editForm.append(editHeading);

    const editNote = document.createElement("p");
    editNote.className = "muted translation-edit-note";
    editNote.textContent =
      "Endringene lagres bare for oversettelsen og påvirker ikke originaloppskriften.";
    editForm.append(editNote);

    const titleField = createLabeledField("Tittel", "text", "translation-edit-title");
    titleField.input.setAttribute("aria-label", "Oversatt tittel");
    editForm.append(titleField.wrap);

    const ingredientsFieldset = document.createElement("fieldset");
    ingredientsFieldset.className = "translation-edit-ingredients";
    const ingredientsLegend = document.createElement("legend");
    ingredientsLegend.textContent = "Ingredienser";
    ingredientsFieldset.append(ingredientsLegend);

    const ingredientRowsContainer = document.createElement("div");
    ingredientRowsContainer.className = "translation-edit-ingredient-rows";
    ingredientsFieldset.append(ingredientRowsContainer);

    let editIngredientRows = [];

    function syncEditIngredientRowsFromDom() {
      const rowEls = ingredientRowsContainer.querySelectorAll(".translation-edit-ingredient-row");
      editIngredientRows.forEach((rowState, index) => {
        const rowEl = rowEls[index];
        if (!rowEl) return;
        const nameInput = rowEl.querySelector(".translation-ingredient-name");
        const quantityInput = rowEl.querySelector(".translation-ingredient-quantity");
        const unitInput = rowEl.querySelector(".translation-ingredient-unit");
        if (nameInput) rowState.name = nameInput.value;
        if (quantityInput) rowState.quantity = quantityInput.value;
        if (unitInput) rowState.unit = unitInput.value;
      });
    }

    function renderEditIngredientRows() {
      ingredientRowsContainer.replaceChildren();
      editIngredientRows.forEach((rowState, index) => {
        const rowNumber = index + 1;
        const rowEl = document.createElement("div");
        rowEl.className = "translation-edit-ingredient-row";

        const nameField = createLabeledField("Navn", "text", "translation-ingredient-name");
        nameField.input.value = rowState.name;
        nameField.input.setAttribute("aria-label", `Ingrediensnavn ${rowNumber}`);
        nameField.input.addEventListener("input", (e) => {
          rowState.name = e.target.value;
        });

        const quantityField = createLabeledField("Mengde", "text", "translation-ingredient-quantity");
        quantityField.input.value = rowState.quantity;
        quantityField.input.setAttribute("aria-label", `Mengde ${rowNumber}`);
        quantityField.input.addEventListener("input", (e) => {
          rowState.quantity = e.target.value;
        });

        const unitField = createLabeledField("Enhet", "text", "translation-ingredient-unit");
        unitField.input.value = rowState.unit;
        unitField.input.setAttribute("aria-label", `Enhet ${rowNumber}`);
        unitField.input.addEventListener("input", (e) => {
          rowState.unit = e.target.value;
        });

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "remove-translation-ingredient-btn quiet small";
        removeBtn.textContent = "Fjern";
        removeBtn.setAttribute("aria-label", `Fjern ingrediens ${rowNumber}`);
        removeBtn.addEventListener("click", () => {
          syncEditIngredientRowsFromDom();
          editIngredientRows = editIngredientRows.filter((item) => item !== rowState);
          renderEditIngredientRows();
        });

        rowEl.append(nameField.wrap, quantityField.wrap, unitField.wrap, removeBtn);
        ingredientRowsContainer.append(rowEl);
      });
    }

    const addIngredientBtn = document.createElement("button");
    addIngredientBtn.type = "button";
    addIngredientBtn.className = "add-translation-ingredient-btn quiet";
    addIngredientBtn.textContent = "+ Legg til ingrediens";
    addIngredientBtn.addEventListener("click", () => {
      syncEditIngredientRowsFromDom();
      editIngredientRows.push({
        name: "",
        quantity: "",
        unit: "",
        category: "Other",
        source_indices: [],
        purchase_estimate: null,
      });
      renderEditIngredientRows();
    });
    ingredientsFieldset.append(addIngredientBtn);
    editForm.append(ingredientsFieldset);

    const instructionsField = createLabeledField(
      "Fremgangsmåte",
      "textarea",
      "translation-edit-instructions",
    );
    instructionsField.input.setAttribute("aria-label", "Oversatt fremgangsmåte");
    const instructionsHelp = document.createElement("small");
    instructionsHelp.textContent = "Skriv ett trinn per linje.";
    instructionsField.wrap.append(instructionsHelp);
    editForm.append(instructionsField.wrap);

    const editActions = document.createElement("div");
    editActions.className = "actions translation-edit-actions";

    const saveEditBtn = document.createElement("button");
    saveEditBtn.type = "button";
    saveEditBtn.className = "save-translation-btn primary";
    saveEditBtn.textContent = "Lagre oversettelse";

    const cancelEditBtn = document.createElement("button");
    cancelEditBtn.type = "button";
    cancelEditBtn.className = "cancel-translation-edit-btn quiet";
    cancelEditBtn.textContent = "Avbryt";

    editActions.append(saveEditBtn, cancelEditBtn);
    editForm.append(editActions);
    section.append(editForm);

    // Status message container
    const statusBox = document.createElement("div");
    statusBox.className = "translation-status-message muted";
    statusBox.setAttribute("role", "status");
    statusBox.setAttribute("aria-live", "polite");
    statusBox.hidden = true;
    section.append(statusBox);

    // Error alert container
    const errorBox = document.createElement("div");
    errorBox.className = "callout error translation-error-message";
    errorBox.setAttribute("role", "alert");
    errorBox.setAttribute("aria-live", "polite");
    errorBox.hidden = true;
    section.append(errorBox);

    function showError(err, action = "translate") {
      const msg =
        action === "validation"
          ? String(err?.message || err || "")
          : getTranslationErrorMessage(err, action);
      errorBox.textContent = msg;
      errorBox.hidden = false;
      notify(msg, true);
    }

    function clearError() {
      errorBox.textContent = "";
      errorBox.hidden = true;
    }

    function showStatus(message) {
      statusBox.textContent = message;
      statusBox.hidden = !message;
    }

    function clearStatus() {
      statusBox.textContent = "";
      statusBox.hidden = true;
    }

    function updateControls() {
      translateBtn.textContent = `Oversett til ${formatLanguageName(targetLocale)}`;
      editBtn.setAttribute("aria-expanded", String(isEditingTranslation));

      if (isEditingTranslation) {
        statusIndicator.textContent = `Redigerer oversettelse (${formatLanguageName(targetLocale)})`;
        editForm.hidden = false;
        localeSelect.hidden = true;
        translateBtn.hidden = true;
        viewOriginalBtn.hidden = true;
        viewTranslationBtn.hidden = true;
        refreshBtn.hidden = true;
        editBtn.hidden = true;
        metaContainer.hidden = true;
        return;
      }

      editForm.hidden = true;

      if (isViewingTranslation) {
        statusIndicator.textContent = `Viser oversettelse (${formatLanguageName(currentTranslation?.target_locale || targetLocale)})`;
        viewOriginalBtn.hidden = false;
        viewTranslationBtn.hidden = true;
        translateBtn.hidden = true;
        localeSelect.hidden = true;
        editBtn.hidden = false;
        metaContainer.hidden = false;

        renderMeta();
      } else {
        statusIndicator.textContent = "Viser original";
        viewOriginalBtn.hidden = true;
        refreshBtn.hidden = true;
        localeSelect.hidden = false;
        editBtn.hidden = false;
        metaContainer.hidden = true;

        if (currentTranslation && currentTranslation.target_locale === targetLocale) {
          viewTranslationBtn.hidden = false;
          viewTranslationBtn.textContent = `Vis oversettelse (${formatLanguageName(targetLocale)})`;
          translateBtn.hidden = true;
        } else {
          viewTranslationBtn.hidden = true;
          translateBtn.hidden = false;
        }
      }
    }

    function renderMeta() {
      metaContainer.replaceChildren();
      if (!currentTranslation) return;

      const infoText = document.createElement("p");
      infoText.className = "muted translation-details";
      const parts = [];
      if (currentTranslation.provider === "manual") {
        parts.push("Manuell oversettelse");
      } else {
        if (currentTranslation.provider) {
          parts.push(`Leverandør: ${currentTranslation.provider}`);
        }
        if (currentTranslation.model) {
          parts.push(`Modell: ${currentTranslation.model}`);
        }
      }
      if (currentTranslation.recipe_version) {
        parts.push(`Versjon: ${currentTranslation.recipe_version.slice(0, 8)}`);
      }
      if (currentTranslation.created_at) {
        try {
          const dt = new Date(currentTranslation.created_at);
          parts.push(`Dato: ${dt.toLocaleDateString(intlLocale)}`);
        } catch {}
      }
      infoText.textContent = parts.join(" · ");
      metaContainer.append(infoText);

      if (currentTranslation.is_stale) {
        const staleNotice = document.createElement("div");
        staleNotice.className = "callout warning translation-stale-notice";
        staleNotice.setAttribute("role", "note");
        staleNotice.textContent =
          "Oppskriften har blitt endret siden oversettelsen ble laget. Innholdet kan være utdatert.";
        metaContainer.append(staleNotice);
        refreshBtn.hidden = false;
      } else {
        refreshBtn.hidden = true;
      }
    }

    function showOriginal() {
      clearError();
      clearStatus();
      isEditingTranslation = false;
      isViewingTranslation = false;
      updateControls();
      onContentChange({
        title: recipe.title,
        ingredients: recipe.ingredients,
        instructions: recipe.instructions,
        isTranslated: false,
        translation: null,
      });
    }

    function showTranslation(translation) {
      clearError();
      if (translation?.target_locale) {
        const loc = String(translation.target_locale).trim().toLowerCase();
        targetLocale = loc;
        ensureLocaleOption(loc);
        localeSelect.value = loc;
        translationsByLocale.set(loc, translation);
      }
      currentTranslation = translation;
      isEditingTranslation = false;
      isViewingTranslation = true;
      updateControls();
      onContentChange({
        title: translation.title,
        ingredients: translation.ingredients,
        instructions: translation.instructions,
        isTranslated: true,
        translation: translation,
      });
    }

    function openEditForm() {
      clearError();
      clearStatus();
      const activeLocale =
        isViewingTranslation && currentTranslation?.target_locale
          ? String(currentTranslation.target_locale).trim().toLowerCase()
          : targetLocale;
      targetLocale = activeLocale;
      const existingForLocale =
        currentTranslation && currentTranslation.target_locale === activeLocale
          ? currentTranslation
          : translationsByLocale.get(activeLocale);
      const sourceData = existingForLocale || recipe;

      editHeading.textContent = `Rediger oversettelse (${formatLanguageName(activeLocale)})`;
      titleField.input.value = sourceData.title || "";
      instructionsField.input.value = Array.isArray(sourceData.instructions)
        ? sourceData.instructions.join("\n")
        : "";
      const sourceIngredients = Array.isArray(sourceData.ingredients) ? sourceData.ingredients : [];
      editIngredientRows = (sourceIngredients.length ? sourceIngredients : [{}]).map((item) => ({
        name: item?.name == null ? "" : String(item.name),
        quantity: item?.quantity == null ? "" : String(item.quantity),
        unit: item?.unit == null ? "" : String(item.unit),
        category: item?.category == null ? "Other" : String(item.category),
        source_indices: Array.isArray(item?.source_indices) ? [...item.source_indices] : [],
        purchase_estimate:
          item?.purchase_estimate && typeof item.purchase_estimate === "object"
            ? { ...item.purchase_estimate }
            : null,
      }));
      renderEditIngredientRows();

      isEditingTranslation = true;
      updateControls();
      titleField.input.focus?.();
    }

    function cancelEdit() {
      clearError();
      isEditingTranslation = false;
      updateControls();
      editBtn.focus?.();
    }

    async function handleSaveEdit() {
      if (isSavingEdit) return;
      clearError();
      clearStatus();

      let payload;
      try {
        // Read latest DOM input values in case tests mutate input.value directly without input events
        const rowEls = ingredientRowsContainer.querySelectorAll(".translation-edit-ingredient-row");
        const ingredients = editIngredientRows.map((rowState, index) => {
          const rowEl = rowEls[index];
          const nameInput = rowEl?.querySelector(".translation-ingredient-name");
          const quantityInput = rowEl?.querySelector(".translation-ingredient-quantity");
          const unitInput = rowEl?.querySelector(".translation-ingredient-unit");
          return {
            name: nameInput ? nameInput.value : rowState.name,
            quantity: quantityInput ? quantityInput.value : rowState.quantity,
            unit: unitInput ? unitInput.value : rowState.unit,
            category: rowState.category,
            source_indices: rowState.source_indices,
            ...(rowState.purchase_estimate ? { purchase_estimate: rowState.purchase_estimate } : {}),
          };
        });
        payload = buildManualTranslationPayload({
          title: titleField.input.value,
          ingredients,
          instructions: instructionsField.input.value,
        });
      } catch (validationErr) {
        showError(validationErr, "validation");
        return;
      }

      isSavingEdit = true;
      saveEditBtn.disabled = true;
      cancelEditBtn.disabled = true;
      saveEditBtn.setAttribute("aria-busy", "true");
      const prevText = saveEditBtn.textContent;
      saveEditBtn.textContent = "Lagrer oversettelse …";

      try {
        const saved = await saveManualTranslation(recipe.id, targetLocale, payload, { api });
        showTranslation(saved);
        showStatus("Oversettelsen er lagret.");
        notify("Oversettelsen er lagret.");
      } catch (err) {
        showError(err, "save");
      } finally {
        isSavingEdit = false;
        saveEditBtn.disabled = false;
        cancelEditBtn.disabled = false;
        saveEditBtn.removeAttribute("aria-busy");
        saveEditBtn.textContent = prevText;
      }
    }

    async function handleTranslate(force = false) {
      if (isLoading) return;
      isLoading = true;
      clearError();
      clearStatus();

      translateBtn.disabled = true;
      refreshBtn.disabled = true;
      translateBtn.setAttribute("aria-busy", "true");
      const prevText = translateBtn.textContent;
      translateBtn.textContent = "Oversetter oppskrift …";

      try {
        const result = await requestTranslation(recipe.id, targetLocale, { force, api });
        showTranslation(result);
      } catch (err) {
        showError(err, "translate");
      } finally {
        isLoading = false;
        translateBtn.disabled = false;
        refreshBtn.disabled = false;
        translateBtn.removeAttribute("aria-busy");
        translateBtn.textContent = prevText;
      }
    }

    translateBtn.addEventListener("click", () => handleTranslate(false));
    refreshBtn.addEventListener("click", () => handleTranslate(true));
    viewOriginalBtn.addEventListener("click", showOriginal);
    viewTranslationBtn.addEventListener("click", () => {
      if (currentTranslation) {
        showTranslation(currentTranslation);
      }
    });
    editBtn.addEventListener("click", openEditForm);
    cancelEditBtn.addEventListener("click", cancelEdit);
    saveEditBtn.addEventListener("click", handleSaveEdit);
    editForm.addEventListener("submit", (e) => {
      e.preventDefault();
      handleSaveEdit();
    });
    editForm.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && isEditingTranslation) {
        e.preventDefault();
        e.stopPropagation?.();
        cancelEdit();
      }
    });

    // Check if a saved translation already exists in the background
    if (api && recipe.id) {
      fetchTranslationsList(recipe.id, api)
        .then((list) => {
          if (Array.isArray(list) && list.length > 0) {
            list.forEach((t) => {
              if (t && t.target_locale) {
                const loc = String(t.target_locale).trim().toLowerCase();
                translationsByLocale.set(loc, t);
                ensureLocaleOption(loc);
              }
            });
            const matching = translationsByLocale.get(targetLocale);
            if (matching) {
              currentTranslation = matching;
              updateControls();
            }
          }
        })
        .catch(() => {});
    }

    updateControls();
    container.append(section);

    return {
      section,
      showOriginal,
      showTranslation,
      openEditForm,
      cancelEdit,
      saveEdit: handleSaveEdit,
      getCurrentTranslation: () => currentTranslation,
      isViewingTranslation: () => isViewingTranslation,
      isEditingTranslation: () => isEditingTranslation,
      getTargetLocale: () => targetLocale,
    };
  }

  root.KitchenRecipeTranslationsView = {
    LANGUAGE_NAMES,
    TARGET_LOCALES,
    formatLanguageName,
    renderLanguageBadge,
    getTranslationErrorMessage,
    normalizeTranslationQuantity,
    buildManualTranslationPayload,
    fetchTranslation,
    fetchTranslationsList,
    requestTranslation,
    saveManualTranslation,
    attachRecipeTranslation,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeTranslationsView;
  }
})(typeof window === "undefined" ? globalThis : window);
