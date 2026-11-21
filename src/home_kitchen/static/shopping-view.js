"use strict";
(function (root) {
  function resolveLanguage(locale = "nb") {
    const code = String(locale ?? "").trim().toLowerCase().split(/[-_]/u)[0];
    if (code === "en" || code === "fr") return code;
    return "nb";
  }

  function resolveIntlLocale(locale = "nb") {
    const raw = String(locale ?? "").trim();
    const lower = raw.toLowerCase();
    if (!lower || lower === "nb" || lower === "nb-no" || lower === "und") return "nb-NO";
    if (lower === "en" || lower === "fr") return lower;
    return raw;
  }

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

  function basketPriceSummary(basket, locale = "nb") {
    if (!hasSelectedProducts(basket)) return null;
    if (basket.total_cost == null)
      return { label: "Prisene for de valgte varene er ufullstendige.", complete: false };
    const exactAmount = normalizeDecimalText(String(basket.total_cost)) || String(basket.total_cost);
    const amount = formatQuantityForDisplay(exactAmount, locale);
    const currency = String(basket.currency || "").trim();
    if (basket.price_complete === true)
      return {
        label: "Sum for valgte varer: " + amount + (currency ? " " + currency : ""),
        accessibleLabel:
          "Nøyaktig sum for valgte varer: " + formatQuantityForDisplay(exactAmount, locale) + (currency ? " " + currency : ""),
        complete: true,
      };
    if (isPositiveDecimal(exactAmount))
      return {
        label:
          "Kjent delsum: " +
          amount +
          (currency ? " " + currency : "") +
          " · noen priser er ukjente",
        accessibleLabel:
          "Nøyaktig kjent delsum: " +
          formatQuantityForDisplay(exactAmount, locale) +
          (currency ? " " + currency : "") +
          "; noen priser er ukjente",
        complete: false,
      };
    return { label: "Prisene for de valgte varene er ufullstendige.", complete: false };
  }

  function pickupProgress(checked, total, locale = "nb") {
    const count = (value) =>
      Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
    const totalCount = count(total);
    const checkedCount = Math.min(totalCount, count(checked));
    const intlLocale = resolveIntlLocale(locale);
    return {
      checked: checkedCount,
      total: totalCount,
      percent: totalCount ? Math.round((checkedCount / totalCount) * 100) : 0,
      label: totalCount
        ? new Intl.NumberFormat(intlLocale).format(checkedCount) + " av " + new Intl.NumberFormat(intlLocale).format(totalCount) + " varer plukket"
        : "Ingen varer å plukke",
    };
  }

  function normalizeDecimalText(value) {
    if (typeof value !== "string" && typeof value !== "number") return null;
    const text = String(value).trim();
    const match = /^(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/u.exec(text);
    if (!match) return null;
    const fraction = match[2] || "";
    if (match[3] == null) return match[1] + (fraction ? "." + fraction : "");
    let exponent;
    try {
      exponent = BigInt(match[3]);
    } catch {
      return null;
    }
    if (exponent < -1000n || exponent > 1000n) return null;
    const digits = match[1] + fraction;
    const decimalPosition = BigInt(match[1].length) + exponent;
    if (decimalPosition <= 0n)
      return "0." + "0".repeat(Number(-decimalPosition)) + digits;
    if (decimalPosition >= BigInt(digits.length))
      return digits + "0".repeat(Number(decimalPosition - BigInt(digits.length)));
    const position = Number(decimalPosition);
    return digits.slice(0, position) + "." + digits.slice(position);
  }

  function formatQuantityForDisplay(value, locale = "nb") {
    if (typeof value !== "string" && typeof value !== "number") return "";
    const exact = normalizeDecimalText(value);
    if (exact == null) return String(value);
    const decimalSeparator = resolveLanguage(locale) === "en" ? "." : ",";
    const [wholeText, fraction = ""] = exact.split(".");
    const whole = BigInt(wholeText);
    const scale = 100n;
    let rounded = whole * scale + BigInt(fraction.slice(0, 2).padEnd(2, "0") || "0");
    if (fraction.length > 2 && fraction[2] >= "5") rounded += 1n;
    if (rounded === 0n && /[1-9]/u.test(exact)) return "<0" + decimalSeparator + "01";
    const wholePart = rounded / scale;
    const decimalPart = String(rounded % scale).padStart(2, "0").replace(/0+$/u, "");
    return String(wholePart) + (decimalPart ? decimalSeparator + decimalPart : "");
  }

  function formatQuantityForAccessibility(value, locale = "nb") {
    return formatQuantityForDisplay(value, locale);
  }

  function unitIsOne(quantity) {
    return /^1(?:\.0+)?$/u.test(normalizeDecimalText(String(quantity ?? "")) || "");
  }

  function displayUnit(unit, accessible = false, quantity = "", locale = "nb") {
    const value = String(unit ?? "").trim();
    const key = value.toLowerCase();
    const lang = resolveLanguage(locale);
    const hasQuantity = String(quantity ?? "").trim() !== "";
    const singular = unitIsOne(quantity);
    const countPlural = hasQuantity ? !singular : (key === "items" || key === "pcs");
    const metricSingular = !hasQuantity || singular;
    const tables = {
      nb: {
        item: ["stk.", "stykk"],
        items: ["stk.", "stykk"],
        pc: ["stk.", "stykk"],
        pcs: ["stk.", "stykk"],
        stk: ["stk.", "stykk"],
        "stk.": ["stk.", "stykk"],
        tsp: ["ts", singular ? "teskje" : "teskjeer"],
        teaspoon: ["ts", singular ? "teskje" : "teskjeer"],
        teaspoons: ["ts", "teskjeer"],
        tbsp: ["ss", singular ? "spiseskje" : "spiseskjeer"],
        tablespoon: ["ss", singular ? "spiseskje" : "spiseskjeer"],
        tablespoons: ["ss", "spiseskjeer"],
        pack: [singular ? "pakke" : "pakker", singular ? "pakke" : "pakker"],
        packs: ["pakker", "pakker"],
        pinch: [singular ? "klype" : "klyper", singular ? "klype" : "klyper"],
        pinches: ["klyper", "klyper"],
        g: ["g", "gram"],
        kg: ["kg", "kilogram"],
        mg: ["mg", "milligram"],
        ml: ["ml", "milliliter"],
        cl: ["cl", "centiliter"],
        dl: ["dl", "desiliter"],
        l: ["l", "liter"],
      },
      en: {
        item: [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        items: [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        pc: [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        pcs: [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        stk: [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        "stk.": [countPlural ? "pcs" : "pc", countPlural ? "pieces" : "piece"],
        tsp: ["tsp", metricSingular ? "teaspoon" : "teaspoons"],
        teaspoon: ["tsp", metricSingular ? "teaspoon" : "teaspoons"],
        teaspoons: ["tsp", "teaspoons"],
        tbsp: ["tbsp", metricSingular ? "tablespoon" : "tablespoons"],
        tablespoon: ["tbsp", metricSingular ? "tablespoon" : "tablespoons"],
        tablespoons: ["tbsp", "tablespoons"],
        pack: [metricSingular ? "pack" : "packs", metricSingular ? "pack" : "packs"],
        packs: ["packs", "packs"],
        pinch: [metricSingular ? "pinch" : "pinches", metricSingular ? "pinch" : "pinches"],
        pinches: ["pinches", "pinches"],
        g: ["g", metricSingular ? "gram" : "grams"],
        kg: ["kg", metricSingular ? "kilogram" : "kilograms"],
        mg: ["mg", metricSingular ? "milligram" : "milligrams"],
        ml: ["ml", metricSingular ? "milliliter" : "milliliters"],
        cl: ["cl", metricSingular ? "centiliter" : "centiliters"],
        dl: ["dl", metricSingular ? "deciliter" : "deciliters"],
        l: ["l", metricSingular ? "liter" : "liters"],
      },
      fr: {
        item: [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        items: [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        pc: [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        pcs: [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        stk: [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        "stk.": [countPlural ? "pièces" : "pièce", countPlural ? "pièces" : "pièce"],
        tsp: ["c. à c.", metricSingular ? "cuillère à café" : "cuillères à café"],
        teaspoon: ["c. à c.", metricSingular ? "cuillère à café" : "cuillères à café"],
        teaspoons: ["c. à c.", "cuillères à café"],
        tbsp: ["c. à s.", metricSingular ? "cuillère à soupe" : "cuillères à soupe"],
        tablespoon: ["c. à s.", metricSingular ? "cuillère à soupe" : "cuillères à soupe"],
        tablespoons: ["c. à s.", "cuillères à soupe"],
        pack: [metricSingular ? "paquet" : "paquets", metricSingular ? "paquet" : "paquets"],
        packs: ["paquets", "paquets"],
        pinch: [metricSingular ? "pincée" : "pincées", metricSingular ? "pincée" : "pincées"],
        pinches: ["pincées", "pincées"],
        g: ["g", metricSingular ? "gramme" : "grammes"],
        kg: ["kg", metricSingular ? "kilogramme" : "kilogrammes"],
        mg: ["mg", metricSingular ? "milligramme" : "milligrammes"],
        ml: ["ml", metricSingular ? "millilitre" : "millilitres"],
        cl: ["cl", metricSingular ? "centilitre" : "centilitres"],
        dl: ["dl", metricSingular ? "décilitre" : "décilitres"],
        l: ["l", metricSingular ? "litre" : "litres"],
      },
    };
    const display = (tables[lang] || tables.nb)[key];
    return display ? display[accessible ? 1 : 0] : value;
  }

  function formatNeedQuantity(quantity, unit, accessible = false, approximate = false, locale = "nb") {
    const formatter = accessible ? formatQuantityForAccessibility : formatQuantityForDisplay;
    const amount = quantity == null || quantity === "" ? "" : formatter(String(quantity), locale);
    const prefix = approximate ? ({ en: "approx. ", fr: "env. " }[resolveLanguage(locale)] || "ca. ") : "";
    return [amount ? prefix + amount : "", displayUnit(unit, accessible, quantity, locale)]
      .filter(Boolean)
      .join(" ");
  }

  function categoryLabel(category) {
    const value = String(category ?? "").trim();
    return ({
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
    })[value] || value || "Annet";
  }

  function isPositiveDecimal(value) {
    const text = normalizeDecimalText(value);
    return text != null && /[1-9]/u.test(text.replace(".", ""));
  }

  function formatQuantityMentions(value, locale = "nb") {
    return String(value ?? "").replace(
      /\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/gu,
      (number) => (/[.eE]/u.test(number) ? formatQuantityForDisplay(number, locale) : number),
    );
  }

  function formatBasketIssue(issue, locale = "nb") {
    const text = typeof issue === "string" ? issue : issue?.message || String(issue ?? "");
    const trimmed = text.trim();
    if (!trimmed) return "";

    let match = /^(.+):\s*product evidence is unverified$/u.exec(trimmed);
    if (match) return `${match[1]}: produktgrunnlaget er ubekreftet`;

    match = /^(.+):\s*product information is stale or future-dated$/u.exec(trimmed);
    if (match) return `${match[1]}: produktinformasjonen er foreldet eller fremtidsdatert`;

    match = /^(.+):\s*availability is not confirmed$/u.exec(trimmed);
    if (match) return `${match[1]}: tilgjengeligheten er ikke bekreftet`;

    match = /^(.+):\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s+(\S+)\s+still needed$/u.exec(trimmed);
    if (match) {
      const name = match[1];
      const quantity = match[2];
      const unit = match[3];
      const amount = formatQuantityForDisplay(quantity, locale);
      const u = displayUnit(unit, false, quantity, locale);
      return `${name}: ${amount}${u ? " " + u : ""} gjenstår`;
    }

    match = /^(.+):\s*overbuy of\s+([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\s+(\S+)$/u.exec(trimmed);
    if (match) {
      const name = match[1];
      const quantity = match[2];
      const unit = match[3];
      const amount = formatQuantityForDisplay(quantity, locale);
      const u = displayUnit(unit, false, quantity, locale);
      return `${name}: ${amount}${u ? " " + u : ""} til overs`;
    }

    if (/^one or more selected product prices are unknown$/u.test(trimmed)) {
      return "Én eller flere valgte produktpriser er ukjente";
    }

    match = /^Quantity requires review:\s*(.+)$/u.exec(trimmed);
    if (match) return `Mengde må kontrolleres: ${match[1]}`;

    match = /^(\d+)\s+saved product selection\(s\) no longer match current shopping needs$/u.exec(trimmed);
    if (match) {
      const count = Number(match[1]);
      return count === 1
        ? "1 lagret produktvalg passer ikke lenger med nåværende handlebehov"
        : `${count} lagrede produktvalg passer ikke lenger med nåværende handlebehov`;
    }

    return formatQuantityMentions(text, locale);
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

  function mergeActionableLines(tripLines, basketNeeds) {
    const matches = new Map(
      (Array.isArray(basketNeeds) ? basketNeeds : [])
        .map((entry) => [String(entry?.need?.id ?? ""), entry])
        .filter(([id]) => id),
    );
    return (Array.isArray(tripLines) ? tripLines : []).map((line) => ({
      line,
      match: matches.get(String(line?.id ?? "")) || null,
    }));
  }

  function actionableGroups(lines, categoryOrder = [], meals = [], groupBy = "category", recipes = [], locale = "nb") {
    if (groupBy !== "meal") {
      const wrapped = (Array.isArray(lines) ? lines : []).map((entry) => ({
        ...entry,
        category: (entry?.line || entry)?.category,
        name: (entry?.line || entry)?.name,
      }));
      return groupedByCategory(wrapped, categoryOrder);
    }
    const recipeTitles = new Map((Array.isArray(recipes) ? recipes : []).map((recipe) => [String(recipe.id), recipe.title]));
    const mealTitles = new Map(
      (Array.isArray(meals) ? meals : []).map((meal) => [String(meal.id), meal.recipe?.title || meal.title || recipeTitles.get(String(meal.recipe_id)) || "Måltid"]),
    );
    const groups = new Map();
    (Array.isArray(lines) ? lines : []).forEach((entry) => {
      const line = entry?.line || entry;
      const mealIds = [...new Set((line?.origins || [])
        .filter((origin) => typeof origin === "string" && origin.startsWith("meal:"))
        .map((origin) => origin.slice(5)))];
      const title = mealIds.length > 1
        ? "Flere måltider"
        : mealIds.length === 1
          ? mealTitles.get(mealIds[0]) || "Planlagt måltid"
          : "Andre varer";
      if (!groups.has(title)) groups.set(title, []);
      groups.get(title).push(entry);
    });
    const sortLocale = resolveLanguage(locale);
    return new Map([...groups.entries()].sort(([left], [right]) => left.localeCompare(right, sortLocale)));
  }

  function mealOriginNames(origins = [], meals = [], recipes = []) {
    const recipeTitles = new Map((Array.isArray(recipes) ? recipes : []).map((recipe) => [String(recipe.id), recipe.title]));
    const titles = new Map(
      (Array.isArray(meals) ? meals : []).map((meal) => [String(meal.id), meal.recipe?.title || meal.title || recipeTitles.get(String(meal.recipe_id)) || "Planlagt måltid"]),
    );
    return [...new Set((Array.isArray(origins) ? origins : [])
      .filter((origin) => typeof origin === "string" && origin.startsWith("meal:"))
      .map((origin) => titles.get(origin.slice(5)) || "Planlagt måltid"))];
  }

  root.KitchenShoppingView = {
    basketPriceSummary,
    formatQuantityForDisplay,
    formatQuantityForAccessibility,
    displayUnit,
    formatNeedQuantity,
    categoryLabel,
    formatQuantityMentions,
    formatBasketIssue,
    groupedByCategory,
    hasSelectedProducts,
    isPositiveDecimal,
    actionableGroups,
    mealOriginNames,
    mergeActionableLines,
    normalizeDecimalText,
    pickupProgress,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenShoppingView;
  }
})(globalThis);
