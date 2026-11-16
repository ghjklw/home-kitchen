"use strict";
(function (root) {
  function cleanLine(value) {
    return String(value ?? "")
      .replace(/[\u0000-\u001f\u007f-\u009f]/gu, " ")
      .replace(/\s+/gu, " ")
      .trim();
  }

  function decimalText(value) {
    if (typeof value !== "string") return "";
    const text = root.KitchenShoppingView?.normalizeDecimalText
      ? root.KitchenShoppingView.normalizeDecimalText(value)
      : value.trim();
    if (!text || !/^[0-9]+(?:\.[0-9]+)?$/u.test(text)) return "";
    return /[1-9]/u.test(text.replace(".", "")) ? text : "";
  }

  function compareText(left, right) {
    const a = left.toLowerCase();
    const b = right.toLowerCase();
    if (a < b) return -1;
    if (a > b) return 1;
    if (left < right) return -1;
    if (left > right) return 1;
    return 0;
  }

  function isRemainingShoppingLine(line) {
    return Boolean(
      line &&
        !line.checked &&
        cleanLine(line.name) &&
        decimalText(line.to_buy_quantity),
    );
  }

  function formatRemainingShoppingText(title, lines, categoryOrder = []) {
    const eligible = [];
    (Array.isArray(lines) ? lines : []).forEach((line) => {
      if (!isRemainingShoppingLine(line)) return;
      const name = cleanLine(line.name);
      const categoryValue = cleanLine(line.category) || "Other";
      const category = categoryValue === "Other" ? "Annet" : categoryValue;
      const quantity = decimalText(line.to_buy_quantity);
      const unit = cleanLine(line.unit);
      if (!name || !quantity) return;
      eligible.push({ name, category, to_buy_quantity: quantity, unit });
    });

    const grouped = root.KitchenShoppingView?.groupedByCategory
      ? root.KitchenShoppingView.groupedByCategory(eligible, categoryOrder)
      : (() => {
          const groups = new Map();
          eligible
            .sort((a, b) => compareText(a.category, b.category))
            .forEach((item) => {
              if (!groups.has(item.category)) groups.set(item.category, []);
              groups.get(item.category).push(item);
            });
          return groups;
        })();
    const sections = [...grouped.entries()].map(([category, items]) => {
        items.sort(
          (a, b) =>
            compareText(a.name, b.name) ||
            compareText(a.unit, b.unit) ||
            compareText(a.to_buy_quantity, b.to_buy_quantity),
        );
        return `${category}:\n${items
          .map(({ name, to_buy_quantity, unit }) => {
            const quantity =
              root.KitchenShoppingView?.formatQuantityForDisplay?.(to_buy_quantity) ||
              to_buy_quantity;
            return `  - ${name} — ${quantity}${unit ? ` ${unit}` : ""}`;
          })
          .join("\n")}`;
      });

    return [cleanLine(title), ...(sections.length ? sections : ["Ingen varer gjenstår."])].join(
      "\n\n",
    );
  }

  root.KitchenShoppingShare = {
    formatRemainingShoppingText,
    isRemainingShoppingLine,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenShoppingShare;
  }
})(globalThis);
