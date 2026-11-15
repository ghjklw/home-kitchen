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
    if (Number(hours)) parts.push(hours + " hr");
    if (Number(minutes)) parts.push(minutes + " min");
    if (Number(seconds) && !Number(days) && !Number(hours) && !Number(minutes))
      parts.push(seconds + " sec");
    return parts.join(" ") || "0 min";
  }

  function statusLabel(status) {
    return ({
      pending: "Waiting to search",
      searching: "Searching Oda",
      matched: "Suggestions ready",
      no_results: "No matches found",
      search_failed: "Search failed",
      search_unavailable: "Search unavailable",
    })[status] || "Search status unavailable";
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

  root.KitchenBasketMatchView = {
    formatDuration,
    mergeNeedPages,
    planKey,
    selectionRecommendation,
    statusLabel,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenBasketMatchView;
})(globalThis);
