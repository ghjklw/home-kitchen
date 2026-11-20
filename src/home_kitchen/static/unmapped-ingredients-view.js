(function (root) {
  "use strict";

  function formatIngredientSourceText(item) {
    if (!item) return "";
    const parts = [];
    if (item.quantity != null && String(item.quantity).trim() !== "") {
      parts.push(String(item.quantity));
    }
    if (item.unit && String(item.unit).trim() !== "") {
      parts.push(String(item.unit).trim());
    }
    if (item.name && String(item.name).trim() !== "") {
      parts.push(String(item.name).trim());
    }
    return parts.join(" ");
  }

  function ingredientNeeds(item) {
    if (!item) return { needsProduct: false, needsUnit: false, isUnmapped: false };
    const needsProduct = !item.canonical_product_id;
    const hasUnit = Boolean(item.unit && String(item.unit).trim() !== "");
    const needsUnit = hasUnit && !item.canonical_unit_id;
    return {
      needsProduct,
      needsUnit,
      isUnmapped: needsProduct || needsUnit,
    };
  }

  function mappingStatusLabel(needs) {
    if (needs.needsProduct && needs.needsUnit) return "Mangler råvare og måleenhet";
    if (needs.needsProduct) return "Mangler kanonisk råvare";
    if (needs.needsUnit) return "Mangler kanonisk måleenhet";
    return "Ferdig koblet";
  }

  function normalizeAliasSearchResponse(data) {
    if (!data || typeof data !== "object") {
      return { status: "unmapped", candidates: [] };
    }
    const status = ["resolved", "ambiguous", "unmapped"].includes(data.status)
      ? data.status
      : "unmapped";
    const candidates = Array.isArray(data.candidates)
      ? data.candidates.filter(
          (c) => c && typeof c.id === "string" && typeof c.canonical_name === "string",
        )
      : [];
    return { status, candidates };
  }

  function buildMapIngredientPayload({
    recipe_id,
    ingredient_index,
    canonical_product_id = null,
    canonical_unit_id = null,
    add_alias = true,
    locale = "nb",
    context = "recipe",
  }) {
    if (!recipe_id || typeof recipe_id !== "string") {
      throw new Error("Oppskrifts-ID er påkrevd.");
    }
    if (!Number.isInteger(ingredient_index) || ingredient_index < 0) {
      throw new Error("Ugyldig ingrediensindeks.");
    }
    if (!canonical_product_id && !canonical_unit_id) {
      throw new Error("Minst én kanonisk råvare eller måleenhet må velges.");
    }
    return {
      recipe_id,
      ingredient_index,
      canonical_product_id: canonical_product_id || null,
      canonical_unit_id: canonical_unit_id || null,
      add_alias: Boolean(add_alias),
      locale: String(locale || "nb"),
      context: String(context || "recipe"),
    };
  }

  function buildCreateProductPayload(name, variant = "") {
    const canonical_name = String(name || "").trim();
    if (!canonical_name) {
      throw new Error("Råvarenavn kan ikke være tomt.");
    }
    return {
      canonical_name,
      variant: String(variant || "").trim(),
    };
  }

  function buildCreateUnitPayload(name) {
    const canonical_name = String(name || "").trim();
    if (!canonical_name) {
      throw new Error("Måleenhetsnavn kan ikke være tomt.");
    }
    return {
      canonical_name,
    };
  }

  function formatAutoMapSummary(result) {
    const mapped = Number(result?.mapped_occurrences || 0);
    const processed = Number(result?.processed_unique || 0);
    const createdProducts = Number(result?.created_products || 0);
    const createdAliases = Number(result?.created_aliases || 0);
    const remaining = Number(result?.remaining_unmapped || 0);
    if (processed === 0 && mapped === 0) {
      return remaining > 0
        ? `Ingen ingredienser ble kartlagt automatisk (${remaining} gjenstår).`
        : "Ingen ukartlagte ingredienser å kartlegge.";
    }
    return (
      `KI-kartlegging fullført: ${mapped} forekomster koblet (${processed} unike), ` +
      `${createdProducts} nye råvarer, ${createdAliases} nye synonymer, ` +
      `${remaining} gjenstår.`
    );
  }

  function renderReviewDialog(root, options = {}) {
    const doc = options.document || root.ownerDocument || (typeof document !== "undefined" ? document : null);
    if (!doc) throw new Error("Document is required to render UI");

    const {
      api,
      recipeId = null,
      locale = "nb",
      notify = () => {},
      onMapped = () => {},
      onClose = () => {},
    } = options;

    function el(tag, text = "", className = "") {
      const node = doc.createElement(tag);
      if (text) node.textContent = text;
      if (className) node.className = className;
      return node;
    }

    function add(parent, ...children) {
      children.filter(Boolean).forEach((c) => parent.append(c));
      return parent;
    }

    function button(text, action, className = "", ariaLabel = "") {
      const btn = el("button", text, className);
      btn.type = "button";
      if (ariaLabel) btn.setAttribute("aria-label", ariaLabel);
      if (action) btn.addEventListener("click", action);
      return btn;
    }

    root.replaceChildren();

    const container = el("div", null, "unmapped-ingredients-review");
    add(root, container);

    const lead = el(
      "p",
      "Koble oppskriftsingredienser til felles råvarer og måleenheter. Den opprinnelige oppskriftsteksten beholdes uendret.",
      "muted unmapped-lead",
    );

    const statusNode = el("p", "", "unmapped-status");
    statusNode.setAttribute("role", "status");
    statusNode.setAttribute("aria-live", "polite");

    const errorNode = el("p", "", "form-error unmapped-error");
    errorNode.setAttribute("role", "alert");
    errorNode.hidden = true;

    const toolbar = el("div", null, "toolbar unmapped-toolbar");
    const recipeFilter = el("select", null, "unmapped-filter");
    recipeFilter.setAttribute("aria-label", "Filtrer etter oppskrift");
    const defaultOption = el("option", "Alle oppskrifter");
    defaultOption.value = "";
    recipeFilter.append(defaultOption);

    let currentFilter = recipeId || "";
    recipeFilter.addEventListener("change", () => {
      currentFilter = recipeFilter.value;
      loadData();
    });

    let autoMapRunning = false;
    const autoMapBtn = button(
      "Kartlegg automatisk med KI",
      () => runAutoMap(),
      "secondary unmapped-automap-btn",
      "Kartlegg ukartlagte ingredienser automatisk med KI",
    );
    const refreshBtn = button("Oppdater", () => loadData(), "quiet", "Oppdater liste over ukartlagte ingredienser");
    add(toolbar, recipeFilter, autoMapBtn, refreshBtn);

    const contentArea = el("div", null, "unmapped-content");
    add(container, lead, statusNode, errorNode, toolbar, contentArea);

    let unmappedItems = [];

    async function runAutoMap() {
      if (autoMapRunning) return;
      autoMapRunning = true;
      errorNode.hidden = true;
      errorNode.textContent = "";
      autoMapBtn.disabled = true;
      autoMapBtn.textContent = "Kartlegger med KI …";
      statusNode.textContent = "Kartlegger ukartlagte ingredienser automatisk med KI …";

      try {
        const payload = { batch_size: 30, max_batches: 3 };
        if (currentFilter) {
          payload.recipe_id = currentFilter;
        }
        const summary = await api("/catalogue/unmapped-ingredients/auto-map", "POST", payload);
        const summaryText = formatAutoMapSummary(summary);
        await loadData();
        statusNode.textContent = summaryText;
        notify(summaryText);
        if (Number(summary?.mapped_occurrences || 0) > 0) {
          onMapped(summary);
        }
      } catch (err) {
        statusNode.textContent = "";
        errorNode.hidden = false;
        errorNode.textContent = "KI-kartlegging feilet: " + (err.message || String(err));
      } finally {
        autoMapRunning = false;
        autoMapBtn.textContent = "Kartlegg automatisk med KI";
        autoMapBtn.disabled = unmappedItems.length === 0;
      }
    }

    async function loadData() {
      errorNode.hidden = true;
      errorNode.textContent = "";
      statusNode.textContent = "Laster inn ukartlagte ingredienser …";
      contentArea.replaceChildren();

      try {
        const query = currentFilter ? "?recipe_id=" + encodeURIComponent(currentFilter) : "";
        const data = await api("/catalogue/unmapped-ingredients" + query);
        unmappedItems = Array.isArray(data) ? [...data] : [];
        statusNode.textContent = "";

        if (!recipeId) {
          updateFilterOptions(unmappedItems);
        }
        renderList();
      } catch (err) {
        statusNode.textContent = "";
        errorNode.hidden = false;
        errorNode.textContent = "Kunne ikke laste inn ukartlagte ingredienser: " + (err.message || String(err));
        const retryBtn = button("Prøv igjen", () => loadData(), "quiet");
        contentArea.replaceChildren(retryBtn);
      }
    }

    function updateFilterOptions(items) {
      const existingVal = recipeFilter.value;
      recipeFilter.replaceChildren();

      const allOpt = el("option", "Alle oppskrifter" + (items.length ? " (" + items.length + ")" : ""));
      allOpt.value = "";
      recipeFilter.append(allOpt);

      const recipesMap = new Map();
      items.forEach((item) => {
        if (item.recipe_id && item.recipe_title) {
          const count = (recipesMap.get(item.recipe_id)?.count || 0) + 1;
          recipesMap.set(item.recipe_id, { title: item.recipe_title, count });
        }
      });

      for (const [id, info] of recipesMap) {
        const opt = el("option", `${info.title} (${info.count})`);
        opt.value = id;
        recipeFilter.append(opt);
      }

      if (recipesMap.has(existingVal) || existingVal === "") {
        recipeFilter.value = existingVal;
      }
    }

    function renderList() {
      contentArea.replaceChildren();
      if (!autoMapRunning) {
        autoMapBtn.disabled = unmappedItems.length === 0;
      }

      if (unmappedItems.length === 0) {
        const emptyDiv = el("div", null, "empty unmapped-empty");
        add(
          emptyDiv,
          el("h3", "Ingen ukartlagte ingredienser funnet"),
          el("p", "Alle oppskriftsingredienser har en koblet råvare og måleenhet.", "muted"),
        );
        contentArea.append(emptyDiv);
        return;
      }

      const list = el("div", null, "unmapped-ingredients-list");
      unmappedItems.forEach((item) => {
        list.append(renderCard(item));
      });
      contentArea.append(list);
    }

    function renderCard(item) {
      const card = el("article", null, "card unmapped-ingredient-card");
      const needs = ingredientNeeds(item);

      const cardHeader = el("div", null, "unmapped-card-header");
      const recipeBadge = el("span", "Oppskrift: " + item.recipe_title, "badge unmapped-recipe-badge");
      const needsBadge = el("span", mappingStatusLabel(needs), "badge " + (needs.isUnmapped ? "warning" : "success"));
      add(cardHeader, recipeBadge, needsBadge);

      const sourceHeading = el("h3", formatIngredientSourceText(item), "unmapped-source-text");
      add(card, cardHeader, sourceHeading);

      if (item.source_unit_expression) {
        add(card, el("p", "Opprinnelig uttrykk: " + item.source_unit_expression, "muted unmapped-unit-expr"));
      }

      let selectedProduct = item.canonical_product_id
        ? { id: item.canonical_product_id, canonical_name: item.name }
        : null;
      let selectedUnit = item.canonical_unit_id
        ? { id: item.canonical_unit_id, canonical_name: item.unit }
        : null;

      const sectionsWrap = el("div", null, "unmapped-sections");

      // Product Section
      const productSection = el("section", null, "unmapped-section unmapped-product-section");
      productSection.setAttribute("aria-label", "Kanonisk råvare for " + item.name);
      add(productSection, el("h4", "Kanonisk råvare"));

      const productSelectionInfo = el("p", "", "unmapped-selection-info");
      productSelectionInfo.setAttribute("role", "status");
      productSelectionInfo.setAttribute("aria-live", "polite");

      function updateProductSelectionDisplay() {
        if (selectedProduct) {
          productSelectionInfo.textContent = "Valgt råvare: " + selectedProduct.canonical_name +
            (selectedProduct.variant ? " (" + selectedProduct.variant + ")" : "");
          productSelectionInfo.hidden = false;
        } else {
          productSelectionInfo.textContent = "";
          productSelectionInfo.hidden = true;
        }
        updateMapButtonState();
      }

      if (item.canonical_product_id) {
        add(productSection, el("p", "✓ Kanonisk råvare er allerede koblet.", "unmapped-already-mapped"));
      } else {
        const searchRow = el("div", null, "unmapped-search-row");
        const searchInput = el("input", null, "unmapped-search-input");
        searchInput.type = "search";
        searchInput.value = item.name;
        searchInput.setAttribute("aria-label", "Søk etter synonym eller kanonisk vare for " + item.name);

        const searchResults = el("div", null, "unmapped-search-results");
        searchResults.setAttribute("role", "region");
        searchResults.setAttribute("aria-label", "Søkeresultater for råvare");
        searchResults.setAttribute("aria-live", "polite");

        const searchBtn = button("Søk etter råvare", async () => {
          const query = searchInput.value.trim();
          if (!query) return;
          searchResults.textContent = "Søker etter «" + query + "» …";
          try {
            const res = normalizeAliasSearchResponse(
              await api(
                "/catalogue/aliases/product?alias=" +
                  encodeURIComponent(query) +
                  "&locale=" +
                  encodeURIComponent(locale) +
                  "&context=recipe",
              ),
            );
            searchResults.replaceChildren();
            if (res.candidates.length === 0) {
              searchResults.append(el("p", "Fant ingen eksisterende råvare med dette navnet.", "muted"));
            } else {
              const candidateList = el("div", null, "unmapped-candidate-list");
              const candidateBtns = [];
              res.candidates.forEach((cand) => {
                const label = cand.canonical_name + (cand.variant ? " (" + cand.variant + ")" : "");
                const chooseBtn = button(
                  "Velg: " + label,
                  () => {
                    selectedProduct = cand;
                    candidateBtns.forEach((b) =>
                      b.setAttribute("aria-pressed", b === chooseBtn ? "true" : "false"),
                    );
                    updateProductSelectionDisplay();
                  },
                  "quiet unmapped-candidate-btn",
                  "Velg " + label + " som kanonisk råvare",
                );
                chooseBtn.setAttribute(
                  "aria-pressed",
                  selectedProduct?.id === cand.id ? "true" : "false",
                );
                candidateBtns.push(chooseBtn);
                candidateList.append(chooseBtn);
              });
              searchResults.append(candidateList);
            }
          } catch (err) {
            searchResults.textContent = "Søk mislyktes: " + (err.message || String(err));
          }
        }, "quiet", "Søk etter råvare for " + item.name);

        searchInput.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            searchBtn.click();
          }
        });

        add(searchRow, searchInput, searchBtn);

        const createDetails = el("details", null, "unmapped-create-details");
        const summary = el("summary", "Opprett ny kanonisk råvare");
        const createBody = el("div", null, "unmapped-create-body");
        const nameInput = el("input", null, "unmapped-create-name");
        nameInput.type = "text";
        nameInput.value = item.name;
        nameInput.setAttribute("aria-label", "Navn på ny kanonisk råvare");

        const variantInput = el("input", null, "unmapped-create-variant");
        variantInput.type = "text";
        variantInput.placeholder = "Variant (f.eks. hermetisk, tørket, hel)";
        variantInput.setAttribute("aria-label", "Variant for ny råvare (valgfritt)");

        const createStatus = el("p", "", "form-error");
        createStatus.setAttribute("role", "alert");
        createStatus.hidden = true;

        const createBtn = button("Opprett og velg råvare", async () => {
          createStatus.hidden = true;
          try {
            const payload = buildCreateProductPayload(nameInput.value, variantInput.value);
            const created = await api("/catalogue/identities/products", "POST", payload);
            selectedProduct = created;
            updateProductSelectionDisplay();
            createDetails.open = false;
          } catch (err) {
            createStatus.hidden = false;
            createStatus.textContent = "Kunne ikke opprette råvare: " + (err.message || String(err));
          }
        }, "quiet", "Opprett og velg ny kanonisk råvare");

        add(createBody, nameInput, variantInput, createBtn, createStatus);
        add(createDetails, summary, createBody);

        add(productSection, searchRow, searchResults, createDetails, productSelectionInfo);
      }
      add(sectionsWrap, productSection);

      // Unit Section
      const hasUnit = Boolean(item.unit && String(item.unit).trim() !== "");
      let unitSelectionInfo = null;

      function updateUnitSelectionDisplay() {
        if (!unitSelectionInfo) return;
        if (selectedUnit) {
          unitSelectionInfo.textContent = "Valgt måleenhet: " + selectedUnit.canonical_name;
          unitSelectionInfo.hidden = false;
        } else {
          unitSelectionInfo.textContent = "";
          unitSelectionInfo.hidden = true;
        }
        updateMapButtonState();
      }

      if (hasUnit) {
        const unitSection = el("section", null, "unmapped-section unmapped-unit-section");
        unitSection.setAttribute("aria-label", "Kanonisk måleenhet for " + item.unit);
        add(unitSection, el("h4", "Kanonisk måleenhet"));

        unitSelectionInfo = el("p", "", "unmapped-selection-info");
        unitSelectionInfo.setAttribute("role", "status");
        unitSelectionInfo.setAttribute("aria-live", "polite");

        if (item.canonical_unit_id) {
          add(unitSection, el("p", "✓ Kanonisk måleenhet er allerede koblet.", "unmapped-already-mapped"));
        } else {
          const unitSearchRow = el("div", null, "unmapped-search-row");
          const unitSearchInput = el("input", null, "unmapped-search-input");
          unitSearchInput.type = "search";
          unitSearchInput.value = item.unit;
          unitSearchInput.setAttribute("aria-label", "Søk etter måleenhet for " + item.unit);

          const unitSearchResults = el("div", null, "unmapped-search-results");
          unitSearchResults.setAttribute("role", "region");
          unitSearchResults.setAttribute("aria-label", "Søkeresultater for måleenhet");
          unitSearchResults.setAttribute("aria-live", "polite");

          const unitSearchBtn = button("Søk etter måleenhet", async () => {
            const query = unitSearchInput.value.trim();
            if (!query) return;
            unitSearchResults.textContent = "Søker etter «" + query + "» …";
            try {
              const res = normalizeAliasSearchResponse(
                await api(
                  "/catalogue/aliases/unit?alias=" +
                    encodeURIComponent(query) +
                    "&locale=" +
                    encodeURIComponent(locale) +
                    "&context=recipe",
                ),
              );
              unitSearchResults.replaceChildren();
              if (res.candidates.length === 0) {
                unitSearchResults.append(el("p", "Fant ingen eksisterende måleenhet med dette navnet.", "muted"));
              } else {
                const candidateList = el("div", null, "unmapped-candidate-list");
                const unitCandidateBtns = [];
                res.candidates.forEach((cand) => {
                  const chooseBtn = button(
                    "Velg: " + cand.canonical_name,
                    () => {
                      selectedUnit = cand;
                      unitCandidateBtns.forEach((b) =>
                        b.setAttribute("aria-pressed", b === chooseBtn ? "true" : "false"),
                      );
                      updateUnitSelectionDisplay();
                    },
                    "quiet unmapped-candidate-btn",
                    "Velg " + cand.canonical_name + " som kanonisk måleenhet",
                  );
                  chooseBtn.setAttribute(
                    "aria-pressed",
                    selectedUnit?.id === cand.id ? "true" : "false",
                  );
                  unitCandidateBtns.push(chooseBtn);
                  candidateList.append(chooseBtn);
                });
                unitSearchResults.append(candidateList);
              }
            } catch (err) {
              unitSearchResults.textContent = "Søk mislyktes: " + (err.message || String(err));
            }
          }, "quiet", "Søk etter måleenhet for " + item.unit);

          unitSearchInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              unitSearchBtn.click();
            }
          });

          add(unitSearchRow, unitSearchInput, unitSearchBtn);

          const createUnitDetails = el("details", null, "unmapped-create-details");
          const unitSummary = el("summary", "Opprett ny kanonisk måleenhet");
          const createUnitBody = el("div", null, "unmapped-create-body");
          const unitNameInput = el("input", null, "unmapped-create-name");
          unitNameInput.type = "text";
          unitNameInput.value = item.unit;
          unitNameInput.setAttribute("aria-label", "Navn på ny kanonisk måleenhet");

          const createUnitStatus = el("p", "", "form-error");
          createUnitStatus.setAttribute("role", "alert");
          createUnitStatus.hidden = true;

          const createUnitBtn = button("Opprett og velg måleenhet", async () => {
            createUnitStatus.hidden = true;
            try {
              const payload = buildCreateUnitPayload(unitNameInput.value);
              const created = await api("/catalogue/identities/units", "POST", payload);
              selectedUnit = created;
              updateUnitSelectionDisplay();
              createUnitDetails.open = false;
            } catch (err) {
              createUnitStatus.hidden = false;
              createUnitStatus.textContent = "Kunne ikke opprette måleenhet: " + (err.message || String(err));
            }
          }, "quiet", "Opprett og velg ny kanonisk måleenhet");

          add(createUnitBody, unitNameInput, createUnitBtn, createUnitStatus);
          add(createUnitDetails, unitSummary, createUnitBody);

          add(unitSection, unitSearchRow, unitSearchResults, createUnitDetails, unitSelectionInfo);
        }
        add(sectionsWrap, unitSection);
      }

      add(card, sectionsWrap);

      const aliasLabel = el("label", null, "unmapped-alias-label");
      const aliasCheckbox = el("input");
      aliasCheckbox.type = "checkbox";
      aliasCheckbox.checked = true;
      aliasCheckbox.setAttribute("aria-label", "Lagre som synonym for framtidige oppskrifter");
      add(aliasLabel, aliasCheckbox, doc.createTextNode(" Lagre som synonym for framtidige oppskrifter"));
      add(card, aliasLabel);

      const cardError = el("p", "", "form-error unmapped-card-error");
      cardError.setAttribute("role", "alert");
      cardError.hidden = true;
      add(card, cardError);

      const actions = el("div", null, "actions unmapped-actions");
      const mapBtn = button(
        "Koble ingrediens",
        async () => {
          cardError.hidden = true;
          cardError.textContent = "";
          mapBtn.disabled = true;
          mapBtn.textContent = "Kobler ingrediens …";

          try {
            const prodId = selectedProduct?.id || item.canonical_product_id || null;
            const unitId = selectedUnit?.id || item.canonical_unit_id || null;
            const payload = buildMapIngredientPayload({
              recipe_id: item.recipe_id,
              ingredient_index: item.ingredient_index,
              canonical_product_id: prodId,
              canonical_unit_id: unitId,
              add_alias: aliasCheckbox.checked,
              locale,
              context: "recipe",
            });

            const updatedRecipe = await api("/catalogue/unmapped-ingredients/map", "POST", payload);
            notify("Ingrediensen «" + item.name + "» ble koblet.");
            statusNode.textContent = "«" + item.name + "» i «" + item.recipe_title + "» ble koblet.";

            onMapped(updatedRecipe);

            unmappedItems = unmappedItems.filter(
              (i) => !(i.recipe_id === item.recipe_id && i.ingredient_index === item.ingredient_index),
            );
            renderList();
          } catch (err) {
            mapBtn.disabled = false;
            mapBtn.textContent = "Koble ingrediens";
            cardError.hidden = false;
            cardError.textContent = "Kunne ikke koble ingrediens: " + (err.message || String(err));
          }
        },
        "primary unmapped-map-btn",
        "Koble ingrediensen " + item.name + " til valgte kanoniske verdier",
      );

      function updateMapButtonState() {
        const hasProductChoice = Boolean(selectedProduct?.id);
        const hasUnitChoice = Boolean(selectedUnit?.id);
        const canSubmit =
          (hasProductChoice && !item.canonical_product_id) ||
          (hasUnitChoice && !item.canonical_unit_id) ||
          (hasProductChoice && hasUnitChoice);
        mapBtn.disabled = !canSubmit;
      }

      updateMapButtonState();
      add(actions, mapBtn);
      add(card, actions);

      return card;
    }

    const footerActions = el("div", null, "actions unmapped-footer-actions");
    const closeBtn = button("Lukk", onClose, "quiet", "Lukk dialogvindu for ukartlagte ingredienser");
    add(footerActions, closeBtn);
    add(container, footerActions);

    const initialLoad = loadData();

    return {
      reload: loadData,
      ready: initialLoad,
    };
  }

  root.KitchenUnmappedIngredientsView = {
    formatIngredientSourceText,
    ingredientNeeds,
    mappingStatusLabel,
    normalizeAliasSearchResponse,
    buildMapIngredientPayload,
    buildCreateProductPayload,
    buildCreateUnitPayload,
    formatAutoMapSummary,
    renderReviewDialog,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenUnmappedIngredientsView;
  }
})(typeof window === "undefined" ? globalThis : window);
