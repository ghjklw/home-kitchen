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

  function formatMappingSource(source) {
    const labels = {
      user: "Bekreftet av deg",
      retailer_identifier: "Butikkens vare-ID",
      ean: "EAN",
      description: "Foreslått fra varetekst",
      ai: "KI-forslag",
      retailer: "Oppgitt av butikk",
      description_rule: "Avledet fra varetekst",
    };
    return labels[source] || (source ? String(source).replaceAll("_", " ") : "Ukjent kilde");
  }

  function formatConfidence(confidence) {
    if (confidence == null || confidence === "") return null;
    const value = Number(confidence);
    if (!Number.isFinite(value)) return null;
    const percent = value >= 0 && value <= 1 ? value * 100 : value;
    return `${Math.round(percent)} % sikkerhet`;
  }

  function isLowConfidence(confidence) {
    if (String(confidence || "").toLowerCase() === "low") return true;
    const value = Number(confidence);
    return Number.isFinite(value) && value >= 0 && value < 0.7;
  }

  function supportsProductMapping(provider) {
    return ["oda", "coop", "trumf", "rema1000"].includes(
      String(provider || "").trim().toLowerCase(),
    );
  }

  function formatMappingMatchLabel(source, match) {
    const labels = {
      retailer_product_id: "Treff på butikkens vare-ID",
      ean: "Eksakt EAN-treff",
      exact_description: "Eksakt varetekst i samme butikk",
      opted_in_exact_description: "Identisk varetekst gjenbrukt etter bekreftelse",
    };
    if (labels[source]) return labels[source];
    if (match === "exact") return "Eksakt treff";
    if (match === "similar") return "Kobling gjenbrukt etter bekreftelse";
    return null;
  }

  function formatCategoryDisplayLabel(category) {
    const value = String(category ?? "").trim();
    if (!value) return "Annet";
    const labels = {
      Produce: "Frukt og grønt",
      Bakery: "Bakeri",
      Meat: "Kjøtt",
      Seafood: "Sjømat",
      Fish: "Fisk",
      Dairy: "Meieri",
      "Dairy & Eggs": "Meieri og egg",
      Chilled: "Kjølevarer",
      Pantry: "Tørrvarer",
      Baking: "Bakevarer",
      Spices: "Krydder",
      Drinks: "Drikke",
      Household: "Husholdning",
      Frozen: "Frysevarer",
      Other: "Annet",
      non_food: "Ikke-mat",
      unmapped: "Uavklart",
    };
    const key = Object.keys(labels).find((candidate) => candidate.toLowerCase() === value.toLowerCase());
    return labels[key] || value;
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
            makeEl("span", "purchase-line-name", line.raw_name || line.name || "Varelinje"),
          );
          const categoryIgnored = String(line.category_review_status || "").toLowerCase() === "ignored";
          const categoryLabel = !categoryIgnored && (line.suggested_category || (
            line.category_review_status === "confirmed" ? line.category_label : null
          ));
          const categoryPrefix = line.category_review_status === "confirmed" ? "Kategori" : "Kategoriforslag";
          lineLeft.append(makeEl(
            "span",
            `badge purchase-category-badge${line.category_review_status === "pending" ? " purchase-category-pending" : ""}`,
            categoryIgnored ? "Ikke kategorisert" : categoryLabel ? `${categoryPrefix}: ${formatCategoryDisplayLabel(categoryLabel)}` : "Kategori ikke bekreftet",
          ));

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
          if (line.retailer_product_id) metadata.push(["Butikkens vare-ID", String(line.retailer_product_id)]);
          if (line.category_evidence) metadata.push(["Grunnlag for kategoriforslag", String(line.category_evidence)]);
          if (line.raw_name && line.raw_name !== line.name) {
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
          li.append(renderProductMappingPanel(line, basket.provider));
          lineList.append(li);
        });
        details.append(lineList);
        card.append(details);
        section.append(card);
      });

      return section;
    }

    function renderProductMappingPanel(line, provider) {
      const panel = makeEl("section", "purchase-line-mapping");
      panel.setAttribute("aria-label", `Produktkobling for ${line.raw_name || line.name || "varelinje"}`);
      const reviewStatus = String(line.mapping_review_status || "pending").toLowerCase();
      const productId = reviewStatus === "ignored" ? null : (line.base_product_id || null);
      const productName = line.base_product_name || null;
      const mappingStatus = reviewStatus === "confirmed"
        ? "Bekreftet av deg"
        : reviewStatus === "ignored"
          ? "Ikke koblet · ignorert"
          : productId
            ? "Foreslått kobling · må kontrolleres"
            : "Ikke koblet";
      const top = makeEl("div", "purchase-line-mapping-top");
      const baseProduct = makeEl("div", "purchase-line-base-product");
      baseProduct.append(
        makeEl("span", "purchase-line-mapping-label", "Grunnprodukt"),
        makeEl("strong", productId ? "purchase-line-base-name" : "purchase-line-unmapped", productId ? (productName || "Grunnprodukt") : "Ikke koblet"),
        makeEl("span", "badge purchase-mapping-status", mappingStatus),
      );

      if (productId && line.mapping_source) {
        baseProduct.append(makeEl("span", "purchase-line-source", `Kilde: ${formatMappingSource(line.mapping_source)}`));
      }
      const suggestedProductName = reviewStatus === "ignored" ? null : (line.suggested_product_name || (
        !productId && line.mapping_source === "ai" ? line.base_product_name : null
      ));
      if (suggestedProductName) {
        baseProduct.append(
          makeEl("span", "purchase-line-suggestion-label", `Forslag: ${suggestedProductName}`),
          makeEl("span", "badge purchase-suggestion-review", "KI-forslag · må kontrolleres"),
        );
      } else if (productId && reviewStatus === "pending" && ["retailer_identifier", "ean"].includes(line.mapping_source)) {
        baseProduct.append(makeEl("span", "badge purchase-suggestion-review", "Eksakt butikkmatch · må kontrolleres"));
      }
      if (line.product_evidence) {
        baseProduct.append(makeEl("span", "purchase-line-evidence", `Grunnlag: ${line.product_evidence}`));
      }
      const mappingConfidence = formatConfidence(line.mapping_confidence);
      if (mappingConfidence) {
        const low = isLowConfidence(line.mapping_confidence);
        baseProduct.append(makeEl("span", low ? "badge warning purchase-low-confidence" : "purchase-line-confidence", `${low ? "Lav sikkerhet · " : ""}${mappingConfidence}`));
      }
      const matchLabel = formatMappingMatchLabel(line.mapping_match_source, line.mapping_match);
      if (matchLabel) baseProduct.append(makeEl("span", "purchase-line-source", matchLabel));

      const suggestedCategory = line.suggested_category || null;
      const categoryStatus = String(line.category_review_status || "").toLowerCase();
      if (suggestedCategory || line.category_source || line.category_evidence || categoryStatus === "confirmed" || categoryStatus === "ignored") {
        const categoryInfo = makeEl("div", "purchase-line-category");
        const categoryConfirmed = categoryStatus === "confirmed";
        categoryInfo.append(
          makeEl("span", "purchase-line-mapping-label", "Kategori"),
          makeEl("strong", categoryConfirmed ? "purchase-line-category-value" : "purchase-line-category-suggestion", categoryStatus === "ignored" ? "Ikke kategorisert" : formatCategoryDisplayLabel(suggestedCategory || (categoryConfirmed ? line.category_label : "Ikke bekreftet") || "Ikke bekreftet")),
        );
        if (line.category_source) {
          categoryInfo.append(makeEl("span", "purchase-line-source", `Kilde: ${formatMappingSource(line.category_source)}`));
        }
        if (line.category_evidence) {
          categoryInfo.append(makeEl("span", "purchase-line-evidence", `Grunnlag: ${line.category_evidence}`));
        }
        const categoryConfidence = formatConfidence(line.category_confidence);
        if (categoryConfidence) {
          const categoryLow = isLowConfidence(line.category_confidence);
          categoryInfo.append(makeEl("span", categoryLow ? "badge warning purchase-low-confidence" : "purchase-line-confidence", `${categoryLow ? "Lav sikkerhet · " : ""}${categoryConfidence}`));
        }
        if (!categoryConfirmed && suggestedCategory) {
          categoryInfo.append(makeEl("span", "badge purchase-suggestion-review", "Må kontrolleres"));
        }
        top.append(categoryInfo);
      }

      const packageInfo = makeEl("div", "purchase-line-package");
      packageInfo.append(makeEl("span", "purchase-line-mapping-label", "Pakningsstørrelse"));
      const hasPackageSize = line.package_size != null && String(line.package_size).trim() !== "";
      packageInfo.append(makeEl("strong", hasPackageSize ? "purchase-line-package-value" : "purchase-line-package-missing", hasPackageSize ? formatQuantityNb(line.package_size, line.package_unit) : "Ikke oppgitt"));
      if (line.package_inference_source) {
        packageInfo.append(makeEl("span", "purchase-line-source", `Kilde: ${formatMappingSource(line.package_inference_source)}`));
      }
      const packageConfidence = formatConfidence(line.package_confidence);
      if (packageConfidence) {
        const low = isLowConfidence(line.package_confidence);
        packageInfo.append(makeEl("span", low ? "badge warning purchase-low-confidence" : "purchase-line-confidence", `${low ? "Lav sikkerhet · " : ""}${packageConfidence}`));
      }
      top.append(baseProduct, packageInfo);
      panel.append(top);

      if (!supportsProductMapping(provider || line.provider)) {
        panel.append(makeEl(
          "p",
          "purchase-line-manual-note",
          "Denne handleturen er registrert manuelt. Produktkoblinger kan ikke endres her.",
        ));
        return panel;
      }

      const review = makeEl("details", "purchase-mapping-review");
      const summary = makeEl("summary", "purchase-mapping-review-summary", "Gjennomgå eller endre");
      review.append(summary);

      const editor = makeEl("div", "purchase-mapping-editor");
      const productSearchLabel = makeEl("label", null, "Søk etter grunnprodukt");
      const productSearch = makeEl("input", "purchase-mapping-product-search");
      productSearch.type = "search";
      productSearch.value = productName || suggestedProductName || line.raw_name || line.name || "";
      const initialProductSearchValue = productSearch.value.trim();
      productSearch.setAttribute("aria-label", `Søk etter grunnprodukt for ${line.raw_name || line.name || "varelinje"}`);
      productSearchLabel.append(productSearch);
      const searchButton = makeEl("button", "quiet small purchase-mapping-search", "Søk etter grunnprodukt");
      searchButton.type = "button";
      const candidates = makeEl("div", "purchase-mapping-candidates");
      candidates.setAttribute("role", "group");
      candidates.setAttribute("aria-label", "Grunnprodukter som samsvarer med søket");
      candidates.setAttribute("aria-live", "polite");
      let selectedProduct = productId ? { id: productId, canonical_name: productName || "Grunnprodukt" } : null;
      productSearch.addEventListener("input", () => {
        if (selectedProduct && productSearch.value.trim() !== selectedProduct.canonical_name) {
          selectedProduct = null;
        }
      });
      const packageFields = makeEl("div", "purchase-mapping-package-fields");
      const packageSizeLabel = makeEl("label", null, "Pakningsstørrelse");
      const packageSize = makeEl("input", "purchase-mapping-package-size");
      packageSize.type = "number";
      packageSize.min = "0";
      packageSize.step = "any";
      packageSize.value = hasPackageSize ? String(line.package_size) : "";
      const initialPackageSize = packageSize.value;
      packageSizeLabel.append(packageSize);
      const packageUnitLabel = makeEl("label", null, "Pakningsenhet");
      const packageUnit = makeEl("input", "purchase-mapping-package-unit");
      packageUnit.type = "text";
      packageUnit.value = line.package_unit || "";
      const initialPackageUnit = packageUnit.value;
      packageUnit.setAttribute("placeholder", "f.eks. g, kg, ml eller stk");
      packageUnitLabel.append(packageUnit);
      packageFields.append(packageSizeLabel, packageUnitLabel);

      const similarLabel = makeEl("label", "purchase-mapping-similar-label");
      const similarCheckbox = makeEl("input");
      similarCheckbox.type = "checkbox";
      similarCheckbox.checked = true;
      similarCheckbox.setAttribute("aria-label", "Bruk grunnproduktkoblingen på kjøp med identisk varetekst");
      similarLabel.append(similarCheckbox, makeEl("span", null, " Bruk grunnproduktkoblingen på kjøp med identisk varetekst"));

      const confirmLabel = makeEl("label", "purchase-mapping-confirm-label");
      const confirmCheckbox = makeEl("input");
      confirmCheckbox.type = "checkbox";
      confirmCheckbox.setAttribute("aria-label", "Bekreft at produktkoblingen og pakningsdataene er riktige");
      confirmLabel.append(confirmCheckbox, makeEl("span", null, " Jeg har kontrollert og bekrefter valget"));

      const acceptOptions = makeEl("div", "purchase-mapping-accept-options");
      const acceptProductCheckbox = makeEl("input");
      acceptProductCheckbox.type = "checkbox";
      acceptProductCheckbox.checked = Boolean(productId || suggestedProductName) && reviewStatus !== "confirmed";
      acceptProductCheckbox.setAttribute("aria-label", "Bekreft grunnproduktforslaget");
      const acceptProductLabel = makeEl("label", "purchase-mapping-accept-label");
      acceptProductLabel.append(acceptProductCheckbox, makeEl("span", null, " Bekreft grunnprodukt"));
      const acceptCategoryCheckbox = makeEl("input");
      acceptCategoryCheckbox.type = "checkbox";
      acceptCategoryCheckbox.checked = Boolean(suggestedCategory) && String(line.category_review_status || "").toLowerCase() !== "confirmed";
      acceptCategoryCheckbox.setAttribute("aria-label", "Bekreft kategoriforslaget");
      const acceptCategoryLabel = makeEl("label", "purchase-mapping-accept-label");
      acceptCategoryLabel.append(acceptCategoryCheckbox, makeEl("span", null, " Bekreft kategori"));
      if (productId || suggestedProductName) acceptOptions.append(acceptProductLabel);
      if (suggestedCategory) acceptOptions.append(acceptCategoryLabel);

      const status = makeEl("p", "purchase-mapping-editor-status");
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      const error = makeEl("p", "purchase-mapping-editor-error");
      error.setAttribute("role", "alert");
      error.hidden = true;
      const actions = makeEl("div", "purchase-mapping-actions");

      async function searchProducts() {
        const query = productSearch.value.trim();
        if (!query) {
          candidates.replaceChildren(makeEl("p", "muted", "Skriv inn et søk først."));
          return;
        }
        searchButton.disabled = true;
        candidates.replaceChildren(makeEl("p", "muted", `Søker etter «${query}» …`));
        try {
          const result = await api(`/catalogue/identities/products?q=${encodeURIComponent(query)}&limit=10`);
          const found = Array.isArray(result) ? result : (result?.items || result?.products || []);
          candidates.replaceChildren();
          if (!found.length) {
            candidates.append(makeEl("p", "muted", "Fant ingen grunnprodukter. Prøv et annet søk."));
            return;
          }
          found.forEach((candidate) => {
            const label = [candidate.canonical_name || candidate.name, candidate.variant].filter(Boolean).join(" · ");
            const button = makeEl("button", "quiet small purchase-mapping-candidate", label);
            button.type = "button";
            button.setAttribute("aria-pressed", selectedProduct?.id === candidate.id ? "true" : "false");
            button.addEventListener("click", () => {
              selectedProduct = { id: candidate.id, canonical_name: candidate.canonical_name || candidate.name || label };
              productSearch.value = selectedProduct.canonical_name;
              candidates.querySelectorAll("button").forEach((item) => {
                item.setAttribute("aria-pressed", item === button ? "true" : "false");
              });
            });
            candidates.append(button);
          });
        } catch (err) {
          candidates.replaceChildren(makeEl("p", "purchase-mapping-editor-error", `Kunne ikke søke: ${err?.message || "Ukjent feil"}`));
        } finally {
          searchButton.disabled = false;
        }
      }
      searchButton.addEventListener("click", searchProducts);

      async function saveMapping(action) {
        error.hidden = true;
        error.textContent = "";
        status.textContent = "";
        if (action !== "infer" && !confirmCheckbox.checked) {
          error.hidden = false;
          error.textContent = "Bekreft at du har kontrollert endringen før du lagrer.";
          confirmCheckbox.focus?.();
          return;
        }
        const typedProductName = productSearch.value.trim();
        const customProductName = typedProductName && typedProductName !== initialProductSearchValue
          ? typedProductName
          : "";
        if (action === "remap" && !selectedProduct?.id && !customProductName) {
          error.hidden = false;
          error.textContent = "Søk etter og velg et grunnprodukt før du kobler varen.";
          return;
        }
        const rowId = line.row_id || line.id;
        if (!rowId) {
          error.hidden = false;
          error.textContent = "Denne varelinjen mangler ID og kan ikke endres.";
          return;
        }
        const payload = {
          row_id: rowId,
          action,
          apply_to_similar: ["confirm", "edit", "remap"].includes(action) && Boolean(similarCheckbox.checked),
        };
        if (action === "confirm") {
          payload.confirm_product = Boolean(acceptProductCheckbox.checked);
          payload.confirm_category = Boolean(acceptCategoryCheckbox.checked);
          if (!payload.confirm_product && !payload.confirm_category) {
            error.hidden = false;
            error.textContent = "Velg om du vil bekrefte grunnproduktet, kategorien eller begge deler.";
            return;
          }
        }
        if (action === "remap" || action === "edit") {
          if (selectedProduct?.id) payload.canonical_product_id = selectedProduct.id;
          else if (customProductName) payload.canonical_product_name = customProductName;
        }
        const packageSizeValue = packageSize.value.trim();
        const packageUnitValue = packageUnit.value.trim();
        const packageChanged = packageSizeValue !== initialPackageSize || packageUnitValue !== initialPackageUnit;
        if (action === "edit" || (action === "remap" && packageChanged)) {
          if (Boolean(packageSizeValue) !== Boolean(packageUnitValue)) {
            error.hidden = false;
            error.textContent = "Oppgi både pakningsstørrelse og enhet, eller la begge feltene stå tomme.";
            return;
          }
          if (packageSizeValue && (!Number.isFinite(Number(packageSizeValue)) || Number(packageSizeValue) <= 0)) {
            error.hidden = false;
            error.textContent = "Pakningsstørrelsen må være et tall større enn null.";
            return;
          }
          payload.package_size = packageSizeValue === "" ? null : Number(packageSizeValue);
          payload.package_unit = packageUnitValue || null;
        }
        const buttons = actions.querySelectorAll("button");
        buttons.forEach((button) => { button.disabled = true; });
        status.textContent = "Lagrer endringen …";
        try {
          const result = await api("/purchase-history/product-mappings", "POST", payload);
          const affected = Number(result?.affected_rows || 0);
          const similar = Number(result?.similar_rows || 0);
          const note = similar > 0 ? ` Koblingen ble også brukt på ${similar} kjøp med identisk varetekst.` : "";
          status.textContent = `Produktkoblingen ble lagret${affected > 0 ? ` for ${affected} varelinjer` : ""}.${note}`;
          notify(status.textContent);
          review.open = false;
          await loadPurchaseHistory();
        } catch (err) {
          status.textContent = "";
          error.hidden = false;
          error.textContent = `Kunne ikke lagre produktkoblingen: ${err?.message || "Ukjent feil"}`;
        } finally {
          buttons.forEach((button) => { button.disabled = false; });
        }
      }

      const saveButton = makeEl("button", "primary small", "Lagre produkt og pakning");
      saveButton.type = "button";
      saveButton.addEventListener("click", () => {
        const typedProductName = productSearch.value.trim();
        const customProductName = typedProductName && typedProductName !== initialProductSearchValue;
        const isRemap = Boolean(selectedProduct?.id && selectedProduct.id !== productId) || Boolean(customProductName);
        saveMapping(isRemap || (!productId && selectedProduct?.id) ? "remap" : "edit");
      });
      actions.append(saveButton);
      const hasPendingProduct = Boolean(productId || suggestedProductName) && reviewStatus !== "confirmed";
      const hasPendingCategory = Boolean(suggestedCategory) && String(line.category_review_status || "").toLowerCase() !== "confirmed";
      if (hasPendingProduct || hasPendingCategory) {
        const confirmButton = makeEl("button", "quiet small", "Bekreft forslag");
        confirmButton.type = "button";
        confirmButton.addEventListener("click", () => saveMapping("confirm"));
        actions.append(confirmButton);
      }
      if (!productId) {
        const inferButton = makeEl("button", "quiet small", "Foreslå pakning med KI");
        inferButton.type = "button";
        inferButton.addEventListener("click", () => saveMapping("infer"));
        actions.append(inferButton);
      }
      if (reviewStatus !== "ignored") {
        const ignoreButton = makeEl("button", "quiet small", "Behold som ukoblet");
        ignoreButton.type = "button";
        ignoreButton.addEventListener("click", () => saveMapping("ignore"));
        actions.append(ignoreButton);
      }

      if (acceptOptions.children.length) editor.append(acceptOptions);
      editor.append(productSearchLabel, searchButton, candidates, packageFields, similarLabel, confirmLabel, actions, status, error);
      review.append(editor);
      panel.append(review);
      return panel;
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
        { key: "base_product_name", label: "Grunnprodukt" },
        { key: "package_size", label: "Pakning" },
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
          row.raw_name || row.product_name || "Varelinje",
          row.base_product_id ? (row.base_product_name || "Grunnprodukt") : "Ikke koblet",
          row.package_size != null ? formatQuantityNb(row.package_size, row.package_unit) : "Ikke oppgitt",
          row.category_review_status === "ignored"
            ? "Ikke kategorisert"
            : row.suggested_category
            ? `${row.category_review_status === "confirmed" ? "Kategori" : "Kategoriforslag"}: ${formatCategoryDisplayLabel(row.suggested_category)}`
            : row.category_review_status === "confirmed" ? `Kategori: ${formatCategoryDisplayLabel(row.category_label || "Annet")}` : "Kategori ikke bekreftet",
          row.provider_label || formatProviderLabel(row.provider),
          row.store_name || "—",
          formatQuantityNb(row.quantity, row.unit),
          row.amount != null ? formatCurrencyNb(row.amount) : "—",
        ];
        cells.forEach((val, index) => {
          const td = document.createElement("td");
          td.textContent = val;
          if (index === 1) td.append(renderProductMappingPanel(row, row.provider));
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
    formatMappingSource,
    formatConfidence,
    isLowConfidence,
    supportsProductMapping,
    formatMappingMatchLabel,
    formatCategoryDisplayLabel,
    filterPurchaseData,
    groupProductsByCategory,
    sortPurchaseRows,
    renderPastPurchasesView,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenPastPurchasesView;
  }
})(typeof window === "undefined" ? globalThis : window);
