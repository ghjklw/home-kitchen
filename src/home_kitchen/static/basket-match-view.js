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
      search_failed: "Produktsøket kunne ikke fullføres. Prøv igjen.",
      search_unavailable: "Produktsøk er midlertidig utilgjengelig.",
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

  function formatNeedQuantity(need, formatQuantity = (value) => String(value ?? ""), formatUnit = (value) => String(value ?? "").trim()) {
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

  root.KitchenBasketMatchView = {
    formatDuration,
    formatNeedQuantity,
    mergeNeedPages,
    planKey,
    selectionRecommendation,
    sourceUrl,
    statusLabel,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenBasketMatchView;
})(globalThis);
