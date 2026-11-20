"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const KitchenCookidooPlanSync = require("../src/home_kitchen/static/cookidoo-plan-sync.js");

const {
  buildConfirmRequest,
  buildPreviewRequest,
  formatEditSummary,
  itemContextNote,
  normalizePreview,
  selectableIds,
} = KitchenCookidooPlanSync;

const previewFixture = {
  preview_id: "11111111-1111-1111-1111-111111111111",
  direction: "cookidoo_to_kitchen",
  start: "2026-11-23",
  end: "2026-11-29",
  status: "ready",
  additions: [
    {
      item_id: "add-0",
      action: "addition",
      direction: "cookidoo_to_kitchen",
      date: "2026-11-23",
      title: "TM6 Risotto",
      servings: 4,
      cookidoo_recipe_id: "r1",
      source_url: "https://cookidoo.international/recipes/recipe/nb-NO/r1",
      will_import_recipe: true,
      same_date_existing_count: 1,
      same_date_existing_titles: ["Lunsjsalat"],
      repeated_recipe_count: 0,
      status: "pending",
    },
    {
      item_id: "add-1",
      action: "addition",
      direction: "cookidoo_to_kitchen",
      date: "2026-11-23",
      title: "TM6 Risotto",
      servings: 2,
      cookidoo_recipe_id: "r1",
      source_url: "https://cookidoo.international/recipes/recipe/nb-NO/r1",
      will_import_recipe: true,
      same_date_existing_count: 1,
      same_date_existing_titles: ["Lunsjsalat"],
      repeated_recipe_count: 1,
      status: "pending",
    },
  ],
  edits: [
    {
      item_id: "edit-2",
      action: "edit",
      direction: "cookidoo_to_kitchen",
      date: "2026-11-24",
      title: "Fiskesuppe",
      servings: 4,
      cookidoo_recipe_id: "r2",
      source_url: "https://cookidoo.international/recipes/recipe/nb-NO/r2",
      before: { title: "Gammel suppe", date: "2026-11-24" },
      after: { title: "Fiskesuppe", date: "2026-11-24" },
      status: "pending",
    },
  ],
  skipped: [],
};

test("normalizes Cookidoo plan sync preview separating additions and edits with repeated entries", () => {
  const normalized = normalizePreview(previewFixture);
  assert.equal(normalized.additions.length, 2);
  assert.equal(normalized.edits.length, 1);
  assert.deepEqual(selectableIds(normalized), ["add-0", "add-1", "edit-2"]);
  assert.match(itemContextNote(normalized.additions[0]), /Deler dato.*ingen konflikt/u);
  assert.match(itemContextNote(normalized.additions[1]), /Gjentatt oppskrift.*eget måltid/u);
  assert.equal(
    formatEditSummary(normalized.edits[0]),
    "Før: Gammel suppe (2026-11-24) → Etter: Fiskesuppe (2026-11-24)",
  );
});

test("builds validated preview and explicit reviewed confirm payloads", () => {
  assert.deepEqual(
    buildPreviewRequest({
      direction: "kitchen_to_cookidoo",
      start: "2026-11-23",
      end: "2026-11-29",
    }),
    {
      path: "/cookidoo/plan-sync/preview",
      body: {
        direction: "kitchen_to_cookidoo",
        start: "2026-11-23",
        end: "2026-11-29",
      },
    },
  );
  assert.throws(
    () => buildPreviewRequest({ direction: "cookidoo_to_kitchen", start: "2026-11-29", end: "2026-11-23" }),
    /Sluttdato/,
  );

  assert.deepEqual(buildConfirmRequest(previewFixture, ["add-0", "add-1"]), {
    path: "/cookidoo/plan-sync/11111111-1111-1111-1111-111111111111/confirm",
    body: { reviewed: true, selected_ids: ["add-0", "add-1"] },
  });
  assert.throws(() => buildConfirmRequest(previewFixture, []), /Velg minst én/);
});
