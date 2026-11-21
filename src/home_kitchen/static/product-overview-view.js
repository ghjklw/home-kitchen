(function (root) {
  "use strict";

  function formatUnitLabel(unit) {
    const raw = String(unit || "").trim();
    if (!raw) return "";
    return raw.toLowerCase() === "unknown" ? "ukjent" : raw;
  }

  function formatLocaleBadge(locale) {
    const raw = String(locale || "nb").trim();
    if (!raw || raw.toLowerCase() === "unknown") return "UKJENT";
    return raw.toUpperCase();
  }

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
    const unitLabel = formatUnitLabel(stock.unit);
    const unitStr = unitLabel ? " " + unitLabel : "";
    return {
      inStock: true,
      label: `På lager: ${stock.total_quantity}${unitStr}`,
      provenanceText: "Kilde: Kjøkkenlager (ikke estimert fra kjøp)",
      totalQuantity: stock.total_quantity,
      unit: stock.unit || "",
    };
  }

  function formatFrequencyDisplay(display, averageIntervalDays = null) {
    const raw = String(display || "").trim();
    if (raw) {
      if (raw.toLowerCase() === "unknown") return "ukjent";
      const match = /^every\s+(\d+(?:[.,]\d+)?)\s+days?$/i.exec(raw);
      if (match) {
        const numVal = Number(match[1].replace(",", "."));
        const numText = match[1].replace(".", ",");
        return numVal === 1 ? "hver dag" : `hver ${numText} dager`;
      }
      return raw;
    }
    if (averageIntervalDays != null && !Number.isNaN(Number(averageIntervalDays))) {
      const numVal = Number(averageIntervalDays);
      const numText = String(averageIntervalDays).replace(".", ",");
      return numVal === 1 ? "ca. hver dag" : `ca. hver ${numText} dager`;
    }
    return "ukjent";
  }

  function formatFrequencySummary(frequency) {
    if (!frequency || frequency.status !== "known") {
      const rawDisplay = String(frequency?.frequency_display || "").trim();
      const details =
        !rawDisplay || rawDisplay.toLowerCase() === "unknown"
          ? "ukjent"
          : formatFrequencyDisplay(rawDisplay, frequency?.average_interval_days);
      return {
        known: false,
        label: "Ukjent frekvens (ikke nok kjøpshistorikk)",
        details,
        sampleSize: frequency?.sample_size || 0,
      };
    }
    const sample = frequency.sample_size ? `${frequency.sample_size} kjøp` : "";
    const days = frequency.date_range_days != null ? `over ${frequency.date_range_days} dager` : "";
    const detailParts = [sample, days].filter(Boolean).join(" ");
    return {
      known: true,
      label: formatFrequencyDisplay(frequency.frequency_display, frequency.average_interval_days),
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

  function formatProviderLabel(provider) {
    const norm = String(provider || "").trim().toLowerCase();
    if (norm === "oda") return "Oda";
    if (norm === "coop") return "Coop";
    if (norm === "trumf") return "Trumf";
    if (norm === "rema1000") return "REMA 1000";
    return provider || "Butikk";
  }

  function translateFoodCategory(category) {
    const raw = String(category || "").trim();
    if (!raw) return "";
    const shoppingView =
      (typeof window !== "undefined" ? window.KitchenShoppingView : null) ||
      root.KitchenShoppingView;
    return shoppingView?.categoryLabel?.(raw) || raw;
  }

  function renderProductCard(product, options = {}) {
    const api = options.api;
    const notify = options.notify || (() => {});
    const onUpdated = options.onUpdated || (() => {});
    const onMerged = options.onMerged || (() => {});
    const onAliasReassigned = options.onAliasReassigned || (() => {});
    const mergeCandidates = Array.isArray(options.mergeCandidates)
      ? options.mergeCandidates
      : [];
    const reassignCandidates = Array.isArray(options.reassignCandidates)
      ? options.reassignCandidates
      : mergeCandidates;

    let currentProduct = { ...product };
    let prefStatusBox = null;

    const card = document.createElement("article");
    card.className = "card product-overview-card";
    card.dataset.productId = currentProduct.id;

    function renderCardContent(prefStatusMessage = "") {
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
          locBadge.textContent = formatLocaleBadge(aliasObj.locale);
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
            confirmBtn.setAttribute(
              "aria-label",
              `Bekreft kallenavn ${aliasObj.alias} (${(aliasObj.locale || "nb").toUpperCase()})`,
            );
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

          if (typeof api === "function") {
            const otherProducts = reassignCandidates.filter(
              (p) => p && p.id !== currentProduct.id,
            );
            const reassignBtn = document.createElement("button");
            reassignBtn.type = "button";
            reassignBtn.className = "quiet small reassign-alias-btn";
            reassignBtn.textContent = "Endre kobling";
            reassignBtn.setAttribute("aria-expanded", "false");
            reassignBtn.setAttribute(
              "aria-label",
              `Endre kobling for kallenavn ${aliasObj.alias} (${formatLocaleBadge(aliasObj.locale)})`,
            );
            reassignBtn.disabled = otherProducts.length === 0;
            row.append(reassignBtn);

            const reassignForm = document.createElement("form");
            reassignForm.className = "alias-reassign-form";
            reassignForm.hidden = true;

            const reassignSelect = document.createElement("select");
            reassignSelect.className = "alias-reassign-select";
            reassignSelect.setAttribute(
              "aria-label",
              `Velg ny råvare for kallenavn ${aliasObj.alias}`,
            );

            const placeholderOpt = document.createElement("option");
            placeholderOpt.value = "";
            placeholderOpt.textContent =
              otherProducts.length > 0 ? "Velg ny råvare …" : "Ingen andre råvarer tilgjengelig";
            reassignSelect.append(placeholderOpt);

            otherProducts.forEach((cand) => {
              const opt = document.createElement("option");
              opt.value = cand.id;
              const variantPart = cand.variant ? ` · ${cand.variant}` : "";
              opt.textContent = `${cand.display_name || cand.canonical_name} (${cand.canonical_name}${variantPart})`;
              reassignSelect.append(opt);
            });

            const reassignSubmitBtn = document.createElement("button");
            reassignSubmitBtn.type = "submit";
            reassignSubmitBtn.className = "quiet small alias-reassign-submit";
            reassignSubmitBtn.textContent = "Flytt kobling";
            reassignSubmitBtn.disabled = otherProducts.length === 0;

            const reassignCancelBtn = document.createElement("button");
            reassignCancelBtn.type = "button";
            reassignCancelBtn.className = "quiet small alias-reassign-cancel";
            reassignCancelBtn.textContent = "Avbryt";

            reassignBtn.addEventListener("click", () => {
              const willOpen = reassignForm.hidden;
              reassignForm.hidden = !willOpen;
              reassignBtn.setAttribute("aria-expanded", willOpen ? "true" : "false");
              if (willOpen && typeof reassignSelect.focus === "function") {
                reassignSelect.focus();
              }
            });

            reassignCancelBtn.addEventListener("click", () => {
              reassignForm.hidden = true;
              reassignBtn.setAttribute("aria-expanded", "false");
              if (typeof reassignBtn.focus === "function") {
                reassignBtn.focus();
              }
            });

            let reassigning = false;
            async function submitReassign(e) {
              if (e && typeof e.preventDefault === "function") e.preventDefault();
              if (reassigning) return;
              const targetId = String(reassignSelect.value || "").trim();
              if (!targetId || targetId === currentProduct.id) return;
              const targetCandidate = otherProducts.find((p) => p.id === targetId);
              const targetLabel =
                targetCandidate?.display_name || targetCandidate?.canonical_name || targetId;
              reassigning = true;
              reassignSubmitBtn.disabled = true;
              try {
                await api("/catalogue/aliases/correct", "POST", {
                  kind: "product",
                  identity_id: targetId,
                  alias: aliasObj.alias,
                  locale: aliasObj.locale || "nb",
                  context: aliasObj.context ?? "",
                  reviewed: true,
                });
                currentProduct.aliases = (currentProduct.aliases || []).filter(
                  (a) =>
                    !(
                      a.alias === aliasObj.alias &&
                      (a.locale || "nb") === (aliasObj.locale || "nb") &&
                      (a.context ?? "") === (aliasObj.context ?? "")
                    ),
                );
                if (
                  currentProduct.translations_by_locale &&
                  Array.isArray(currentProduct.translations_by_locale[aliasObj.locale])
                ) {
                  currentProduct.translations_by_locale = {
                    ...currentProduct.translations_by_locale,
                    [aliasObj.locale]: currentProduct.translations_by_locale[
                      aliasObj.locale
                    ].filter((name) => name !== aliasObj.alias),
                  };
                }
                notify(`Kallenavnet «${aliasObj.alias}» ble flyttet til «${targetLabel}».`);
                renderCardContent();
                onUpdated(currentProduct);
                onAliasReassigned(targetId, aliasObj);
              } catch (err) {
                notify(`Kunne ikke flytte kallenavn: ${err.message || "Ukjent feil"}`, true);
                reassignSubmitBtn.disabled = false;
              } finally {
                reassigning = false;
              }
            }

            reassignForm.addEventListener("submit", submitReassign);
            reassignForm.append(reassignSelect, reassignSubmitBtn, reassignCancelBtn);
            row.append(reassignForm);
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
            locBadge.textContent = formatLocaleBadge(loc);
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
        aliasInput.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            addAliasBtn.click();
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
          const itemUnit = formatUnitLabel(item.unit);
          let txt = `${item.name}: ${item.quantity}${itemUnit ? " " + itemUnit : ""}`;
          if (item.note) txt += ` (${item.note})`;
          li.textContent = txt;
          stockItemsList.append(li);
        });
        stockSection.append(stockItemsList);
      }

      if (typeof api === "function") {
        const stockEditForm = document.createElement("form");
        stockEditForm.className = "stock-edit-form";

        const firstStockItem =
          Array.isArray(currentProduct.stock?.items) && currentProduct.stock.items.length > 0
            ? currentProduct.stock.items[0]
            : null;

        const stockQtyInput = document.createElement("input");
        stockQtyInput.type = "number";
        stockQtyInput.className = "stock-qty-input";
        stockQtyInput.setAttribute("min", "0");
        stockQtyInput.setAttribute("step", "any");
        stockQtyInput.placeholder = "Mengde";
        stockQtyInput.setAttribute("aria-label", "Mengde på lager");
        stockQtyInput.required = true;
        stockQtyInput.value = firstStockItem
          ? String(firstStockItem.quantity)
          : currentProduct.stock?.in_stock
            ? String(currentProduct.stock.total_quantity)
            : "";

        const stockUnitInput = document.createElement("input");
        stockUnitInput.type = "text";
        stockUnitInput.className = "stock-unit-input";
        stockUnitInput.placeholder = "Enhet (f.eks. stk, kg, l)";
        stockUnitInput.setAttribute("aria-label", "Enhet for lagerbeholdning");
        stockUnitInput.required = true;
        stockUnitInput.value = firstStockItem?.unit || currentProduct.stock?.unit || "stk";

        const stockNoteInput = document.createElement("input");
        stockNoteInput.type = "text";
        stockNoteInput.className = "stock-note-input";
        stockNoteInput.placeholder = "Notat (valgfritt)";
        stockNoteInput.setAttribute("aria-label", "Notat for lagerbeholdning");
        stockNoteInput.value = firstStockItem?.note || "";

        const stockSaveBtn = document.createElement("button");
        stockSaveBtn.type = "submit";
        stockSaveBtn.className = "quiet stock-save-btn";
        stockSaveBtn.textContent = "Lagre beholdning";

        let savingStock = false;
        async function submitStockUpdate(e) {
          if (e && typeof e.preventDefault === "function") e.preventDefault();
          if (savingStock) return;
          const qtyStr = String(stockQtyInput.value ?? "").trim();
          const unitStr = String(stockUnitInput.value ?? "").trim() || "stk";
          const noteStr = String(stockNoteInput.value ?? "").trim();
          if (!qtyStr || Number.isNaN(Number(qtyStr)) || Number(qtyStr) < 0) return;
          savingStock = true;
          stockSaveBtn.disabled = true;
          try {
            const updated = await api(
              `/catalogue/products/${encodeURIComponent(currentProduct.id)}/stock`,
              "PUT",
              {
                quantity: qtyStr,
                unit: unitStr,
                category: firstStockItem?.category || "Other",
                note: noteStr || null,
                inventory_id: firstStockItem?.inventory_id || null,
              },
            );
            currentProduct = updated;
            renderCardContent();
            notify(`Lagerbeholdning for «${currentProduct.display_name}» er lagret.`);
            onUpdated(currentProduct);
          } catch (err) {
            notify(`Kunne ikke lagre lagerbeholdning: ${err.message || "Ukjent feil"}`, true);
            stockSaveBtn.disabled = false;
          } finally {
            savingStock = false;
          }
        }

        stockEditForm.addEventListener("submit", submitStockUpdate);
        stockSaveBtn.addEventListener("click", submitStockUpdate);

        stockEditForm.append(stockQtyInput, stockUnitInput, stockNoteInput, stockSaveBtn);
        stockSection.append(stockEditForm);
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
      statusBox.textContent = prefStatusMessage || "";
      statusBox.hidden = !prefStatusMessage;
      prefStatusBox = statusBox;
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

      const preferredIds = new Set(preferred.map((s) => s.oda_product_id));
      const excludedIds = new Set(excluded.map((e) => e.oda_product_id));

      // Remembered Oda SKUs (distinct from preferred & purchased)
      const remSubsection = document.createElement("div");
      remSubsection.className = "remembered-skus-container";

      const remTitle = document.createElement("h5");
      remTitle.textContent = "Huskede Oda-valg";
      remSubsection.append(remTitle);

      const remNote = document.createElement("p");
      remNote.className = "muted remembered-skus-note";
      remNote.textContent =
        "Tidligere valgte Oda-varer i handlekurv eller katalog (ikke automatisk foretrukket eller bekreftet kjøpt).";
      remSubsection.append(remNote);

      const rememberedList = document.createElement("ul");
      rememberedList.className = "remembered-skus-list";

      const remembered = Array.isArray(currentProduct.remembered_oda_skus)
        ? currentProduct.remembered_oda_skus
        : [];
      if (remembered.length === 0) {
        const emptyRem = document.createElement("p");
        emptyRem.className = "muted empty-remembered-text";
        emptyRem.textContent = "Ingen huskede Oda-valg registrert.";
        rememberedList.append(emptyRem);
      } else {
        remembered.forEach((sku) => {
          const row = document.createElement("li");
          row.className = "remembered-sku-row";
          row.dataset.odaId = sku.oda_product_id;

          const remBadge = document.createElement("span");
          remBadge.className = "badge remembered-badge";
          remBadge.textContent = "Husket valg";
          row.append(remBadge);

          const skuInfo = document.createElement("div");
          skuInfo.className = "sku-info";
          const skuName = sku.name || sku.oda_product_id;
          let label = skuName;
          if (sku.brand) label += ` (${sku.brand})`;
          if (sku.price) label += ` · ${sku.price} kr`;
          const selCount = Number(sku.selection_count || 1);
          label += ` · Valgt ${selCount} ${selCount === 1 ? "gang" : "ganger"}`;
          skuInfo.textContent = label;
          row.append(skuInfo);

          const actions = document.createElement("div");
          actions.className = "actions sku-actions";

          const promoteBtn = document.createElement("button");
          promoteBtn.type = "button";
          promoteBtn.className = "quiet small promote-remembered-btn";
          promoteBtn.textContent = "Gjør foretrukket";
          promoteBtn.setAttribute("aria-label", `Gjør ${skuName} til foretrukket vare`);
          promoteBtn.disabled =
            preferredIds.has(sku.oda_product_id) || excludedIds.has(sku.oda_product_id);
          promoteBtn.addEventListener("click", () =>
            handleAddPreferred({
              oda_product_id: sku.oda_product_id,
              name: sku.name || null,
              brand: sku.brand || null,
              price: sku.price || null,
              unit_name: sku.unit_name || null,
            }),
          );
          actions.append(promoteBtn);

          const excludeBtn = document.createElement("button");
          excludeBtn.type = "button";
          excludeBtn.className = "quiet small exclude-remembered-btn";
          excludeBtn.textContent = "Ekskluder";
          excludeBtn.setAttribute("aria-label", `Ekskluder ${skuName}`);
          excludeBtn.disabled = excludedIds.has(sku.oda_product_id);
          excludeBtn.addEventListener("click", () =>
            handleAddExcluded({
              oda_product_id: sku.oda_product_id,
              name: sku.name || null,
              reason: "",
            }),
          );
          actions.append(excludeBtn);

          row.append(actions);
          rememberedList.append(row);
        });
      }
      remSubsection.append(rememberedList);
      prefSection.append(remSubsection);

      // Previously Purchased Store/Receipt SKUs (distinct from remembered & preferred)
      const purSubsection = document.createElement("div");
      purSubsection.className = "purchased-skus-container";

      const purTitle = document.createElement("h5");
      purTitle.textContent = "Tidligere kjøpte butikkvarer";
      purSubsection.append(purTitle);

      const purNote = document.createElement("p");
      purNote.className = "muted purchased-skus-note";
      purNote.textContent =
        "Faktiske varer observert i bekreftede kvitteringer og leverte ordre.";
      purSubsection.append(purNote);

      const purchasedList = document.createElement("ul");
      purchasedList.className = "purchased-skus-list";

      const previouslyPurchased = Array.isArray(currentProduct.previously_purchased_skus)
        ? currentProduct.previously_purchased_skus
        : [];
      if (previouslyPurchased.length === 0) {
        const emptyPur = document.createElement("p");
        emptyPur.className = "muted empty-purchased-text";
        emptyPur.textContent = "Ingen tidligere kjøpte varer registrert fra kvitteringer.";
        purchasedList.append(emptyPur);
      } else {
        previouslyPurchased.forEach((sku) => {
          const row = document.createElement("li");
          row.className = "purchased-sku-row";
          row.dataset.provider = sku.provider || "";
          row.dataset.skuId = sku.sku_id;

          const storeBadge = document.createElement("span");
          storeBadge.className = "badge store-badge";
          storeBadge.textContent = sku.store_name || formatProviderLabel(sku.provider);
          row.append(storeBadge);

          const skuInfo = document.createElement("div");
          skuInfo.className = "sku-info";
          const skuName = sku.name || sku.sku_id;
          let label = skuName;
          if (sku.ean && sku.ean !== skuName) label += ` · EAN: ${sku.ean}`;
          else if (sku.sku_id && sku.sku_id !== skuName) label += ` · SKU: ${sku.sku_id}`;
          const count = Number(sku.purchase_count || 1);
          label += ` · Kjøpt ${count} ${count === 1 ? "gang" : "ganger"}`;
          skuInfo.textContent = label;
          row.append(skuInfo);

          const provBadge = document.createElement("span");
          provBadge.className = `badge ${sku.provenance === "provider_verified" ? "success" : "warning"} purchase-provenance-badge`;
          provBadge.textContent =
            sku.provenance === "provider_verified" ? "Verifisert kvittering" : "Brukerbekreftet";
          row.append(provBadge);

          if ((sku.provider || "").toLowerCase() === "oda") {
            const actions = document.createElement("div");
            actions.className = "actions sku-actions";

            const promoteBtn = document.createElement("button");
            promoteBtn.type = "button";
            promoteBtn.className = "quiet small promote-purchased-btn";
            promoteBtn.textContent = "Gjør foretrukket";
            promoteBtn.setAttribute("aria-label", `Gjør ${skuName} til foretrukket vare`);
            promoteBtn.disabled = preferredIds.has(sku.sku_id) || excludedIds.has(sku.sku_id);
            promoteBtn.addEventListener("click", () =>
              handleAddPreferred({
                oda_product_id: sku.sku_id,
                name: sku.name || null,
              }),
            );
            actions.append(promoteBtn);

            const excludeBtn = document.createElement("button");
            excludeBtn.type = "button";
            excludeBtn.className = "quiet small exclude-purchased-btn";
            excludeBtn.textContent = "Ekskluder";
            excludeBtn.setAttribute("aria-label", `Ekskluder ${skuName}`);
            excludeBtn.disabled = excludedIds.has(sku.sku_id);
            excludeBtn.addEventListener("click", () =>
              handleAddExcluded({
                oda_product_id: sku.sku_id,
                name: sku.name || null,
                reason: "",
              }),
            );
            actions.append(excludeBtn);

            row.append(actions);
          }

          purchasedList.append(row);
        });
      }
      purSubsection.append(purchasedList);
      prefSection.append(purSubsection);

      card.append(prefSection);

      // Section: Canonical Product Merge / Reconcile
      if (typeof api === "function") {
        const mergeSection = document.createElement("section");
        mergeSection.className = "product-merge-section";
        mergeSection.setAttribute("aria-label", "Slå sammen duplikatråvare");

        const mergeHeading = document.createElement("h4");
        mergeHeading.textContent = "Slå sammen duplikatråvare";
        mergeSection.append(mergeHeading);

        const mergeDesc = document.createElement("p");
        mergeDesc.className = "muted merge-description";
        mergeDesc.textContent =
          "Flytter kallenavn, lagerbeholdning, Oda-preferanser, oppskriftskoblinger og kjøpshistorikk til valgt målråvare og fjerner denne duplikaten.";
        mergeSection.append(mergeDesc);

        const candidates = mergeCandidates.filter((p) => p && p.id !== currentProduct.id);
        const mergeForm = document.createElement("form");
        mergeForm.className = "merge-product-form";

        const mergeSelect = document.createElement("select");
        mergeSelect.className = "merge-target-select";
        mergeSelect.setAttribute("aria-label", "Velg målråvare å slå sammen inn i");

        const placeholderOpt = document.createElement("option");
        placeholderOpt.value = "";
        placeholderOpt.textContent =
          candidates.length > 0
            ? "Velg målråvare …"
            : "Ingen andre råvarer tilgjengelig";
        mergeSelect.append(placeholderOpt);

        candidates.forEach((cand) => {
          const opt = document.createElement("option");
          opt.value = cand.id;
          const variantPart = cand.variant ? ` · ${cand.variant}` : "";
          opt.textContent = `${cand.display_name || cand.canonical_name} (${cand.canonical_name}${variantPart})`;
          mergeSelect.append(opt);
        });

        const mergeBtn = document.createElement("button");
        mergeBtn.type = "submit";
        mergeBtn.className = "quiet merge-product-btn";
        mergeBtn.textContent = "Forhåndsvis sammenslåing";
        mergeBtn.disabled = candidates.length === 0;

        const mergePreviewHost = document.createElement("div");
        mergePreviewHost.className = "merge-preview-host";

        let loadingPreview = false;
        let merging = false;

        function clearMergePreview() {
          mergePreviewHost.replaceChildren();
        }

        function renderMergePreview(targetId, preview) {
          clearMergePreview();

          const box = document.createElement("div");
          box.className = "callout merge-preview-box";
          box.setAttribute("role", "region");
          box.setAttribute("aria-label", "Forhåndsvisning av sammenslåing");

          const summary = document.createElement("p");
          summary.className = "merge-preview-summary";
          const sourceLabel = preview.source_display_name || currentProduct.display_name;
          const targetLabel = preview.target_display_name || targetId;
          summary.textContent = `Forhåndsvisning av sammenslåing: «${sourceLabel}» → «${targetLabel}»`;
          box.append(summary);

          const impactList = document.createElement("ul");
          impactList.className = "merge-impact-list";
          const impactItems = [
            `Kallenavn som flyttes: ${preview.aliases_to_move ?? 0}`,
            `Lagerlinjer som flyttes: ${preview.stock_rows_to_move ?? 0}`,
            `Oppskriftskoblinger som flyttes: ${preview.recipe_links_to_move ?? 0}`,
            `Oda-preferanser som flyttes: ${preview.preferences_to_move ?? 0}`,
            `Oda-ekskluderinger som flyttes: ${preview.exclusions_to_move ?? 0}`,
            `Kjøpshistorikkrader som flyttes: ${preview.purchase_history_rows_to_move ?? 0}`,
          ];
          impactItems.forEach((line) => {
            const li = document.createElement("li");
            li.textContent = line;
            impactList.append(li);
          });
          box.append(impactList);

          let variantConfirmCheckbox = null;
          if (preview.variant_mismatch) {
            const warning = document.createElement("div");
            warning.className = "callout warning merge-variant-warning";
            warning.setAttribute("role", "alert");
            const srcVar = preview.source_variant || "uten variant";
            const tgtVar = preview.target_variant || "uten variant";
            warning.textContent = `Advarsel: Variantene er ulike («${srcVar}» vs. «${tgtVar}»). Kontroller at dette ikke er to ulike råvarevarianter før du slår dem sammen.`;
            box.append(warning);

            const confirmLabel = document.createElement("label");
            confirmLabel.className = "merge-variant-confirm-label";
            variantConfirmCheckbox = document.createElement("input");
            variantConfirmCheckbox.type = "checkbox";
            variantConfirmCheckbox.className = "merge-variant-confirm-checkbox";
            variantConfirmCheckbox.checked = false;
            const confirmText = document.createElement("span");
            confirmText.textContent = "Jeg bekrefter at ulike varianter skal slås sammen";
            confirmLabel.append(variantConfirmCheckbox, " ", confirmText);
            box.append(confirmLabel);
          }

          const actions = document.createElement("div");
          actions.className = "actions merge-preview-actions";

          const confirmMergeBtn = document.createElement("button");
          confirmMergeBtn.type = "button";
          confirmMergeBtn.className = "primary confirm-merge-btn";
          confirmMergeBtn.textContent = "Bekreft sammenslåing";
          confirmMergeBtn.disabled = Boolean(preview.variant_mismatch);

          if (variantConfirmCheckbox) {
            variantConfirmCheckbox.addEventListener("change", () => {
              confirmMergeBtn.disabled = !variantConfirmCheckbox.checked;
            });
          }

          confirmMergeBtn.addEventListener("click", async () => {
            if (merging) return;
            if (preview.variant_mismatch && !variantConfirmCheckbox?.checked) return;
            merging = true;
            confirmMergeBtn.disabled = true;
            mergeBtn.disabled = true;
            try {
              const mergedTarget = await api(
                `/catalogue/products/${encodeURIComponent(targetId)}/merge`,
                "POST",
                { source_product_id: currentProduct.id },
              );
              notify(
                `Råvaren «${currentProduct.display_name}» ble slått sammen inn i «${mergedTarget?.display_name || targetId}».`,
              );
              onMerged(mergedTarget, currentProduct.id);
            } catch (err) {
              notify(`Kunne ikke slå sammen råvare: ${err.message || "Ukjent feil"}`, true);
              confirmMergeBtn.disabled = Boolean(
                preview.variant_mismatch && !variantConfirmCheckbox?.checked,
              );
              mergeBtn.disabled = false;
            } finally {
              merging = false;
            }
          });

          const cancelMergeBtn = document.createElement("button");
          cancelMergeBtn.type = "button";
          cancelMergeBtn.className = "quiet cancel-merge-btn";
          cancelMergeBtn.textContent = "Avbryt";
          cancelMergeBtn.addEventListener("click", clearMergePreview);

          actions.append(confirmMergeBtn, cancelMergeBtn);
          box.append(actions);
          mergePreviewHost.append(box);
        }

        async function requestMergePreview(e) {
          if (e && typeof e.preventDefault === "function") e.preventDefault();
          if (loadingPreview || merging) return;
          const targetId = String(mergeSelect.value || "").trim();
          if (!targetId || targetId === currentProduct.id) return;
          loadingPreview = true;
          mergeBtn.disabled = true;
          try {
            const preview = await api(
              `/catalogue/products/${encodeURIComponent(targetId)}/merge-preview?source_id=${encodeURIComponent(currentProduct.id)}`,
            );
            renderMergePreview(targetId, preview || {});
          } catch (err) {
            clearMergePreview();
            notify(
              `Kunne ikke hente forhåndsvisning for sammenslåing: ${err.message || "Ukjent feil"}`,
              true,
            );
          } finally {
            loadingPreview = false;
            mergeBtn.disabled = candidates.length === 0;
          }
        }

        mergeSelect.addEventListener("change", clearMergePreview);
        mergeForm.addEventListener("submit", requestMergePreview);
        mergeBtn.addEventListener("click", requestMergePreview);

        mergeForm.append(mergeSelect, mergeBtn);
        mergeSection.append(mergeForm, mergePreviewHost);
        card.append(mergeSection);
      }
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
        const statusMsg = `Oda-preferanser for «${currentProduct.display_name}» er lagret.`;
        renderCardContent(statusMsg);
        notify(statusMsg);
        onUpdated(currentProduct);
      } catch (err) {
        const errMsg = `Kunne ikke lagre preferanser: ${err.message || "Ukjent feil"}`;
        if (prefStatusBox) {
          prefStatusBox.textContent = errMsg;
          prefStatusBox.hidden = false;
        }
        notify(errMsg, true);
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
    let allProducts = [];

    async function loadProducts(query = "") {
      if (!api) return;
      resultsContainer.innerHTML = '<div class="loading">Henter råvarer …</div>';
      try {
        let url = `/catalogue/overview?locale=${encodeURIComponent(locale)}`;
        if (query.trim()) {
          url += `&q=${encodeURIComponent(query.trim())}`;
        }
        const products = await api(url);
        if (!query.trim() && Array.isArray(products)) {
          allProducts = products;
        }
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

        const candidatePool = allProducts.length > 0 ? allProducts : products;
        products.forEach((prod) => {
          const card = renderProductCard(prod, {
            api,
            notify,
            mergeCandidates: candidatePool.filter((p) => p.id !== prod.id),
            onUpdated: (updatedProd) => {
              if (updatedProd && updatedProd.id) {
                allProducts = allProducts.map((p) =>
                  p.id === updatedProd.id ? updatedProd : p,
                );
              }
            },
            onMerged: async () => {
              allProducts = [];
              await loadProducts(searchInput.value);
            },
            onAliasReassigned: async () => {
              allProducts = [];
              await loadProducts(searchInput.value);
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
    renderCuratedFoodsSection(curatedHost, {
      api,
      notify,
      getProducts: () => allProducts,
    });
    wrapper.append(curatedHost);
    container.append(wrapper);

    return {
      reload: () => loadProducts(searchInput.value),
      ready: initialLoad,
    };
  }

  function collectCuratedLinkCandidates(products) {
    const candidates = [];
    const seen = new Set();
    (Array.isArray(products) ? products : []).forEach((prod) => {
      if (!prod || !prod.id) return;
      const canonicalKey = `canonical:${prod.id}`;
      if (!seen.has(canonicalKey)) {
        seen.add(canonicalKey);
        const prodName = prod.display_name || prod.canonical_name;
        candidates.push({
          key: canonicalKey,
          label: `Råvare: ${prodName}`,
          provider: "canonical",
          product_id: String(prod.id),
          name: prodName,
        });
      }
      const addSku = (provider, skuId, skuName) => {
        const prov = String(provider || "oda").trim().toLowerCase();
        const pid = String(skuId || "").trim();
        if (!pid) return;
        const key = `${prov}:${pid}`;
        if (seen.has(key)) return;
        seen.add(key);
        const labelName = skuName || pid;
        candidates.push({
          key,
          label: `${formatProviderLabel(prov)}: ${labelName}`,
          provider: prov,
          product_id: pid,
          name: labelName,
        });
      };
      (prod.preferred_oda_skus || []).forEach((s) => addSku("oda", s.oda_product_id, s.name));
      (prod.remembered_oda_skus || []).forEach((s) =>
        addSku(s.provider || "oda", s.oda_product_id, s.name),
      );
      (prod.previously_purchased_skus || []).forEach((s) =>
        addSku(s.provider || "oda", s.sku_id, s.name),
      );
    });
    return candidates;
  }

  function renderCuratedFoodsSection(container, options = {}) {
    const { api, notify, getProducts } = options;
    const staticProducts = Array.isArray(options.products) ? options.products : null;
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
            translateFoodCategory(food.category) || null,
            food.default_unit ? `Standard enhet: ${formatUnitLabel(food.default_unit)}` : null,
          ].filter(Boolean);
          meta.textContent = parts.join(" · ");
          card.append(meta);

          const optionsHost = document.createElement("div");
          optionsHost.className = "stack curated-food-options";

          const loadOptsBtn = document.createElement("button");
          loadOptsBtn.type = "button";
          loadOptsBtn.className = "quiet small";
          loadOptsBtn.textContent = "Vis produktkoblinger";
          loadOptsBtn.setAttribute("aria-expanded", "false");
          loadOptsBtn.setAttribute(
            "aria-label",
            `Vis produktkoblinger for ${food.display_name || food.source_name}`,
          );

          async function refreshFoodOptions() {
            loadOptsBtn.disabled = true;
            try {
              const opts = await api(`/catalogue/foods/${encodeURIComponent(food.id)}/products`);
              optionsHost.replaceChildren();
              loadOptsBtn.setAttribute("aria-expanded", "true");
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
                  const optName = opt.snapshot?.name || opt.product_id;
                  label.textContent = `${optName} (${formatProviderLabel(opt.provider)})${opt.preferred ? " ★ Foretrukket" : ""}`;
                  const toggleBtn = document.createElement("button");
                  toggleBtn.type = "button";
                  toggleBtn.className = "quiet small";
                  toggleBtn.textContent = opt.preferred ? "Fjern foretrukket" : "Velg som foretrukket";
                  toggleBtn.setAttribute("aria-pressed", opt.preferred ? "true" : "false");
                  toggleBtn.setAttribute(
                    "aria-label",
                    `${opt.preferred ? "Fjern foretrukket" : "Velg som foretrukket"}: ${optName}`,
                  );
                  toggleBtn.addEventListener("click", async () => {
                    await api(
                      `/catalogue/products/${encodeURIComponent(opt.id)}/preferred`,
                      opt.preferred ? "DELETE" : "PUT",
                    );
                    if (notify) notify("Produktpreferanse oppdatert.");
                    await refreshFoodOptions();
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
          }

          loadOptsBtn.addEventListener("click", refreshFoodOptions);
          card.append(loadOptsBtn, optionsHost);

          if (typeof api === "function") {
            const linkForm = document.createElement("form");
            linkForm.className = "curated-food-link-form";

            const candidates = collectCuratedLinkCandidates(
              typeof getProducts === "function" ? getProducts() : staticProducts,
            );

            const linkSelect = document.createElement("select");
            linkSelect.className = "curated-food-link-select";
            linkSelect.setAttribute(
              "aria-label",
              `Velg råvare eller butikkprodukt å koble til ${food.display_name || food.source_name}`,
            );

            const manualBox = document.createElement("div");
            manualBox.className = "curated-food-link-manual";

            if (candidates.length > 0) {
              const placeholderOpt = document.createElement("option");
              placeholderOpt.value = "";
              placeholderOpt.textContent = "Koble til råvare eller butikkprodukt …";
              linkSelect.append(placeholderOpt);

              candidates.forEach((cand) => {
                const opt = document.createElement("option");
                opt.value = cand.key;
                opt.textContent = cand.label;
                linkSelect.append(opt);
              });
            }

            const customOpt = document.createElement("option");
            customOpt.value = "__custom__";
            customOpt.textContent = "Annet butikkprodukt (skriv inn) …";
            linkSelect.append(customOpt);

            if (candidates.length === 0) {
              linkSelect.value = "__custom__";
            }

            const providerSelect = document.createElement("select");
            providerSelect.className = "curated-food-provider-select";
            providerSelect.setAttribute("aria-label", "Leverandør eller butikk");
            [
              ["oda", "Oda"],
              ["rema1000", "REMA 1000"],
              ["coop", "Coop"],
              ["trumf", "Trumf"],
              ["manual", "Annen / manuell"],
            ].forEach(([val, text]) => {
              const opt = document.createElement("option");
              opt.value = val;
              opt.textContent = text;
              providerSelect.append(opt);
            });

            const productIdInput = document.createElement("input");
            productIdInput.type = "text";
            productIdInput.className = "curated-food-product-id-input";
            productIdInput.placeholder = "Produkt-ID / SKU";
            productIdInput.setAttribute("aria-label", "Produkt-ID for kobling");

            const productNameInput = document.createElement("input");
            productNameInput.type = "text";
            productNameInput.className = "curated-food-product-name-input";
            productNameInput.placeholder = "Produktnavn (f.eks. Tine Helmelk 1L)";
            productNameInput.setAttribute("aria-label", "Produktnavn for kobling");

            const syncManualVisibility = () => {
              const showManual = candidates.length === 0 || linkSelect.value === "__custom__";
              manualBox.hidden = !showManual;
            };
            syncManualVisibility();
            linkSelect.addEventListener("change", syncManualVisibility);

            manualBox.append(providerSelect, productIdInput, productNameInput);

            const linkBtn = document.createElement("button");
            linkBtn.type = "submit";
            linkBtn.className = "quiet small curated-food-link-btn";
            linkBtn.textContent = "Koble produkt";

            let linking = false;
            async function submitLink(e) {
              if (e && typeof e.preventDefault === "function") e.preventDefault();
              if (linking) return;
              const selectedVal = String(linkSelect.value || "").trim();
              let payload = null;
              if (selectedVal && selectedVal !== "__custom__") {
                const cand = candidates.find((c) => c.key === selectedVal);
                if (cand) {
                  payload = {
                    provider: cand.provider,
                    product_id: cand.product_id,
                    name: cand.name,
                    observed_at: new Date().toISOString(),
                    evidence_status: "unverified",
                  };
                }
              } else if (selectedVal === "__custom__" || candidates.length === 0) {
                const pid = String(productIdInput.value || "").trim();
                const pname = String(productNameInput.value || "").trim();
                const prov = String(providerSelect.value || "oda").trim() || "oda";
                if (!pid || !pname) {
                  if (notify) notify("Oppgi både produkt-ID og produktnavn for å koble.", true);
                  return;
                }
                payload = {
                  provider: prov,
                  product_id: pid,
                  name: pname,
                  observed_at: new Date().toISOString(),
                  evidence_status: "unverified",
                };
              }
              if (!payload) return;
              linking = true;
              linkBtn.disabled = true;
              try {
                await api(
                  `/catalogue/foods/${encodeURIComponent(food.id)}/products`,
                  "POST",
                  payload,
                );
                if (notify) {
                  notify(
                    `Koblet «${payload.name}» til «${food.display_name || food.source_name}».`,
                  );
                }
                productIdInput.value = "";
                productNameInput.value = "";
                if (candidates.length > 0) {
                  linkSelect.value = "";
                  syncManualVisibility();
                }
                await refreshFoodOptions();
              } catch (err) {
                if (notify) {
                  notify(`Kunne ikke koble produkt: ${err.message || "Ukjent feil"}`, true);
                }
              } finally {
                linking = false;
                linkBtn.disabled = false;
              }
            }

            linkForm.addEventListener("submit", submitLink);
            linkForm.append(linkSelect, manualBox, linkBtn);
            card.append(linkForm);
          }

          listEl.append(card);
        });
      } catch (err) {
        listEl.replaceChildren();
        const errP = document.createElement("p");
        errP.className = "callout error";
        errP.setAttribute("role", "alert");
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
    formatUnitLabel,
    formatLocaleBadge,
    formatStockSummary,
    formatFrequencyDisplay,
    formatFrequencySummary,
    formatProvenanceCounts,
    formatProviderLabel,
    translateFoodCategory,
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
