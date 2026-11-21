const assert = require("node:assert/strict");
const test = require("node:test");

const {
  basketPriceSummary,
  actionableGroups,
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
  mealOriginNames,
  mergeActionableLines,
  normalizeDecimalText,
  pickupProgress,
} = require("../src/home_kitchen/static/shopping-view.js");

test("does not show a basket total when nothing has been selected", () => {
  assert.equal(hasSelectedProducts({ needs: [{ selections: [] }] }), false);
  assert.equal(basketPriceSummary({ total_cost: "0", currency: "NOK", price_complete: false, needs: [] }), null);
});

test("joins the checked store-trip line to its matching product review by stable ID", () => {
  const line = { id: "need-1", name: "Milk", checked: true, to_buy_quantity: "0.5" };
  const match = { need: { id: "need-1" }, selections: [] };
  assert.deepEqual(mergeActionableLines([line], [match]), [{ line, match }]);
  assert.equal(mergeActionableLines([line], [{ need: { id: "other" } }])[0].match, null);
});

test("recipe grouping keeps shared needs to one actionable row and names every origin", () => {
  const line = { id: "milk", origins: ["meal:1", "meal:2"] };
  const entries = mergeActionableLines([line], []);
  const grouped = actionableGroups(entries, [], [
    { id: "1", recipe_id: "r1" },
    { id: "2", recipe_id: "r2" },
  ], "meal", [
    { id: "r1", title: "Grøt" },
    { id: "r2", title: "Pannekaker" },
  ]);
  assert.equal([...grouped.values()].flat().length, 1);
  assert.equal([...grouped.keys()][0], "Flere måltider");
  assert.deepEqual(mealOriginNames(line.origins, [
    { id: "1", recipe_id: "r1" },
    { id: "2", recipe_id: "r2" },
  ], [
    { id: "r1", title: "Grøt" },
    { id: "r2", title: "Pannekaker" },
  ]), ["Grøt", "Pannekaker"]);
});

test("category grouping sorts and retains actionable row payloads", () => {
  const entries = mergeActionableLines([
    { id: "2", name: "Milk", category: "Dairy" },
    { id: "1", name: "Apples", category: "Fruit" },
  ], []);
  const grouped = actionableGroups(entries, ["Fruit", "Dairy"]);
  assert.deepEqual([...grouped.keys()], ["Fruit", "Dairy"]);
  assert.equal([...grouped.values()].flat()[0].line.name, "Apples");
});

test("shows a complete total only for selected products with complete prices", () => {
  const basket = {
    total_cost: "92.50",
    currency: "NOK",
    price_complete: true,
    needs: [{ selections: [{ product: { product_id: "42" } }] }],
  };
  assert.equal(hasSelectedProducts(basket), true);
  assert.deepEqual(basketPriceSummary(basket), {
    label: "Sum for valgte varer: 92,5 NOK",
    accessibleLabel: "Nøyaktig sum for valgte varer: 92,5 NOK",
    complete: true,
  });
});

test("labels partial price data as a subtotal and suppresses a misleading zero total", () => {
  const selected = { product: { product_id: "42" } };
  assert.equal(
    basketPriceSummary({
      total_cost: "25.00",
      currency: "NOK",
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Kjent delsum: 25 NOK · noen priser er ukjente",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: "0",
      currency: "NOK",
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Prisene for de valgte varene er ufullstendige.",
  );
  assert.equal(
    basketPriceSummary({
      total_cost: null,
      price_complete: false,
      needs: [{ selections: [selected] }],
    }).label,
    "Prisene for de valgte varene er ufullstendige.",
  );
});

test("formats pickup progress and handles empty or inconsistent counts", () => {
  assert.deepEqual(pickupProgress(2, 5), {
    checked: 2,
    total: 5,
    percent: 40,
    label: "2 av 5 varer plukket",
  });
  assert.deepEqual(pickupProgress(9, 3), {
    checked: 3,
    total: 3,
    percent: 100,
    label: "3 av 3 varer plukket",
  });
  assert.deepEqual(pickupProgress(0, 0), {
    checked: 0,
    total: 0,
    percent: 0,
    label: "Ingen varer å plukke",
  });
});

test("rounds display quantities with decimal-string arithmetic only", () => {
  assert.equal(formatQuantityForDisplay("2.345"), "2,35");
  assert.equal(formatQuantityForDisplay("1.2300"), "1,23");
  assert.equal(formatQuantityForDisplay("0.004"), "<0,01");
  assert.equal(formatQuantityForDisplay("12345678901234567890.005"), "12345678901234567890,01");
  assert.equal(formatQuantityForDisplay("unknown"), "unknown");
  assert.equal(formatQuantityForDisplay("1E+3"), "1000");
  assert.equal(formatQuantityForDisplay("1.25e-3"), "<0,01");
  assert.equal(normalizeDecimalText("1.25e-3"), "0.00125");
  assert.equal(normalizeDecimalText("1,000.5"), null);
});

test("formats accessible quantities compactly without hiding small positive amounts", () => {
  assert.equal(formatQuantityForAccessibility("233.3333333333333333333333334"), "233,33");
  assert.equal(formatQuantityForAccessibility("1.2300"), "1,23");
  assert.equal(formatQuantityForAccessibility("1E+3"), "1000");
  assert.equal(formatQuantityForAccessibility("0.0001"), "<0,01");
  assert.equal(formatQuantityForAccessibility("not-a-number"), "not-a-number");
});

test("formats shopping amounts and units for Bokmål without changing source values", () => {
  assert.equal(formatNeedQuantity("249.999999999999999999", "ml"), "250 ml");
  assert.equal(formatNeedQuantity("2", "tsp"), "2 ts");
  assert.equal(formatNeedQuantity("1", "tbsp", true), "1 spiseskje");
  assert.equal(formatNeedQuantity("2", "item", true), "2 stykk");
  assert.equal(formatNeedQuantity("3", "stk"), "3 stk.");
  assert.equal(formatNeedQuantity(null, "g"), "g");
  assert.equal(displayUnit("unmapped-source-unit"), "unmapped-source-unit");
});

test("localizes canonical category headings while retaining custom labels", () => {
  assert.equal(categoryLabel("Produce"), "Frukt og grønt");
  assert.equal(categoryLabel("Dairy"), "Meieri");
  assert.equal(categoryLabel("Dairy & Eggs"), "Meieri og egg");
  assert.equal(categoryLabel("Baking"), "Bakevarer");
  assert.equal(categoryLabel("Kjøkkenfavoritter"), "Kjøkkenfavoritter");
});

test("detects positive decimal text without floating-point coercion", () => {
  assert.equal(isPositiveDecimal("0.0001"), true);
  assert.equal(isPositiveDecimal("0.000"), false);
  assert.equal(isPositiveDecimal("1e2"), true);
  assert.equal(isPositiveDecimal("1,000.5"), false);
});

test("formats decimal quantities inside basket issue text", () => {
  assert.equal(
    formatQuantityMentions("Milk: 233.3333333333333333333333334 g still needed; 1E+3 ml overbuy"),
    "Milk: 233,33 g still needed; 1000 ml overbuy",
  );
});

test("localizes known backend basket evaluation issues to Bokmål with formatted quantities and units", () => {
  assert.equal(
    formatBasketIssue("Tine yoghurt: product evidence is unverified"),
    "Tine yoghurt: produktgrunnlaget er ubekreftet",
  );
  assert.equal(
    formatBasketIssue("Tine yoghurt: product information is stale or future-dated"),
    "Tine yoghurt: produktinformasjonen er foreldet eller fremtidsdatert",
  );
  assert.equal(
    formatBasketIssue("Tine yoghurt: availability is not confirmed"),
    "Tine yoghurt: tilgjengeligheten er ikke bekreftet",
  );
  assert.equal(
    formatBasketIssue("Milk: 233.3333333333333333333333334 g still needed"),
    "Milk: 233,33 g gjenstår",
  );
  assert.equal(
    formatBasketIssue("Eggs: 1 item still needed"),
    "Eggs: 1 stk. gjenstår",
  );
  assert.equal(
    formatBasketIssue("Eggs: overbuy of 2 item"),
    "Eggs: 2 stk. til overs",
  );
  assert.equal(
    formatBasketIssue("Flour: overbuy of 0.5 kg"),
    "Flour: 0,5 kg til overs",
  );
  assert.equal(
    formatBasketIssue("one or more selected product prices are unknown"),
    "Én eller flere valgte produktpriser er ukjente",
  );
  assert.equal(
    formatBasketIssue("Quantity requires review: Salt"),
    "Mengde må kontrolleres: Salt",
  );
  assert.equal(
    formatBasketIssue("1 saved product selection(s) no longer match current shopping needs"),
    "1 lagret produktvalg passer ikke lenger med nåværende handlebehov",
  );
  assert.equal(
    formatBasketIssue("3 saved product selection(s) no longer match current shopping needs"),
    "3 lagrede produktvalg passer ikke lenger med nåværende handlebehov",
  );
  // Fallback preserves unknown issues while formatting decimal numbers
  assert.equal(
    formatBasketIssue("Custom warning with 12.500 kg amount"),
    "Custom warning with 12,5 kg amount",
  );
});

test("supports a configured category walking order with a stable alphabetical fallback", () => {
  const lines = [
    { name: "Milk", category: "Dairy" },
    { name: "Bread", category: "Bakery" },
    { name: "Apples", category: "Produce" },
    { name: "Oats", category: "Pantry" },
  ];
  assert.deepEqual(
    [...groupedByCategory(lines, ["Produce", "Bakery"]).keys()],
    ["Produce", "Bakery", "Dairy", "Pantry"],
  );
  assert.deepEqual([...groupedByCategory(lines).keys()], ["Bakery", "Dairy", "Pantry", "Produce"]);
});

test("formats units, decimal separators, and counts for en, fr, and nb household locales while keeping unmapped units verbatim", () => {
  // English locale
  assert.equal(formatNeedQuantity("2.345", "dl", false, false, "en"), "2.35 dl");
  assert.equal(formatNeedQuantity("0.004", "g", false, false, "en"), "<0.01 g");
  assert.equal(formatNeedQuantity("1", "item", false, false, "en"), "1 pc");
  assert.equal(formatNeedQuantity("3", "item", false, false, "en"), "3 pcs");
  assert.equal(formatNeedQuantity("2", "item", true, false, "en"), "2 pieces");
  assert.equal(formatNeedQuantity("1", "tsp", true, false, "en"), "1 teaspoon");
  assert.equal(formatNeedQuantity("2", "tbsp", false, false, "en"), "2 tbsp");
  assert.equal(displayUnit("unmapped-source-unit", false, "2", "en"), "unmapped-source-unit");
  assert.equal(
    pickupProgress(1200, 2500, "en").label,
    `${new Intl.NumberFormat("en").format(1200)} av ${new Intl.NumberFormat("en").format(2500)} varer plukket`,
  );

  // French locale
  assert.equal(formatNeedQuantity("2.345", "dl", false, false, "fr"), "2,35 dl");
  assert.equal(formatNeedQuantity("1", "item", false, false, "fr"), "1 pièce");
  assert.equal(formatNeedQuantity("3", "item", false, false, "fr"), "3 pièces");
  assert.equal(formatNeedQuantity("2", "tsp", false, false, "fr"), "2 c. à c.");
  assert.equal(formatNeedQuantity("1", "tbsp", true, false, "fr"), "1 cuillère à soupe");
  assert.equal(formatNeedQuantity("2", "dl", true, false, "fr"), "2 décilitres");
  assert.equal(displayUnit("pincée", false, "2", "fr"), "pincée");
  assert.equal(
    pickupProgress(1200, 2500, "fr").label,
    `${new Intl.NumberFormat("fr").format(1200)} av ${new Intl.NumberFormat("fr").format(2500)} varer plukket`,
  );

  // Bokmål default when unset or 'nb'
  assert.equal(formatNeedQuantity("2", "item", false, false, "nb"), "2 stk.");
  assert.equal(formatNeedQuantity("2", "item", false, false, undefined), "2 stk.");
  assert.equal(formatNeedQuantity("2", "tsp", false, false, null), "2 ts");
});
