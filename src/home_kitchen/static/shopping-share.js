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
    const text = value.trim();
    if (!/^[0-9]+(?:\.[0-9]+)?$/u.test(text)) return "";
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

  function formatRemainingShoppingText(title, lines) {
    const groups = new Map();
    (Array.isArray(lines) ? lines : []).forEach((line) => {
      if (!isRemainingShoppingLine(line)) return;
      const name = cleanLine(line.name);
      const category = cleanLine(line.category) || "Other";
      const quantity = decimalText(line.to_buy_quantity);
      const unit = cleanLine(line.unit);
      if (!name || !quantity) return;
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push({ name, quantity, unit });
    });

    const sections = [...groups.entries()]
      .sort(([a], [b]) => compareText(a, b))
      .map(([category, items]) => {
        items.sort(
          (a, b) =>
            compareText(a.name, b.name) ||
            compareText(a.unit, b.unit) ||
            compareText(a.quantity, b.quantity),
        );
        return `${category}:\n${items
          .map(({ name, quantity, unit }) => `  - ${name} — ${quantity}${unit ? ` ${unit}` : ""}`)
          .join("\n")}`;
      });

    return [cleanLine(title), ...(sections.length ? sections : ["No items remaining."])].join(
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
