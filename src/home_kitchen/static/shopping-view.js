"use strict";
(function (root) {
  function hasSelectedProducts(basket) {
    return Boolean(
      basket &&
        Array.isArray(basket.needs) &&
        basket.needs.some((entry) =>
          Array.isArray(entry.selections) &&
          entry.selections.some((selection) => Boolean(selection?.product)),
        ),
    );
  }

  function basketPriceSummary(basket) {
    if (!hasSelectedProducts(basket)) return null;
    if (basket.total_cost == null)
      return { label: "Selected product prices are incomplete.", complete: false };
    const amount = String(basket.total_cost);
    const currency = String(basket.currency || "").trim();
    if (basket.price_complete === true)
      return {
        label: "Selected product total: " + amount + (currency ? " " + currency : ""),
        complete: true,
      };
    const knownSubtotal = Number(amount);
    if (Number.isFinite(knownSubtotal) && knownSubtotal > 0)
      return {
        label:
          "Known subtotal: " +
          amount +
          (currency ? " " + currency : "") +
          " · some prices are unknown",
        complete: false,
      };
    return { label: "Selected product prices are incomplete.", complete: false };
  }

  function pickupProgress(checked, total) {
    const count = (value) =>
      Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
    const totalCount = count(total);
    const checkedCount = Math.min(totalCount, count(checked));
    return {
      checked: checkedCount,
      total: totalCount,
      percent: totalCount ? Math.round((checkedCount / totalCount) * 100) : 0,
      label: totalCount
        ? checkedCount + " of " + totalCount + " picked up"
        : "No items to pick up",
    };
  }

  function formatQuantityForDisplay(value) {
    if (typeof value !== "string") return "";
    const match = /^(\d+)(?:\.(\d+))?$/u.exec(value.trim());
    if (!match) return value;
    const whole = BigInt(match[1]);
    const fraction = match[2] || "";
    const scale = 100n;
    let rounded = whole * scale + BigInt(fraction.slice(0, 2).padEnd(2, "0") || "0");
    if (fraction.length > 2 && fraction[2] >= "5") rounded += 1n;
    if (rounded === 0n && /[1-9]/u.test(value)) return "<0.01";
    const wholePart = rounded / scale;
    const decimalPart = String(rounded % scale).padStart(2, "0").replace(/0+$/u, "");
    return String(wholePart) + (decimalPart ? "." + decimalPart : "");
  }

  function isPositiveDecimal(value) {
    if (typeof value !== "string" && typeof value !== "number") return false;
    const text = String(value).trim();
    return /^\d+(?:\.\d+)?$/u.test(text) && /[1-9]/u.test(text.replace(".", ""));
  }

  function groupedByCategory(lines, categoryOrder = []) {
    const rank = new Map(
      (Array.isArray(categoryOrder) ? categoryOrder : []).map((category, index) => [
        String(category).trim().toLowerCase(),
        index,
      ]),
    );
    const groups = new Map();
    [...(Array.isArray(lines) ? lines : [])]
      .sort((left, right) => {
        const a = String(left.category || "Other").trim() || "Other";
        const b = String(right.category || "Other").trim() || "Other";
        const aRank = rank.get(a.toLowerCase());
        const bRank = rank.get(b.toLowerCase());
        if (aRank != null || bRank != null)
          return (aRank ?? Number.MAX_SAFE_INTEGER) - (bRank ?? Number.MAX_SAFE_INTEGER) || a.localeCompare(b);
        return a.localeCompare(b) || String(left.name).localeCompare(String(right.name));
      })
      .forEach((line) => {
        const category = String(line.category || "Other").trim() || "Other";
        if (!groups.has(category)) groups.set(category, []);
        groups.get(category).push(line);
      });
    return groups;
  }

  root.KitchenShoppingView = {
    basketPriceSummary,
    formatQuantityForDisplay,
    groupedByCategory,
    hasSelectedProducts,
    isPositiveDecimal,
    pickupProgress,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenShoppingView;
  }
})(globalThis);
