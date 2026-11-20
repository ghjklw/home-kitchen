(function (root) {
  "use strict";

  function planKey(start, end, needs) {
    const fingerprint = (Array.isArray(needs) ? needs : [])
      .map((need) => [
        String(need.id ?? need.need_id ?? ""),
        String(need.name ?? ""),
        String(need.quantity ?? ""),
        String(need.unit ?? ""),
      ])
      .sort((left, right) => left[0].localeCompare(right[0]));
    return JSON.stringify([start, end, fingerprint]);
  }

  function mergeNeedPages(existing, updates, activeVersions, jobVersions) {
    const merged = { ...(existing || {}) };
    (Array.isArray(updates) ? updates : []).forEach((need) => {
      const id = String(need.need_id ?? "");
      if (!id || activeVersions?.[id] !== jobVersions?.[id]) return;
      const page = Number.isInteger(need.page) && need.page > 0 ? need.page : 1;
      const pages = { ...(merged[id] || {}) };
      const previous = pages[page];
      if (
        previous &&
        previous.match_job_id &&
        need.match_job_id &&
        previous.match_job_id === need.match_job_id &&
        Number.isInteger(previous.revision) &&
        Number.isInteger(need.revision) &&
        need.revision < previous.revision
      ) {
        return;
      }
      pages[page] = {
        ...need,
        candidates:
          ["pending", "searching"].includes(need.status) && previous
            ? previous.candidates
            : need.candidates || [],
      };
      merged[id] = pages;
    });
    return merged;
  }

  function formatDuration(value) {
    if (typeof value !== "string") return "";
    const match = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$/u.exec(value);
    if (!match) return value;
    const [, days = "0", hours = "0", minutes = "0", seconds = "0"] = match;
    const parts = [];
    if (Number(days)) parts.push(days + " d");
    if (Number(hours)) parts.push(hours + " t");
    if (Number(minutes)) parts.push(minutes + " min");
      if (Number(seconds) && !Number(days) && !Number(hours) && !Number(minutes))
        parts.push(seconds + " sek");
    return parts.join(" ") || "0 min";
  }

  function statusLabel(status) {
    return ({
      pending: "Venter på søk",
      searching: "Søker hos Oda",
      matched: "Forslagene er klare",
      no_results: "Ingen treff funnet",
      search_failed: "Søket mislyktes",
      search_unavailable: "Søket er ikke tilgjengelig",
    })[status] || "Søkestatus er ikke tilgjengelig";
  }

  function selectionRecommendation(candidate, needUnit) {
    if (
      !Number.isSafeInteger(candidate?.recommended_packages) ||
      candidate.recommended_packages < 1 ||
      candidate.recommended_fulfillment_quantity == null ||
      candidate.pack_quantity == null ||
      candidate.pack_unit !== needUnit
    )
      return null;
    return {
      packages: candidate.recommended_packages,
      fulfillment: String(candidate.recommended_fulfillment_quantity),
      packQuantity: String(candidate.pack_quantity),
      packUnit: candidate.pack_unit,
    };
  }

  function identityStatus(candidate) {
    return candidate?.identity_status === "compatible" ? "compatible" : "unverified";
  }

  function formatNeedQuantity(
    need,
    formatQuantity = (value) => String(value ?? ""),
    formatUnit = (value) => String(value ?? "").trim(),
  ) {
    const quantity = formatQuantity(need?.quantity);
    const unit = formatUnit(need?.unit, need?.quantity);
    return [need?.approximate === true ? "ca. " + quantity : quantity, unit]
      .filter(Boolean)
      .join(" ");
  }

  function sourceUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const parsed = new URL(value);
      if (
        parsed.protocol !== "https:" ||
        parsed.username ||
        parsed.password
      )
        return null;
      return parsed.href;
    } catch {
      return null;
    }
  }

  function availabilityStatusLabel(status) {
    return ({
      available: "Tilgjengelig hos Oda",
      unavailable: "Ikke tilgjengelig hos Oda",
      unknown: "Tilgjengeligheten kunne ikke bekreftes",
    })[status] || "Tilgjengeligheten er ukjent";
  }

  function buildAvailableCartProposalRequest(basket, availabilityReport) {
    const reportItems = Array.isArray(availabilityReport?.items)
      ? availabilityReport.items
      : [];
    const reportByPair = new Map();
    const unavailablePairs = new Set();
    reportItems.forEach((item) => {
      const key = String(item.need_id) + ":" + String(item.product_id);
      reportByPair.set(key, item);
      if (item.availability_status !== "available") {
        unavailablePairs.add(key);
      }
    });

    const byProduct = new Map();
    const queries = new Set();
    let skippedCount = 0;
    let availableCount = 0;

    (basket?.needs || []).forEach((entry) => {
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
          return;
        }
        const pairKey = String(need.id) + ":" + String(product.product_id);
        if (unavailablePairs.has(pairKey)) {
          skippedCount += 1;
          return;
        }
        availableCount += 1;
        const key = String(productId);
        const current = byProduct.get(key) || {
          product_id: productId,
          quantity: 0,
        };
        current.quantity += quantity;
        byProduct.set(key, current);
        const reported = reportByPair.get(pairKey);
        const query = reported?.search_query || product.name || need.name;
        if (query) queries.add(query);
      });
    });

    const additions = [...byProduct.values()];
    return {
      queries: [...queries],
      additions,
      skippedCount,
      availableCount,
      issue:
        additions.length > 10 || queries.size > 10
          ? "Se over høyst 10 ulike produkttyper om gangen."
          : !additions.length
            ? "Ingen tilgjengelige Oda-produkter gjenstår å sende til gjennomgang."
            : "",
    };
  }

  root.KitchenBasketMatchView = {
    availabilityStatusLabel,
    buildAvailableCartProposalRequest,
    formatBasketIssue: (issue) => root.KitchenShoppingView?.formatBasketIssue?.(issue) ?? issue,
    formatDuration,
    formatNeedQuantity,
    identityStatus,
    mergeNeedPages,
    planKey,
    selectionRecommendation,
    sourceUrl,
    statusLabel,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenBasketMatchView;
})(globalThis);
