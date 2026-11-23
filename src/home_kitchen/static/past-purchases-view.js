(function (root) {
  "use strict";

  function formatProviderLabel(provider) {
    const norm = String(provider || "").trim().toLowerCase();
    if (norm === "oda") return "Oda";
    if (norm === "coop") return "Coop";
    if (norm === "trumf") return "Trumf";
    if (norm === "rema1000") return "REMA 1000";
    if (norm === "kitchen_history" || norm === "store") return "Manuell handletur";
    return provider || "Butikk";
  }

  function formatDateNb(isoValue, includeTime = false) {
    if (!isoValue) return "Ukjent dato";
    const dt = isoValue instanceof Date ? isoValue : new Date(isoValue);
    if (Number.isNaN(dt.getTime())) return "Ukjent dato";
    try {
      const opts = includeTime
        ? { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }
        : { day: "numeric", month: "short", year: "numeric" };
      return new Intl.DateTimeFormat("nb-NO", opts).format(dt);
    } catch {
      return dt.toISOString().slice(0, 10);
    }
  }

  function formatCurrencyNb(amount, currency = "NOK") {
    if (amount == null || amount === "") return "Sum ikke oppgitt";
    const num = Number(amount);
    if (Number.isNaN(num)) return String(amount);
    try {
      const formatted = new Intl.NumberFormat("nb-NO", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(num);
      return currency && currency !== "NOK" ? `${formatted} ${currency}` : `${formatted} kr`;
    } catch {
      return `${num.toFixed(2).replace(".", ",")} kr`;
    }
  }

  function formatQuantityNb(quantity, unit) {
    if (quantity == null || quantity === "") return unit ? String(unit) : "—";
    const num = Number(quantity);
    let qtyText;
    if (Number.isNaN(num)) {
      qtyText = String(quantity);
    } else {
      try {
        qtyText = new Intl.NumberFormat("nb-NO", { maximumFractionDigits: 2 }).format(num);
      } catch {
        qtyText = String(num).replace(".", ",");
      }
    }
    return unit ? `${qtyText} ${unit}` : qtyText;
  }

  function formatFrequencyBadgeText(frequency) {
    if (!frequency || frequency.status !== "known") {
      return "Ukjent frekvens";
    }
    const interval = frequency.recency_weighted_interval_days ?? frequency.average_interval_days;
    if (interval == null || Number.isNaN(Number(interval))) {
      return "Ukjent frekvens";
    }
    const num = Number(interval);
    if (num === 1) return "ca. hver dag";
    const formatted = String(num).replace(".", ",");
    return `ca. hver ${formatted}. dag`;
  }

  function formatFrequencyDetailText(frequency) {
    if (!frequency || frequency.status !== "known") {
      const count = frequency?.sample_size || 0;
      return count === 1 ? "1 registrert kjøp (trenger minst 2 kjøp over tid)" : "For få registrerte kjøp til å anslå frekvens";
    }
    const parts = [];
    if (frequency.sample_size) {
      parts.push(`${frequency.sample_size} kjøp`);
    }
    if (frequency.weighted_purchases_per_30_days != null) {
      const per30 = String(frequency.weighted_purchases_per_30_days).replace(".", ",");
      parts.push(`ca. ${per30} per 30 dager (vektet mot nyere kjøp)`);
    }
    if (frequency.coverage_days != null) {
      parts.push(`nedre grense over ${frequency.coverage_days} dager`);
    }
    return parts.join(" · ");
  }

  function formatProvenanceLabel(provenance) {
    return provenance === "user_confirmed" ? "Registrert handletur" : "Bekreftet kvittering";
  }

  function toIsoDateOnly(isoValue) {
    if (!isoValue) return "";
    const dt = isoValue instanceof Date ? isoValue : new Date(isoValue);
    if (Number.isNaN(dt.getTime())) return String(isoValue).slice(0, 10);
    return dt.toISOString().slice(0, 10);
  }

  function filterPurchaseData(data, filters = {}) {
    const baskets = Array.isArray(data?.baskets) ? data.baskets : [];
    const rows = Array.isArray(data?.rows) ? data.rows : [];
    const products = Array.isArray(data?.products) ? data.products : [];

    const q = String(filters.q || "").trim().toLowerCase();
    const provider = String(filters.provider || "").trim().toLowerCase();
    const store = String(filters.store || "").trim().toLowerCase();
    const category = String(filters.category || "").trim().toLowerCase();
    const dateFrom = String(filters.dateFrom || "").trim();
    const dateTo = String(filters.dateTo || "").trim();

    const matchesRow = (row) => {
      if (provider && String(row.provider || "").trim().toLowerCase() !== provider) return false;
      if (store && String(row.store_name || "").trim().toLowerCase() !== store) return false;
      if (category) {
        const catKey = String(row.category || "").trim().toLowerCase();
        const catLbl = String(row.category_label || "").trim().toLowerCase();
        if (catKey !== category && catLbl !== category) return false;
      }
      const rowDate = toIsoDateOnly(row.purchased_at);
      if (dateFrom && (!rowDate || rowDate < dateFrom)) return false;
      if (dateTo && (!rowDate || rowDate > dateTo)) return false;
      if (q) {
        const hay = [
          row.product_name,
          row.raw_name,
          row.store_name,
          row.provider_label,
          row.category_label,
          row.ean,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    };

    const filteredRows = rows.filter(matchesRow);
    const matchingBasketIds = new Set(filteredRows.map((r) => r.basket_id));

    const filteredBaskets = baskets
      .filter((b) => {
        if (provider && String(b.provider || "").trim().toLowerCase() !== provider) return false;
        if (store && String(b.store_name || "").trim().toLowerCase() !== store) return false;
        const bDate = toIsoDateOnly(b.purchased_at);
        if (dateFrom && (!bDate || bDate < dateFrom)) return false;
        if (dateTo && (!bDate || bDate > dateTo)) return false;
        if ((q || category) && rows.length > 0 && !matchingBasketIds.has(b.id)) return false;
        return true;
      })
      .map((b) => {
        if (!q && !category) return b;
        const matchedLines = (b.lines || []).filter((line) => {
          if (category) {
            const cKey = String(line.category || "").trim().toLowerCase();
            const cLbl = String(line.category_label || "").trim().toLowerCase();
            if (cKey !== category && cLbl !== category) return false;
          }
          if (q) {
            const hay = [
              line.name,
              line.raw_name,
              line.canonical_product_name,
              line.category_label,
              line.ean,
              b.store_name,
              b.provider_label,
            ]
              .filter(Boolean)
              .join(" ")
              .toLowerCase();
            if (!hay.includes(q)) return false;
          }
          return true;
        });
        return { ...b, lines: matchedLines, item_count: matchedLines.length };
      })
      .filter((b) => (b.lines ? b.lines.length > 0 : true));

    const matchingProductKeys = new Set(
      filteredRows.map((r) =>
        r.canonical_product_id
          ? `pid:${r.canonical_product_id}`
          : `name:${String(r.product_name || "").trim().toLowerCase().replace(/\s+/g, " ")}`,
      ),
    );
    const hasFilter = Boolean(q || provider || store || category || dateFrom || dateTo);

    const filteredProducts = products.filter((p) => {
      if (!hasFilter) return true;
      if (rows.length > 0) {
        return matchingProductKeys.has(p.key);
      }
      if (provider && !(p.providers || []).some((pr) => pr.toLowerCase() === provider)) return false;
      if (store && !(p.stores || []).some((st) => st.toLowerCase() === store)) return false;
      if (category) {
        const cKey = String(p.category || "").toLowerCase();
        const cLbl = String(p.category_label || "").toLowerCase();
        if (cKey !== category && cLbl !== category) return false;
      }
      if (q && !String(p.product_name || "").toLowerCase().includes(q)) return false;
      return true;
    });

    return {
      baskets: filteredBaskets,
      rows: filteredRows,
      products: filteredProducts,
      hasFilter,
    };
  }

  function groupProductsByCategory(products) {
    const groups = new Map();
    (Array.isArray(products) ? products : []).forEach((prod) => {
      const catKey = prod.category || "Other";
      const catLabel = prod.category_label || "Annet";
      if (!groups.has(catKey)) {
        groups.set(catKey, {
          category: catKey,
          category_label: catLabel,
          products: [],
          total_purchases: 0,
        });
      }
      const grp = groups.get(catKey);
      grp.products.push(prod);
      grp.total_purchases += Number(prod.purchase_count || 0);
    });
    return [...groups.values()].sort(
      (a, b) =>
        b.total_purchases - a.total_purchases ||
        a.category_label.localeCompare(b.category_label, "nb"),
    );
  }

  function sortPurchaseRows(rows, sortBy = "purchased_at", sortDir = "desc") {
    const dir = sortDir === "asc" ? 1 : -1;
    const copy = [...(Array.isArray(rows) ? rows : [])];
    copy.sort((a, b) => {
      let cmp = 0;
      if (sortBy === "purchased_at") {
        const ta = a.purchased_at ? new Date(a.purchased_at).getTime() : 0;
        const tb = b.purchased_at ? new Date(b.purchased_at).getTime() : 0;
        cmp = ta - tb;
      } else if (sortBy === "amount" || sortBy === "quantity") {
        const na = a[sortBy] != null && a[sortBy] !== "" ? Number(a[sortBy]) : -Infinity;
        const nb = b[sortBy] != null && b[sortBy] !== "" ? Number(b[sortBy]) : -Infinity;
        cmp = na - nb;
      } else {
        const sa = String(a[sortBy] || "");
        const sb = String(b[sortBy] || "");
        cmp = sa.localeCompare(sb, "nb", { sensitivity: "base" });
      }
      if (cmp !== 0) return cmp * dir;
      const ta = a.purchased_at ? new Date(a.purchased_at).getTime() : 0;
      const tb = b.purchased_at ? new Date(b.purchased_at).getTime() : 0;
      return tb - ta;
    });
    return copy;
  }

  function makeEl(tag, className, textContent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (textContent !== undefined && textContent !== null) node.textContent = textContent;
    return node;
  }

  function renderPastPurchasesView(container, options = {}) {
    if (!container) return null;

    const api = options.api;
    const notify = options.notify || (() => {});
    const onNavigate = options.onNavigate || (() => {});

    container.replaceChildren();

    const wrapper = makeEl("div", "past-purchases-view");

    // Header
    const header = makeEl("div", "header purchase-history-header");
    const headerCopy = makeEl("div", "purchase-header-copy");
    headerCopy.append(
      makeEl("h1", null, "Tidligere kjøp"),
      makeEl(
        "p",
        "muted",
        "Utforsk kvitteringer, handlekurver og vektet kjøpsfrekvens på tvers av tilkoblede butikker.",
      ),
    );
    const headerActions = makeEl("div", "actions");
    const refreshBtn = makeEl("button", "quiet", "Oppdater");
    refreshBtn.type = "button";
    const settingsBtn = makeEl("button", "quiet", "Butikkoblinger");
    settingsBtn.type = "button";
    settingsBtn.addEventListener("click", () => onNavigate("settings"));
    headerActions.append(refreshBtn, settingsBtn);
    header.append(headerCopy, headerActions);
    wrapper.append(header);

    // Coverage & lower-bound callout
    const coverageBox = makeEl("div", "callout purchase-coverage-callout");
    const coverageText = makeEl(
      "p",
      "purchase-coverage-note",
      "Vist kjøpshistorikk og frekvens er en nedre grense basert på tilgjengelige kvitteringer (vektet mot nyere kjøp). Kjøpshistorikk viser ikke nåværende lager hjemme.",
    );
    const providerWindowChips = makeEl("div", "purchase-window-chips");
    coverageBox.append(coverageText, providerWindowChips);
    wrapper.append(coverageBox);

    // 3 visualization mode tabs
    const modeNav = makeEl("div", "section-tabs purchase-mode-tabs");
    modeNav.setAttribute("role", "tablist");
    modeNav.setAttribute("aria-label", "Visningsmåte for tidligere kjøp");

    const MODES = [
      { id: "baskets", label: "Kvitteringer og handlekurver" },
      { id: "products", label: "Produkter og kategorier" },
      { id: "table", label: "Tabell med filtre" },
    ];
    let activeMode = options.initialMode || "baskets";
    const modeButtons = new Map();

    // Filter state
    const filterState = {
      q: "",
      provider: "",
      store: "",
      category: "",
      dateFrom: "",
      dateTo: "",
    };
    let sortBy = "purchased_at";
    let sortDir = "desc";
    let loadedData = null;

    MODES.forEach((m) => {
      const btn = makeEl("button", "purchase-mode-tab", m.label);
      btn.type = "button";
      btn.setAttribute("role", "tab");
      btn.dataset.mode = m.id;
      btn.addEventListener("click", () => {
        if (activeMode === m.id) return;
        activeMode = m.id;
        syncModeTabs();
        renderActiveContent();
      });
      modeButtons.set(m.id, btn);
      modeNav.append(btn);
    });
    wrapper.append(modeNav);

    function syncModeTabs() {
      modeButtons.forEach((btn, id) => {
        const selected = id === activeMode;
        btn.className = selected ? "purchase-mode-tab primary" : "purchase-mode-tab quiet";
        btn.setAttribute("aria-selected", selected ? "true" : "false");
        if (selected) btn.setAttribute("aria-current", "page");
        else btn.removeAttribute("aria-current");
      });
    }
    syncModeTabs();

    // Shared multi-criteria filters panel
    const filterPanel = makeEl("section", "panel purchase-filters-panel");
    filterPanel.setAttribute("aria-label", "Filtrer tidligere kjøp");

    const filterGrid = makeEl("div", "purchase-filter-grid");

    function makeLabeledControl(id, labelText, controlEl) {
      const wrap = makeEl("div", "purchase-filter-field");
      const label = makeEl("label", null, labelText);
      label.setAttribute("for", id);
      controlEl.id = id;
      wrap.append(label, controlEl);
      return wrap;
    }

    const searchInput = makeEl("input", "purchase-filter-search");
    searchInput.type = "search";
    searchInput.placeholder = "Vare, butikk eller EAN …";

    const providerSelect = makeEl("select", "purchase-filter-provider");
    const storeSelect = makeEl("select", "purchase-filter-store");
    const categorySelect = makeEl("select", "purchase-filter-category");

    const dateFromInput = makeEl("input", "purchase-filter-date-from");
    dateFromInput.type = "date";

    const dateToInput = makeEl("input", "purchase-filter-date-to");
    dateToInput.type = "date";

    filterGrid.append(
      makeLabeledControl("purchase-filter-q", "Søk", searchInput),
      makeLabeledControl("purchase-filter-provider", "Leverandør", providerSelect),
      makeLabeledControl("purchase-filter-store", "Butikk", storeSelect),
      makeLabeledControl("purchase-filter-category", "Kategori", categorySelect),
      makeLabeledControl("purchase-filter-from", "Fra dato", dateFromInput),
      makeLabeledControl("purchase-filter-to", "Til dato", dateToInput),
    );

    const filterActions = makeEl("div", "purchase-filter-actions");
    const resetBtn = makeEl("button", "quiet small purchase-filter-reset", "Nullstill filtre");
    resetBtn.type = "button";
    resetBtn.hidden = true;
    const summaryLabel = makeEl("span", "muted purchase-filter-summary");
    summaryLabel.setAttribute("role", "status");
    summaryLabel.setAttribute("aria-live", "polite");
    filterActions.append(resetBtn, summaryLabel);

    filterPanel.append(filterGrid, filterActions);
    wrapper.append(filterPanel);

    // Content host
    const contentHost = makeEl("div", "purchase-history-content");
    contentHost.append(makeEl("div", "loading", "Henter kjøpshistorikk …"));
    wrapper.append(contentHost);

    container.append(wrapper);

    function populateFilterSelects(data) {
      const fillSelect = (selectEl, defaultLabel, optionsList, currentVal) => {
        selectEl.replaceChildren();
        const defOpt = document.createElement("option");
        defOpt.value = "";
        defOpt.textContent = defaultLabel;
        selectEl.append(defOpt);
        optionsList.forEach(({ value, label }) => {
          const opt = document.createElement("option");
          opt.value = value;
          opt.textContent = label;
          selectEl.append(opt);
        });
        selectEl.value = currentVal || "";
      };

      const provOpts = (data?.providers || []).map((p) => ({
        value: p.key,
        label: p.label || formatProviderLabel(p.key),
      }));
      const storeOpts = (data?.stores || []).map((s) => ({ value: s, label: s }));
      const catOpts = (data?.categories || []).map((c) => ({
        value: c.key,
        label: c.label || c.key,
      }));

      fillSelect(providerSelect, "Alle leverandører", provOpts, filterState.provider);
      fillSelect(storeSelect, "Alle butikker", storeOpts, filterState.store);
      fillSelect(categorySelect, "Alle kategorier", catOpts, filterState.category);
    }

    function renderProviderWindowChips(data) {
      providerWindowChips.replaceChildren();
      const windows = data?.provider_windows || {};
      const keys = Object.keys(windows).sort();
      if (keys.length === 0) return;
      keys.forEach((key) => {
        const win = windows[key];
        const days = win.observation_days ?? 0;
        const count = win.total_records ?? 0;
        const capTxt = win.retained_months ? ` · maks ${win.retained_months} mnd` : "";
        const chip = makeEl(
          "span",
          "badge purchase-window-chip",
          `${formatProviderLabel(key)}: ${count} kjøp over ${days} dager${capTxt}`,
        );
        providerWindowChips.append(chip);
      });
    }

    function resetFilters() {
      filterState.q = "";
      filterState.provider = "";
      filterState.store = "";
      filterState.category = "";
      filterState.dateFrom = "";
      filterState.dateTo = "";
      searchInput.value = "";
      providerSelect.value = "";
      storeSelect.value = "";
      categorySelect.value = "";
      dateFromInput.value = "";
      dateToInput.value = "";
      renderActiveContent();
    }
    resetBtn.addEventListener("click", resetFilters);

    function renderEmptyState(hasFilter) {
      const empty = makeEl("div", "empty purchase-empty-state");
      if (hasFilter) {
        empty.append(
          makeEl("h2", null, "Ingen kjøp matcher filtrene"),
          makeEl("p", null, "Prøv å utvide datoområdet eller nullstille filtrene."),
        );
        const btn = makeEl("button", "primary", "Nullstill filtre");
        btn.type = "button";
        btn.addEventListener("click", resetFilters);
        empty.append(btn);
      } else {
        empty.append(
          makeEl("h2", null, "Ingen kjøpshistorikk funnet ennå"),
          makeEl(
            "p",
            null,
            "Koble til Oda, Coop, Trumf eller REMA 1000 i innstillingene, eller registrer en handletur fra handlelisten.",
          ),
        );
        const btn = makeEl("button", "primary", "Åpne butikkoblinger");
        btn.type = "button";
        btn.addEventListener("click", () => onNavigate("settings"));
        empty.append(btn);
      }
      return empty;
    }

    function renderBasketsMode(baskets) {
      const section = makeEl("section", "purchase-baskets-view");
      section.setAttribute("aria-label", "Kvitteringer og handlekurver");

      baskets.forEach((basket) => {
        const card = makeEl("article", "card purchase-basket-card");
        card.dataset.basketId = basket.id;

        const topRow = makeEl("div", "purchase-basket-top");
        const identity = makeEl("div", "purchase-basket-identity");
        const titleEl = makeEl(
          "h2",
          "purchase-basket-store",
          basket.store_name || basket.provider_label || formatProviderLabel(basket.provider),
        );
        const badges = makeEl("div", "purchase-basket-badges");
        badges.append(
          makeEl(
            "span",
            "badge purchase-provider-badge",
            basket.provider_label || formatProviderLabel(basket.provider),
          ),
          makeEl("span", "badge purchase-provenance-badge", formatProvenanceLabel(basket.provenance)),
        );
        if (basket.is_correction) {
          badges.append(makeEl("span", "badge purchase-correction-badge", "Korrigert kvittering"));
        }
        identity.append(titleEl, badges);

        const meta = makeEl("div", "purchase-basket-meta");
        meta.append(
          makeEl("span", "purchase-basket-date", formatDateNb(basket.purchased_at)),
          makeEl("strong", "purchase-basket-total", formatCurrencyNb(basket.total_amount, basket.currency)),
          makeEl(
            "span",
            "muted purchase-basket-count",
            `${basket.item_count} ${basket.item_count === 1 ? "vare" : "varer"}`,
          ),
        );

        topRow.append(identity, meta);
        card.append(topRow);

        const details = makeEl("details", "purchase-basket-details");
        const summary = makeEl(
          "summary",
          "purchase-basket-summary",
          `Vis varelinjer (${(basket.lines || []).length})`,
        );
        details.append(summary);

        const lineList = makeEl("ul", "purchase-basket-lines");
        (basket.lines || []).forEach((line) => {
          const li = makeEl("li", "purchase-basket-line");
          const lineLeft = makeEl("div", "purchase-line-main");
          lineLeft.append(
            makeEl("span", "purchase-line-name", line.canonical_product_name || line.name),
          );
          lineLeft.append(makeEl("span", "badge purchase-category-badge", line.category_label || "Annet"));

          const hasQuantity = line.quantity != null && String(line.quantity).trim() !== "";
          const unitText = String(line.unit ?? "").trim();
          const quantityText = hasQuantity
            ? formatQuantityNb(line.quantity, unitText)
            : "Mengde ikke oppgitt";
          const lineQuantity = makeEl("div", "purchase-line-purchased");
          lineQuantity.append(
            makeEl("span", "purchase-line-quantity-label", "Kjøpt"),
            makeEl("strong", "purchase-line-qty", quantityText),
          );
          if (hasQuantity && !unitText) {
            lineQuantity.append(makeEl("span", "purchase-line-missing-unit", "Enhet ikke oppgitt"));
          }
          lineQuantity.querySelector(".purchase-line-qty").setAttribute(
            "aria-label",
            `Kjøpt mengde: ${quantityText}`,
          );

          const metadata = [];
          if (line.amount != null) {
            metadata.push(["Linjesum", formatCurrencyNb(line.amount, basket.currency)]);
          }
          if (line.ean) metadata.push(["EAN", String(line.ean)]);
          if (line.raw_name && line.raw_name !== (line.canonical_product_name || line.name)) {
            metadata.push(["Tekst på kvitteringen", String(line.raw_name)]);
          }

          li.append(lineLeft, lineQuantity);
          if (metadata.length) {
            const lineDetails = makeEl("details", "purchase-line-details");
            lineDetails.append(makeEl("summary", "purchase-line-details-summary", "Flere detaljer"));
            const metadataList = makeEl("dl", "purchase-line-metadata");
            metadata.forEach(([label, value]) => {
              metadataList.append(makeEl("dt", "purchase-line-metadata-label", label));
              metadataList.append(makeEl("dd", "purchase-line-metadata-value", value));
            });
            lineDetails.append(metadataList);
            li.append(lineDetails);
          }
          lineList.append(li);
        });
        details.append(lineList);
        card.append(details);
        section.append(card);
      });

      return section;
    }

    function renderProductsMode(products) {
      const section = makeEl("section", "purchase-products-view");
      section.setAttribute("aria-label", "Produkter og kategorier");

      const categoryGroups = groupProductsByCategory(products);
      categoryGroups.forEach((grp) => {
        const catSection = makeEl("section", "panel purchase-category-group");
        catSection.dataset.category = grp.category;

        const catHeader = makeEl("div", "purchase-category-header");
        catHeader.append(
          makeEl("h2", "purchase-category-title", grp.category_label),
          makeEl(
            "span",
            "muted purchase-category-stats",
            `${grp.products.length} ${grp.products.length === 1 ? "produkt" : "produkter"} · ${grp.total_purchases} kjøp`,
          ),
        );
        catSection.append(catHeader);

        const prodGrid = makeEl("div", "purchase-product-grid");
        grp.products.forEach((prod) => {
          const card = makeEl("article", "card purchase-product-card");
          card.dataset.productKey = prod.key;

          const cardHead = makeEl("div", "purchase-product-head");
          cardHead.append(makeEl("h3", "purchase-product-name", prod.product_name));
          const provBadges = makeEl("div", "purchase-product-providers");
          (prod.providers || []).forEach((prov) => {
            provBadges.append(makeEl("span", "badge purchase-provider-badge", formatProviderLabel(prov)));
          });
          cardHead.append(provBadges);
          card.append(cardHead);

          // Last purchased + Frequency
          const freqRow = makeEl("div", "purchase-product-frequency");
          freqRow.append(
            makeEl("span", "badge frequency-badge", formatFrequencyBadgeText(prod.frequency)),
            makeEl(
              "span",
              "muted purchase-last-date",
              `Sist kjøpt: ${formatDateNb(prod.last_purchased_at)}`,
            ),
          );
          card.append(freqRow);

          const freqDetails = makeEl(
            "p",
            "muted purchase-product-freq-details",
            formatFrequencyDetailText(prod.frequency),
          );
          card.append(freqDetails);

          // Item summary
          const summaryParts = [`${prod.purchase_count} kjøp`];
          if (prod.total_quantity != null) {
            summaryParts.push(`Totalt ${formatQuantityNb(prod.total_quantity, prod.unit)}`);
          }
          if (prod.total_spent != null) {
            summaryParts.push(formatCurrencyNb(prod.total_spent));
          }
          if (Array.isArray(prod.stores) && prod.stores.length > 0) {
            summaryParts.push(`Butikker: ${prod.stores.join(", ")}`);
          }
          card.append(makeEl("p", "purchase-product-summary", summaryParts.join(" · ")));

          if (Array.isArray(prod.recent_items) && prod.recent_items.length > 0) {
            const recentDetails = makeEl("details", "purchase-product-recent");
            recentDetails.append(
              makeEl("summary", null, `Siste kjøp (${prod.recent_items.length})`),
            );
            const ul = makeEl("ul", "purchase-recent-list");
            prod.recent_items.forEach((item) => {
              const li = makeEl("li", "purchase-recent-item");
              const txt = [
                formatDateNb(item.purchased_at),
                item.store_name || item.provider_label || formatProviderLabel(item.provider),
                formatQuantityNb(item.quantity, item.unit),
                item.amount != null ? formatCurrencyNb(item.amount) : null,
              ]
                .filter((x) => x && x !== "—")
                .join(" · ");
              li.textContent = txt;
              ul.append(li);
            });
            recentDetails.append(ul);
            card.append(recentDetails);
          }

          prodGrid.append(card);
        });

        catSection.append(prodGrid);
        section.append(catSection);
      });

      return section;
    }

    function renderTableMode(rows) {
      const section = makeEl("section", "purchase-table-view");
      section.setAttribute("aria-label", "Tabell over kjøpslinjer");

      const sortedRows = sortPurchaseRows(rows, sortBy, sortDir);

      const tableWrap = makeEl("div", "purchase-table-wrap");
      const table = makeEl("table", "purchase-table");

      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");

      const COLUMNS = [
        { key: "purchased_at", label: "Dato" },
        { key: "product_name", label: "Vare" },
        { key: "category_label", label: "Kategori" },
        { key: "provider_label", label: "Leverandør" },
        { key: "store_name", label: "Butikk" },
        { key: "quantity", label: "Mengde" },
        { key: "amount", label: "Beløp" },
      ];

      COLUMNS.forEach((col) => {
        const th = document.createElement("th");
        th.setAttribute("scope", "col");
        const isCurrent = sortBy === col.key;
        th.setAttribute(
          "aria-sort",
          isCurrent ? (sortDir === "asc" ? "ascending" : "descending") : "none",
        );
        const btn = makeEl("button", "quiet small purchase-sort-btn");
        btn.type = "button";
        btn.dataset.sortKey = col.key;
        const arrow = isCurrent ? (sortDir === "asc" ? " ↑" : " ↓") : "";
        btn.textContent = `${col.label}${arrow}`;
        btn.setAttribute(
          "aria-label",
          `Sorter etter ${col.label} (${isCurrent && sortDir === "asc" ? "stigende" : "synkende"})`,
        );
        btn.addEventListener("click", () => {
          if (sortBy === col.key) {
            sortDir = sortDir === "asc" ? "desc" : "asc";
          } else {
            sortBy = col.key;
            sortDir = col.key === "purchased_at" || col.key === "amount" ? "desc" : "asc";
          }
          renderActiveContent();
        });
        th.append(btn);
        headRow.append(th);
      });
      thead.append(headRow);
      table.append(thead);

      const tbody = document.createElement("tbody");
      let totalVisibleAmount = 0;
      let hasAmount = false;

      sortedRows.forEach((row) => {
        const tr = document.createElement("tr");
        if (row.amount != null && row.amount !== "" && !Number.isNaN(Number(row.amount))) {
          totalVisibleAmount += Number(row.amount);
          hasAmount = true;
        }
        const cells = [
          formatDateNb(row.purchased_at),
          row.product_name,
          row.category_label || "Annet",
          row.provider_label || formatProviderLabel(row.provider),
          row.store_name || "—",
          formatQuantityNb(row.quantity, row.unit),
          row.amount != null ? formatCurrencyNb(row.amount) : "—",
        ];
        cells.forEach((val) => {
          const td = document.createElement("td");
          td.textContent = val;
          tr.append(td);
        });
        tbody.append(tr);
      });
      table.append(tbody);
      tableWrap.append(table);
      section.append(tableWrap);

      const footer = makeEl(
        "p",
        "muted purchase-table-footer",
        `Viser ${sortedRows.length} varelinjer` +
          (hasAmount ? ` · Sum viste beløp: ${formatCurrencyNb(totalVisibleAmount)}` : ""),
      );
      section.append(footer);

      return section;
    }

    function renderActiveContent() {
      contentHost.replaceChildren();
      if (!loadedData) return;

      const filtered = filterPurchaseData(loadedData, filterState);
      resetBtn.hidden = !filtered.hasFilter;

      if (activeMode === "baskets") {
        summaryLabel.textContent = `${filtered.baskets.length} kvitteringer / handlekurver`;
        if (filtered.baskets.length === 0) {
          contentHost.append(renderEmptyState(filtered.hasFilter));
          return;
        }
        contentHost.append(renderBasketsMode(filtered.baskets));
      } else if (activeMode === "products") {
        summaryLabel.textContent = `${filtered.products.length} produkter`;
        if (filtered.products.length === 0) {
          contentHost.append(renderEmptyState(filtered.hasFilter));
          return;
        }
        contentHost.append(renderProductsMode(filtered.products));
      } else {
        summaryLabel.textContent = `${filtered.rows.length} varelinjer`;
        if (filtered.rows.length === 0) {
          contentHost.append(renderEmptyState(filtered.hasFilter));
          return;
        }
        contentHost.append(renderTableMode(filtered.rows));
      }
    }

    let searchTimer = null;
    searchInput.addEventListener("input", (e) => {
      filterState.q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(renderActiveContent, 120);
    });
    providerSelect.addEventListener("change", (e) => {
      filterState.provider = e.target.value;
      renderActiveContent();
    });
    storeSelect.addEventListener("change", (e) => {
      filterState.store = e.target.value;
      renderActiveContent();
    });
    categorySelect.addEventListener("change", (e) => {
      filterState.category = e.target.value;
      renderActiveContent();
    });
    dateFromInput.addEventListener("change", (e) => {
      filterState.dateFrom = e.target.value;
      renderActiveContent();
    });
    dateToInput.addEventListener("change", (e) => {
      filterState.dateTo = e.target.value;
      renderActiveContent();
    });

    async function loadPurchaseHistory() {
      if (!api) return;
      refreshBtn.disabled = true;
      contentHost.replaceChildren(makeEl("div", "loading", "Henter kjøpshistorikk …"));
      try {
        const data = await api("/purchase-history");
        loadedData = data;
        populateFilterSelects(data);
        renderProviderWindowChips(data);
        renderActiveContent();
      } catch (err) {
        contentHost.replaceChildren();
        const errBox = makeEl(
          "div",
          "callout error",
          `Kunne ikke laste kjøpshistorikk: ${err?.message || "Ukjent feil"}`,
        );
        errBox.setAttribute("role", "alert");
        contentHost.append(errBox);
        if (notify) notify(`Kunne ikke laste kjøpshistorikk: ${err?.message || "Ukjent feil"}`, true);
      } finally {
        refreshBtn.disabled = false;
      }
    }

    refreshBtn.addEventListener("click", loadPurchaseHistory);

    const ready = loadPurchaseHistory();
    return {
      ready,
      reload: loadPurchaseHistory,
      setMode(mode) {
        if (MODES.some((m) => m.id === mode)) {
          activeMode = mode;
          syncModeTabs();
          renderActiveContent();
        }
      },
      getMode: () => activeMode,
    };
  }

  root.KitchenPastPurchasesView = {
    formatProviderLabel,
    formatDateNb,
    formatCurrencyNb,
    formatQuantityNb,
    formatFrequencyBadgeText,
    formatFrequencyDetailText,
    formatProvenanceLabel,
    filterPurchaseData,
    groupProductsByCategory,
    sortPurchaseRows,
    renderPastPurchasesView,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenPastPurchasesView;
  }
})(typeof window === "undefined" ? globalThis : window);
