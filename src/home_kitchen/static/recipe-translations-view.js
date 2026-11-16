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

  function getTranslationErrorMessage(error) {
    const msg = String(error?.message || error || "");
    const status = error?.status || (error?.response && error.response.status);
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

  function attachRecipeTranslation(container, recipe, options = {}) {
    if (!container || !recipe) return null;

    const api = options.api;
    const notify = options.notify || (() => {});
    const onContentChange = options.onContentChange || (() => {});
    const householdLocale = options.locale || "nb";

    let currentTranslation = null;
    let isViewingTranslation = false;
    let isLoading = false;

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

    const nbOption = document.createElement("option");
    nbOption.value = "nb";
    nbOption.textContent = "Norsk (bokmål)";

    const enOption = document.createElement("option");
    enOption.value = "en";
    enOption.textContent = "Engelsk";

    localeSelect.append(nbOption, enOption);
    localeSelect.value = targetLocale;
    localeSelect.addEventListener("change", (e) => {
      targetLocale = e.target.value;
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

    section.append(controlsRow);

    // Meta details container (shows provider, model, version, date)
    const metaContainer = document.createElement("div");
    metaContainer.className = "recipe-translation-meta";
    metaContainer.hidden = true;
    section.append(metaContainer);

    // Error alert container
    const errorBox = document.createElement("div");
    errorBox.className = "callout error translation-error-message";
    errorBox.setAttribute("role", "alert");
    errorBox.setAttribute("aria-live", "polite");
    errorBox.hidden = true;
    section.append(errorBox);

    function showError(err) {
      const msg = getTranslationErrorMessage(err);
      errorBox.textContent = msg;
      errorBox.hidden = false;
      notify(msg, true);
    }

    function clearError() {
      errorBox.textContent = "";
      errorBox.hidden = true;
    }

    function updateControls() {
      translateBtn.textContent = `Oversett til ${formatLanguageName(targetLocale)}`;

      if (isViewingTranslation) {
        statusIndicator.textContent = `Viser oversettelse (${formatLanguageName(currentTranslation?.target_locale || targetLocale)})`;
        viewOriginalBtn.hidden = false;
        viewTranslationBtn.hidden = true;
        translateBtn.hidden = true;
        localeSelect.hidden = true;
        metaContainer.hidden = false;

        renderMeta();
      } else {
        statusIndicator.textContent = "Viser original";
        viewOriginalBtn.hidden = true;
        localeSelect.hidden = false;
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
      if (currentTranslation.provider) {
        parts.push(`Leverandør: ${currentTranslation.provider}`);
      }
      if (currentTranslation.model) {
        parts.push(`Modell: ${currentTranslation.model}`);
      }
      if (currentTranslation.recipe_version) {
        parts.push(`Versjon: ${currentTranslation.recipe_version.slice(0, 8)}`);
      }
      if (currentTranslation.created_at) {
        try {
          const dt = new Date(currentTranslation.created_at);
          parts.push(`Dato: ${dt.toLocaleDateString("nb-NO")}`);
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
      currentTranslation = translation;
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

    async function handleTranslate(force = false) {
      if (isLoading) return;
      isLoading = true;
      clearError();

      translateBtn.disabled = true;
      refreshBtn.disabled = true;
      translateBtn.setAttribute("aria-busy", "true");
      const prevText = translateBtn.textContent;
      translateBtn.textContent = "Oversetter oppskrift …";

      try {
        const result = await requestTranslation(recipe.id, targetLocale, { force, api });
        showTranslation(result);
      } catch (err) {
        showError(err);
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

    // Check if a saved translation already exists in the background
    if (api && recipe.id) {
      fetchTranslationsList(recipe.id, api)
        .then((list) => {
          if (Array.isArray(list) && list.length > 0) {
            const matching = list.find((t) => t.target_locale === targetLocale);
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
      getCurrentTranslation: () => currentTranslation,
      isViewingTranslation: () => isViewingTranslation,
      getTargetLocale: () => targetLocale,
    };
  }

  root.KitchenRecipeTranslationsView = {
    LANGUAGE_NAMES,
    formatLanguageName,
    renderLanguageBadge,
    getTranslationErrorMessage,
    fetchTranslation,
    fetchTranslationsList,
    requestTranslation,
    attachRecipeTranslation,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeTranslationsView;
  }
})(typeof window === "undefined" ? globalThis : window);
