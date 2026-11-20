(function (root) {
  "use strict";

  function formatStockSummary(stock) {
    if (!stock || !stock.in_stock || Number(stock.total_quantity) <= 0) {
      return {
        inStock: false,
        label: "Ikke på lager (0)",
        provenanceText: "Kilde: Kjøkkenlager (ikke estimert fra kjøp)",
        totalQuantity: 0,
        unit: stock?.unit || "",
      };
    }
    const unitStr = stock.unit ? " " + stock.unit : "";
    return {
      inStock: true,
      label: `På lager: ${stock.total_quantity}${unitStr}`,
      provenanceText: "Kilde: Kjøkkenlager (ikke estimert fra kjøp)",
      totalQuantity: stock.total_quantity,
      unit: stock.unit || "",
    };
  }

  function formatFrequencySummary(frequency) {
    if (!frequency || frequency.status !== "known") {
      return {
        known: false,
        label: "Ukjent frekvens (ikke nok kjøpshistorikk)",
        details: frequency?.frequency_display || "ukjent",
        sampleSize: frequency?.sample_size || 0,
      };
    }
    const sample = frequency.sample_size ? `${frequency.sample_size} kjøp` : "";
    const days = frequency.date_range_days != null ? `over ${frequency.date_range_days} dager` : "";
    const detailParts = [sample, days].filter(Boolean).join(" ");
    return {
      known: true,
      label: frequency.frequency_display || `ca. hver ${frequency.average_interval_days} dager`,
      details: detailParts,
      sampleSize: frequency.sample_size || 0,
    };
  }

  function formatProvenanceCounts(counts) {
    const verified = counts?.provider_verified ?? 0;
    const confirmed = counts?.user_confirmed ?? 0;
    const uncertain = counts?.uncertain ?? 0;
    return {
      provider_verified: verified,
      user_confirmed: confirmed,
      uncertain: uncertain,
      summaryText: `Verifisert butikk/kvittering: ${verified} · Brukerbekreftet: ${confirmed} · Usikker: ${uncertain}`,
    };
  }

  function reorderPreferredSkus(skus, fromIndex, toIndex) {
    if (
      !Array.isArray(skus) ||
      fromIndex < 0 ||
      toIndex < 0 ||
      fromIndex >= skus.length ||
      toIndex >= skus.length ||
      fromIndex === toIndex
    ) {
      return [...(skus || [])];
    }
    const copy = [...skus];
    const [moved] = copy.splice(fromIndex, 1);
    copy.splice(toIndex, 0, moved);
    return copy.map((item, idx) => ({
      ...(typeof item === "string" ? { oda_product_id: item } : item),
      rank: idx + 1,
    }));
  }

  function addPreferredSku(skus, newSku) {
    const list = Array.isArray(skus) ? [...skus] : [];
    const skuObj =
      typeof newSku === "string"
        ? { oda_product_id: newSku.trim() }
        : { ...newSku, oda_product_id: String(newSku?.oda_product_id || "").trim() };
    if (!skuObj.oda_product_id) return list;
    if (list.some((s) => (s.oda_product_id || s) === skuObj.oda_product_id)) return list;
    skuObj.rank = list.length + 1;
    list.push(skuObj);
    return list;
  }

  function removePreferredSku(skus, skuId) {
    if (!Array.isArray(skus)) return [];
    return skus
      .filter((s) => (s.oda_product_id || s) !== skuId)
      .map((item, idx) => ({
        ...(typeof item === "string" ? { oda_product_id: item } : item),
        rank: idx + 1,
      }));
  }

  function addExcludedSku(exclusions, newExclusion) {
    const list = Array.isArray(exclusions) ? [...exclusions] : [];
    const item =
      typeof newExclusion === "string"
        ? { oda_product_id: newExclusion.trim(), reason: "" }
        : {
            ...newExclusion,
            oda_product_id: String(newExclusion?.oda_product_id || "").trim(),
            reason: String(newExclusion?.reason || "").trim(),
          };
    if (!item.oda_product_id) return list;
    if (list.some((e) => (e.oda_product_id || e) === item.oda_product_id)) return list;
    list.push(item);
    return list;
  }

  function removeExcludedSku(exclusions, skuId) {
    if (!Array.isArray(exclusions)) return [];
    return exclusions.filter((e) => (e.oda_product_id || e) !== skuId);
  }

  function buildPreferencesPayload(preferredSkus, excludedSkus) {
    return {
      preferred_oda_skus: (preferredSkus || []).map((s, idx) => {
        if (typeof s === "string") return { oda_product_id: s, rank: idx + 1 };
        return { ...s, rank: idx + 1 };
      }),
      excluded_oda_skus: (excludedSkus || []).map((e) => {
        if (typeof e === "string") return { oda_product_id: e, reason: "" };
        return {
          oda_product_id: e.oda_product_id,
          name: e.name || null,
          reason: e.reason || "",
        };
      }),
    };
  }

  function renderProductCard(product, options = {}) {
    const api = options.api;
    const notify = options.notify || (() => {});
    const onUpdated = options.onUpdated || (() => {});

    let currentProduct = { ...product };

    const card = document.createElement("article");
    card.className = "card product-overview-card";
    card.dataset.productId = currentProduct.id;

    function renderCardContent() {
      card.replaceChildren();

      // Header: Display name, canonical name & variant
      const header = document.createElement("div");
      header.className = "product-card-header";

      const title = document.createElement("h3");
      title.className = "product-display-name";
      title.textContent = currentProduct.display_name || currentProduct.canonical_name;
      header.append(title);

      const subtitle = document.createElement("p");
      subtitle.className = "muted product-canonical-subtitle";
      let nameDesc = `Kanonisk: ${currentProduct.canonical_name}`;
      if (currentProduct.variant) {
        nameDesc += ` · Variant: ${currentProduct.variant}`;
      }
      subtitle.textContent = nameDesc;
      header.append(subtitle);

      card.append(header);

      // Section: Aliases and translations by locale
      const translationsSection = document.createElement("section");
      translationsSection.className = "product-translations-section";
      translationsSection.setAttribute("aria-label", "Oversettelser og kallenavn");

      const transHeading = document.createElement("h4");
      transHeading.textContent = "Språk og kallenavn";
      translationsSection.append(transHeading);

      const translationsList = document.createElement("div");
      translationsList.className = "translations-by-locale-list";

      const aliases = Array.isArray(currentProduct.aliases) ? currentProduct.aliases : [];
      const locales = Object.keys(currentProduct.translations_by_locale || {});
      if (aliases.length > 0) {
        aliases.forEach((aliasObj) => {
          const row = document.createElement("div");
          row.className = "locale-translation-row alias-review-row";

          const locBadge = document.createElement("span");
          locBadge.className = "badge locale-badge";
          locBadge.textContent = (aliasObj.locale || "nb").toUpperCase();
          row.append(locBadge);

          const namesSpan = document.createElement("span");
          namesSpan.className = "translation-names";
          namesSpan.textContent = aliasObj.alias;
          row.append(namesSpan);

          const statusBadge = document.createElement("span");
          statusBadge.className = `badge ${aliasObj.reviewed ? "success" : "warning"} alias-status-badge`;
          statusBadge.textContent = aliasObj.reviewed ? "Bekreftet" : "Ubekreftet / forslag";
          row.append(statusBadge);

          if (!aliasObj.reviewed && typeof api === "function") {
            const confirmBtn = document.createElement("button");
            confirmBtn.type = "button";
            confirmBtn.className = "quiet small confirm-alias-btn";
            confirmBtn.textContent = "Bekreft";
            confirmBtn.addEventListener("click", async () => {
              confirmBtn.disabled = true;
              try {
                await api(
                  `/catalogue/aliases/product/${encodeURIComponent(currentProduct.id)}/review`,
                  "PUT",
                  {
                    alias: aliasObj.alias,
                    locale: aliasObj.locale || "nb",
                    context: aliasObj.context || "",
                  },
                );
                aliasObj.reviewed = true;
                if (notify) notify("Kallenavn bekreftet.");
                renderCardContent();
              } catch (err) {
                if (notify) notify(err.message || "Kunne ikke bekrefte kallenavn.");
                confirmBtn.disabled = false;
              }
            });
            row.append(confirmBtn);
          }

          translationsList.append(row);
        });
      } else if (locales.length > 0) {
        locales.forEach((loc) => {
          const names = currentProduct.translations_by_locale[loc];
          if (Array.isArray(names) && names.length > 0) {
            const row = document.createElement("div");
            row.className = "locale-translation-row";

            const locBadge = document.createElement("span");
            locBadge.className = "badge locale-badge";
            locBadge.textContent = loc.toUpperCase();
            row.append(locBadge);

            const namesSpan = document.createElement("span");
            namesSpan.className = "translation-names";
            namesSpan.textContent = names.join(", ");
            row.append(namesSpan);

            translationsList.append(row);
          }
        });
      } else {
        const noTrans = document.createElement("p");
        noTrans.className = "muted";
        noTrans.textContent = "Ingen kallenavn eller oversettelser registrert.";
        translationsList.append(noTrans);
      }
      translationsSection.append(translationsList);

      if (typeof api === "function") {
        const addAliasRow = document.createElement("div");
        addAliasRow.className = "actions alias-add-row";
        const aliasInput = document.createElement("input");
        aliasInput.type = "text";
        aliasInput.className = "alias-add-input";
        aliasInput.placeholder = "Nytt kallenavn (f.eks. gulrot) …";
        aliasInput.setAttribute("aria-label", "Nytt kallenavn");
        const localeSelect = document.createElement("select");
        localeSelect.className = "alias-locale-select";
        localeSelect.setAttribute("aria-label", "Språk for kallenavn");
        ["nb", "en", "fr"].forEach((loc) => {
          const opt = document.createElement("option");
          opt.value = loc;
          opt.textContent = loc.toUpperCase();
          localeSelect.append(opt);
        });
        const addAliasBtn = document.createElement("button");
        addAliasBtn.type = "button";
        addAliasBtn.className = "quiet small alias-add-btn";
        addAliasBtn.textContent = "Lagre kallenavn";
        addAliasBtn.addEventListener("click", async () => {
          const val = aliasInput.value.trim();
          if (!val) return;
          addAliasBtn.disabled = true;
          try {
            const loc = localeSelect.value || "nb";
            await api(
              `/catalogue/aliases/product/${encodeURIComponent(currentProduct.id)}/review`,
              "PUT",
              { alias: val, locale: loc, context: "recipe" },
            );
            currentProduct.aliases = [
              ...(currentProduct.aliases || []).filter(
                (a) => !(a.alias.toLowerCase() === val.toLowerCase() && a.locale === loc),
              ),
              { alias: val, locale: loc, context: "recipe", reviewed: true },
            ];
            if (notify) notify("Kallenavn lagret og bekreftet.");
            renderCardContent();
          } catch (err) {
            if (notify) notify(err.message || "Kunne ikke lagre kallenavn.");
            addAliasBtn.disabled = false;
          }
        });
        addAliasRow.append(aliasInput, localeSelect, addAliasBtn);
        translationsSection.append(addAliasRow);
      }
      card.append(translationsSection);

      // Section: Current Stock (Strictly distinct from purchases!)
      const stockSection = document.createElement("section");
      stockSection.className = "product-stock-section";
      stockSection.setAttribute("aria-label", "Lagerbeholdning");

      const stockHeading = document.createElement("h4");
      stockHeading.textContent = "Nåværende lagerbeholdning";
      stockSection.append(stockHeading);

      const stockSummary = formatStockSummary(currentProduct.stock);
      const stockBadge = document.createElement("div");
      stockBadge.className = `badge stock-badge ${stockSummary.inStock ? "success" : "warning"}`;
      stockBadge.textContent = stockSummary.label;
      stockSection.append(stockBadge);

      const stockProvenance = document.createElement("p");
      stockProvenance.className = "muted provenance-note";
      stockProvenance.textContent = stockSummary.provenanceText;
      stockSection.append(stockProvenance);

      if (Array.isArray(currentProduct.stock?.items) && currentProduct.stock.items.length > 0) {
        const stockItemsList = document.createElement("ul");
        stockItemsList.className = "stock-items-list";
        currentProduct.stock.items.forEach((item) => {
          const li = document.createElement("li");
          let txt = `${item.name}: ${item.quantity} ${item.unit}`;
          if (item.note) txt += ` (${item.note})`;
          li.textContent = txt;
          stockItemsList.append(li);
        });
        stockSection.append(stockItemsList);
      }
      card.append(stockSection);

      // Section: Purchase Frequency & Evidence-based Provenance Counts
      const freqSection = document.createElement("section");
      freqSection.className = "product-frequency-section";
      freqSection.setAttribute("aria-label", "Kjøpshistorikk og frekvens");

      const freqHeading = document.createElement("h4");
      freqHeading.textContent = "Kjøpshistorikk og frekvens";
      freqSection.append(freqHeading);

      const freqSummary = formatFrequencySummary(currentProduct.purchase_frequency);
      const freqBadge = document.createElement("div");
      freqBadge.className = "badge frequency-badge";
      freqBadge.textContent = freqSummary.label;
      freqSection.append(freqBadge);

      if (freqSummary.details) {
        const freqDetails = document.createElement("p");
        freqDetails.className = "muted frequency-details";
        freqDetails.textContent = freqSummary.details;
        freqSection.append(freqDetails);
      }

      const provCounts = formatProvenanceCounts(currentProduct.purchase_frequency?.counts_by_provenance);
      const provInfo = document.createElement("p");
      provInfo.className = "muted provenance-counts";
      provInfo.textContent = provCounts.summaryText;
      freqSection.append(provInfo);

      card.append(freqSection);

      // Section: Oda Preferences (Preferred SKUs & Exclusions)
      const prefSection = document.createElement("section");
      prefSection.className = "product-oda-preferences-section";
      prefSection.setAttribute("aria-label", "Oda-preferanser og ekskluderinger");

      const prefHeading = document.createElement("h4");
      prefHeading.textContent = "Oda-preferanser og varevalg";
      prefSection.append(prefHeading);

      const statusBox = document.createElement("div");
      statusBox.className = "callout quiet preferences-status-box";
      statusBox.setAttribute("role", "status");
      statusBox.setAttribute("aria-live", "polite");
      statusBox.hidden = true;
      prefSection.append(statusBox);

      // Preferred SKUs list
      const prefSubsection = document.createElement("div");
      prefSubsection.className = "preferred-skus-container";

      const prefTitle = document.createElement("h5");
      prefTitle.textContent = "Foretrukne varer hos Oda (prioritert rekkefølge)";
      prefSubsection.append(prefTitle);

      const preferredList = document.createElement("ol");
      preferredList.className = "preferred-skus-list";

      const preferred = currentProduct.preferred_oda_skus || [];
      if (preferred.length === 0) {
        const emptyPref = document.createElement("p");
        emptyPref.className = "muted empty-pref-text";
        emptyPref.textContent = "Ingen foretrukne Oda-varer er valgt ennå.";
        preferredList.append(emptyPref);
      } else {
        preferred.forEach((sku, idx) => {
          const row = document.createElement("li");
          row.className = "preferred-sku-row";
          row.dataset.odaId = sku.oda_product_id;

          const rankBadge = document.createElement("span");
          rankBadge.className = "badge rank-badge";
          rankBadge.textContent = `#${sku.rank || idx + 1}`;
          row.append(rankBadge);

          const skuInfo = document.createElement("div");
          skuInfo.className = "sku-info";
          const skuName = sku.name || sku.oda_product_id;
          let label = skuName;
          if (sku.brand) label += ` (${sku.brand})`;
          if (sku.price) label += ` · ${sku.price} kr`;
          skuInfo.textContent = label;
          row.append(skuInfo);

          const skuActions = document.createElement("div");
          skuActions.className = "actions sku-actions";

          const upBtn = document.createElement("button");
          upBtn.type = "button";
          upBtn.className = "quiet move-up-btn";
          upBtn.textContent = "↑";
          upBtn.setAttribute("aria-label", `Flytt ${skuName} opp i prioritering`);
          upBtn.disabled = idx === 0;
          upBtn.addEventListener("click", () => handleMovePreferred(idx, idx - 1));
          skuActions.append(upBtn);

          const downBtn = document.createElement("button");
          downBtn.type = "button";
          downBtn.className = "quiet move-down-btn";
          downBtn.textContent = "↓";
          downBtn.setAttribute("aria-label", `Flytt ${skuName} ned i prioritering`);
          downBtn.disabled = idx === preferred.length - 1;
          downBtn.addEventListener("click", () => handleMovePreferred(idx, idx + 1));
          skuActions.append(downBtn);

          const removeBtn = document.createElement("button");
          removeBtn.type = "button";
          removeBtn.className = "quiet remove-pref-btn";
          removeBtn.textContent = "Fjern";
          removeBtn.setAttribute("aria-label", `Fjern ${skuName} fra foretrukne varer`);
          removeBtn.addEventListener("click", () => handleRemovePreferred(sku.oda_product_id));
          skuActions.append(removeBtn);

          row.append(skuActions);
          preferredList.append(row);
        });
      }
      prefSubsection.append(preferredList);

      // Add preferred SKU form
      const addPrefForm = document.createElement("form");
      addPrefForm.className = "add-preferred-sku-form";
      addPrefForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const skuInput = addPrefForm.querySelector(".pref-sku-input");
        const nameInput = addPrefForm.querySelector(".pref-name-input");
        const skuId = (skuInput.value || "").trim();
        const name = (nameInput.value || "").trim();
        if (skuId) {
          handleAddPreferred({ oda_product_id: skuId, name: name || null });
          skuInput.value = "";
          nameInput.value = "";
        }
      });

      const prefSkuInput = document.createElement("input");
      prefSkuInput.type = "text";
      prefSkuInput.className = "pref-sku-input";
      prefSkuInput.placeholder = "Oda produkt-ID (SKU)";
      prefSkuInput.setAttribute("aria-label", "Oda produkt-ID for ny foretrukket vare");
      prefSkuInput.required = true;

      const prefNameInput = document.createElement("input");
      prefNameInput.type = "text";
      prefNameInput.className = "pref-name-input";
      prefNameInput.placeholder = "Varenavn (valgfritt)";
      prefNameInput.setAttribute("aria-label", "Varenavn for ny foretrukket vare");

      const addPrefBtn = document.createElement("button");
      addPrefBtn.type = "submit";
      addPrefBtn.className = "quiet add-pref-btn";
      addPrefBtn.textContent = "Legg til foretrukket vare";

      addPrefForm.append(prefSkuInput, prefNameInput, addPrefBtn);
      prefSubsection.append(addPrefForm);
      prefSection.append(prefSubsection);

      // Excluded SKUs list
      const exclSubsection = document.createElement("div");
      exclSubsection.className = "excluded-skus-container";

      const exclTitle = document.createElement("h5");
      exclTitle.textContent = "Ekskluderte varer hos Oda";
      exclSubsection.append(exclTitle);

      const excludedList = document.createElement("ul");
      excludedList.className = "excluded-skus-list";

      const excluded = currentProduct.excluded_oda_skus || [];
      if (excluded.length === 0) {
        const emptyExcl = document.createElement("p");
        emptyExcl.className = "muted empty-excl-text";
        emptyExcl.textContent = "Ingen varer er ekskludert.";
        excludedList.append(emptyExcl);
      } else {
        excluded.forEach((sku) => {
          const row = document.createElement("li");
          row.className = "excluded-sku-row";
          row.dataset.odaId = sku.oda_product_id;

          const exclInfo = document.createElement("div");
          exclInfo.className = "sku-info";
          const skuName = sku.name || sku.oda_product_id;
          let label = skuName;
          if (sku.reason) label += ` · Grunn: ${sku.reason}`;
          exclInfo.textContent = label;
          row.append(exclInfo);

          const removeExclBtn = document.createElement("button");
          removeExclBtn.type = "button";
          removeExclBtn.className = "quiet remove-excl-btn";
          removeExclBtn.textContent = "Fjern ekskludering";
          removeExclBtn.setAttribute("aria-label", `Fjern ekskludering for ${skuName}`);
          removeExclBtn.addEventListener("click", () => handleRemoveExcluded(sku.oda_product_id));
          row.append(removeExclBtn);

          excludedList.append(row);
        });
      }
      exclSubsection.append(excludedList);

      // Add exclusion form
      const addExclForm = document.createElement("form");
      addExclForm.className = "add-excluded-sku-form";
      addExclForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const skuInput = addExclForm.querySelector(".excl-sku-input");
        const reasonInput = addExclForm.querySelector(".excl-reason-input");
        const skuId = (skuInput.value || "").trim();
        const reason = (reasonInput.value || "").trim();
        if (skuId) {
          handleAddExcluded({ oda_product_id: skuId, reason });
          skuInput.value = "";
          reasonInput.value = "";
        }
      });

      const exclSkuInput = document.createElement("input");
      exclSkuInput.type = "text";
      exclSkuInput.className = "excl-sku-input";
      exclSkuInput.placeholder = "Oda produkt-ID (SKU)";
      exclSkuInput.setAttribute("aria-label", "Oda produkt-ID for vare som skal ekskluderes");
      exclSkuInput.required = true;

      const exclReasonInput = document.createElement("input");
      exclReasonInput.type = "text";
      exclReasonInput.className = "excl-reason-input";
      exclReasonInput.placeholder = "Årsak for ekskludering (valgfritt)";
      exclReasonInput.setAttribute("aria-label", "Årsak for ekskludering");

      const addExclBtn = document.createElement("button");
      addExclBtn.type = "submit";
      addExclBtn.className = "quiet add-excl-btn";
      addExclBtn.textContent = "Ekskluder vare";

      addExclForm.append(exclSkuInput, exclReasonInput, addExclBtn);
      exclSubsection.append(addExclForm);
      prefSection.append(exclSubsection);

      card.append(prefSection);
    }

    async function savePreferences(newPreferred, newExcluded) {
      if (!api) return;
      const payload = buildPreferencesPayload(newPreferred, newExcluded);
      try {
        const updated = await api(
          `/catalogue/products/${encodeURIComponent(currentProduct.id)}/preferences`,
          "PUT",
          payload,
        );
        currentProduct = updated;
        renderCardContent();
        notify(`Oda-preferanser for «${currentProduct.display_name}» er lagret.`);
        onUpdated(currentProduct);
      } catch (err) {
        notify(`Kunne ikke lagre preferanser: ${err.message || "Ukjent feil"}`, true);
      }
    }

    function handleMovePreferred(fromIdx, toIdx) {
      const reordered = reorderPreferredSkus(currentProduct.preferred_oda_skus, fromIdx, toIdx);
      savePreferences(reordered, currentProduct.excluded_oda_skus);
    }

    function handleAddPreferred(skuObj) {
      const added = addPreferredSku(currentProduct.preferred_oda_skus, skuObj);
      savePreferences(added, currentProduct.excluded_oda_skus);
    }

    function handleRemovePreferred(skuId) {
      const removed = removePreferredSku(currentProduct.preferred_oda_skus, skuId);
      savePreferences(removed, currentProduct.excluded_oda_skus);
    }

    function handleAddExcluded(exclObj) {
      const added = addExcludedSku(currentProduct.excluded_oda_skus, exclObj);
      savePreferences(currentProduct.preferred_oda_skus, added);
    }

    function handleRemoveExcluded(skuId) {
      const removed = removeExcludedSku(currentProduct.excluded_oda_skus, skuId);
      savePreferences(currentProduct.preferred_oda_skus, removed);
    }

    renderCardContent();
    return card;
  }

  function renderProductOverview(container, options = {}) {
    if (!container) return null;

    const api = options.api;
    const notify = options.notify || (() => {});
    const locale = options.locale || "nb";

    container.replaceChildren();

    const wrapper = document.createElement("div");
    wrapper.className = "product-overview-view";

    // Title & description
    const header = document.createElement("div");
    header.className = "overview-header";

    const heading = document.createElement("h1");
    heading.textContent = "Råvareoversikt og preferanser";
    header.append(heading);

    const desc = document.createElement("p");
    desc.className = "muted";
    desc.textContent =
      "Her finner du husholdningens kanoniske råvarer med oversettelser, faktisk lagerbeholdning, kjøpsfrekvens og foretrukne varer hos Oda.";
    header.append(desc);

    wrapper.append(header);

    // Search toolbar
    const toolbar = document.createElement("div");
    toolbar.className = "toolbar catalogue-toolbar";

    const searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.className = "search catalogue-search-input";
    searchInput.placeholder = "Søk etter råvare, variant eller kallenavn …";
    searchInput.setAttribute("aria-label", "Søk etter råvare");
    toolbar.append(searchInput);

    wrapper.append(toolbar);

    // Status message for screen readers
    const liveStatus = document.createElement("div");
    liveStatus.className = "visually-hidden";
    liveStatus.setAttribute("role", "status");
    liveStatus.setAttribute("aria-live", "polite");
    wrapper.append(liveStatus);

    // Results container
    const resultsContainer = document.createElement("div");
    resultsContainer.className = "product-overview-list";
    resultsContainer.innerHTML = '<div class="loading">Laster inn råvarer …</div>';
    wrapper.append(resultsContainer);

    let searchTimeout = null;

    async function loadProducts(query = "") {
      if (!api) return;
      resultsContainer.innerHTML = '<div class="loading">Henter råvarer …</div>';
      try {
        let url = `/catalogue/overview?locale=${encodeURIComponent(locale)}`;
        if (query.trim()) {
          url += `&q=${encodeURIComponent(query.trim())}`;
        }
        const products = await api(url);
        resultsContainer.replaceChildren();

        if (!Array.isArray(products) || products.length === 0) {
          const empty = document.createElement("div");
          empty.className = "empty catalogue-empty";

          const emptyTitle = document.createElement("h2");
          emptyTitle.textContent = query.trim() ? "Ingen råvarer matcher søket" : "Ingen råvarer funnet";
          empty.append(emptyTitle);

          const emptyDesc = document.createElement("p");
          emptyDesc.textContent = query.trim()
            ? `Fant ingen kanoniske råvarer som matcher «${query.trim()}».`
            : "Husholdningen har ikke registrert noen kanoniske råvarer ennå.";
          empty.append(emptyDesc);

          resultsContainer.append(empty);
          liveStatus.textContent = "Ingen råvarer funnet.";
          return;
        }

        liveStatus.textContent = `Fant ${products.length} råvarer.`;

        products.forEach((prod) => {
          const card = renderProductCard(prod, {
            api,
            notify,
            onUpdated: (updatedProd) => {
              // Optionally refresh list if needed
            },
          });
          resultsContainer.append(card);
        });
      } catch (err) {
        resultsContainer.replaceChildren();
        const errorCard = document.createElement("div");
        errorCard.className = "callout error";
        errorCard.setAttribute("role", "alert");
        errorCard.textContent = `Kunne ikke laste råvareoversikt: ${err.message || "Ukjent feil"}`;
        resultsContainer.append(errorCard);
        liveStatus.textContent = "Feil ved lasting av råvarer.";
      }
    }

    searchInput.addEventListener("input", (e) => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        loadProducts(e.target.value);
      }, 250);
    });

    const initialLoad = loadProducts("");
    const curatedHost = document.createElement("div");
    curatedHost.className = "curated-catalogue-host";
    renderCuratedFoodsSection(curatedHost, { api, notify });
    wrapper.append(curatedHost);
    container.append(wrapper);

    return {
      reload: () => loadProducts(searchInput.value),
      ready: initialLoad,
    };
  }

  function renderCuratedFoodsSection(container, { api, notify }) {
    const details = document.createElement("details");
    details.className = "panel curated-foods-panel";

    const summary = document.createElement("summary");
    summary.textContent = "Matvaretabellen – referansekatalog";
    details.append(summary);

    const body = document.createElement("div");
    body.className = "stack curated-foods-body";

    const note = document.createElement("p");
    note.className = "muted";
    note.textContent =
      "Referansedata fra Matvaretabellen 2026. Visning eller kobling her oppretter ikke lagerbeholdning eller kjøpshistorikk.";
    body.append(note);

    const searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.placeholder = "Søk i Matvaretabellen …";
    searchInput.setAttribute("aria-label", "Søk i Matvaretabellen");
    body.append(searchInput);

    const listEl = document.createElement("div");
    listEl.className = "stack curated-foods-list";
    body.append(listEl);

    let loadedOnce = false;
    async function loadFoods(q) {
      try {
        const params = new URLSearchParams({ q: (q || "").trim(), limit: "37" });
        const foods = await api(`/catalogue/foods?${params.toString()}`);
        listEl.replaceChildren();
        if (!Array.isArray(foods) || foods.length === 0) {
          const empty = document.createElement("p");
          empty.className = "muted";
          empty.textContent = "Ingen matvarer funnet i Matvaretabellen.";
          listEl.append(empty);
          return;
        }
        foods.forEach((food) => {
          const card = document.createElement("article");
          card.className = "curated-food-card";
          const title = document.createElement("strong");
          title.textContent = food.display_name || food.source_name;
          card.append(title);

          const meta = document.createElement("p");
          meta.className = "muted";
          const parts = [
            food.source_name && food.source_name !== food.display_name
              ? `Kilde: ${food.source_name}`
              : null,
            `ID: ${food.source_item_id}`,
            food.category,
            food.default_unit ? `Standard enhet: ${food.default_unit}` : null,
          ].filter(Boolean);
          meta.textContent = parts.join(" · ");
          card.append(meta);

          const optionsHost = document.createElement("div");
          optionsHost.className = "stack curated-food-options";

          const loadOptsBtn = document.createElement("button");
          loadOptsBtn.type = "button";
          loadOptsBtn.className = "quiet small";
          loadOptsBtn.textContent = "Vis produktkoblinger";
          loadOptsBtn.addEventListener("click", async () => {
            loadOptsBtn.disabled = true;
            try {
              const opts = await api(`/catalogue/foods/${encodeURIComponent(food.id)}/products`);
              optionsHost.replaceChildren();
              if (!Array.isArray(opts) || opts.length === 0) {
                const none = document.createElement("p");
                none.className = "muted";
                none.textContent = "Ingen koblede butikkprodukter ennå.";
                optionsHost.append(none);
              } else {
                const ul = document.createElement("ul");
                ul.className = "stack";
                opts.forEach((opt) => {
                  const li = document.createElement("li");
                  const label = document.createElement("span");
                  label.textContent = `${opt.snapshot?.name || opt.product_id} (${opt.provider})${opt.preferred ? " ★ Foretrukket" : ""}`;
                  const toggleBtn = document.createElement("button");
                  toggleBtn.type = "button";
                  toggleBtn.className = "quiet small";
                  toggleBtn.textContent = opt.preferred ? "Fjern foretrukket" : "Velg som foretrukket";
                  toggleBtn.addEventListener("click", async () => {
                    await api(
                      `/catalogue/products/${encodeURIComponent(opt.id)}/preferred`,
                      opt.preferred ? "DELETE" : "PUT",
                    );
                    if (notify) notify("Produktpreferanse oppdatert.");
                    loadOptsBtn.click();
                  });
                  li.append(label, " ", toggleBtn);
                  ul.append(li);
                });
                optionsHost.append(ul);
              }
            } catch (err) {
              if (notify) notify(err.message || "Kunne ikke hente produktkoblinger.");
            } finally {
              loadOptsBtn.disabled = false;
            }
          });

          card.append(loadOptsBtn, optionsHost);
          listEl.append(card);
        });
      } catch (err) {
        listEl.replaceChildren();
        const errP = document.createElement("p");
        errP.className = "callout error";
        errP.textContent = `Kunne ikke hente Matvaretabellen: ${err.message || "Ukjent feil"}`;
        listEl.append(errP);
      }
    }

    details.addEventListener("toggle", () => {
      if (details.open && !loadedOnce) {
        loadedOnce = true;
        loadFoods(searchInput.value);
      }
    });
    let timer = null;
    searchInput.addEventListener("input", (e) => {
      clearTimeout(timer);
      timer = setTimeout(() => loadFoods(e.target.value), 200);
    });

    details.append(body);
    container.append(details);
    return { loadFoods, details };
  }

  root.KitchenProductOverviewView = {
    formatStockSummary,
    formatFrequencySummary,
    formatProvenanceCounts,
    reorderPreferredSkus,
    addPreferredSku,
    removePreferredSku,
    addExcludedSku,
    removeExcludedSku,
    buildPreferencesPayload,
    renderProductCard,
    renderProductOverview,
    renderCuratedFoodsSection,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenProductOverviewView;
  }
})(typeof window === "undefined" ? globalThis : window);
