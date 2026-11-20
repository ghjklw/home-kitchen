"use strict";
(function (root) {
  const DIRECTIONS = new Set(["cookidoo_to_kitchen", "kitchen_to_cookidoo"]);
  const STATUSES = new Set(["ready", "applied", "expired", "failed"]);

  function normalizePreview(value) {
    if (!value || typeof value !== "object" || typeof value.preview_id !== "string") {
      throw new Error("Cookidoo svarte med en ugyldig synkroniseringsstatus.");
    }
    if (!DIRECTIONS.has(value.direction) || !STATUSES.has(value.status)) {
      throw new Error("Cookidoo svarte med en ugyldig synkroniseringsstatus.");
    }
    const normalizeItem = (item) => {
      if (!item || typeof item.item_id !== "string" || typeof item.title !== "string") {
        throw new Error("Ugyldig rad i synkroniseringsforhåndsvisningen.");
      }
      return {
        ...item,
        same_date_existing_count: Number(item.same_date_existing_count) || 0,
        same_date_existing_titles: Array.isArray(item.same_date_existing_titles)
          ? item.same_date_existing_titles
          : [],
        repeated_recipe_count: Number(item.repeated_recipe_count) || 0,
      };
    };
    return {
      ...value,
      additions: Array.isArray(value.additions) ? value.additions.map(normalizeItem) : [],
      edits: Array.isArray(value.edits) ? value.edits.map(normalizeItem) : [],
      skipped: Array.isArray(value.skipped) ? value.skipped : [],
      existing_kitchen_meals: Array.isArray(value.existing_kitchen_meals)
        ? value.existing_kitchen_meals
        : [],
      existing_my_week_entries: Array.isArray(value.existing_my_week_entries)
        ? value.existing_my_week_entries
        : [],
      errors: Array.isArray(value.errors) ? value.errors : [],
    };
  }

  function buildPreviewRequest({ direction, start, end, mealIds = null, edits = [] }) {
    if (!DIRECTIONS.has(direction)) {
      throw new Error("Velg retning for Cookidoo-synkronisering.");
    }
    if (!start || !end || end < start) {
      throw new Error("Sluttdato må være på eller etter startdato.");
    }
    const body = { direction, start, end };
    if (Array.isArray(mealIds) && mealIds.length) {
      body.meal_ids = mealIds;
    }
    if (Array.isArray(edits) && edits.length) {
      body.edits = edits;
    }
    return { path: "/cookidoo/plan-sync/preview", body };
  }

  function buildConfirmRequest(preview, selectedIds) {
    const unique = [...new Set(selectedIds || [])];
    if (!preview?.preview_id || !unique.length || unique.length !== (selectedIds || []).length) {
      throw new Error("Velg minst én oppføring før du bekrefter synkroniseringen.");
    }
    return {
      path: `/cookidoo/plan-sync/${encodeURIComponent(preview.preview_id)}/confirm`,
      body: { reviewed: true, selected_ids: unique },
    };
  }

  function selectableIds(preview) {
    const all = [...(preview?.additions || []), ...(preview?.edits || [])];
    return all.filter((item) => item.status === "pending").map((item) => item.item_id);
  }

  function itemContextNote(item) {
    const notes = [];
    if (item?.repeated_recipe_count > 0) {
      notes.push("Gjentatt oppskrift denne dagen · legges til som eget måltid");
    } else if (item?.same_date_existing_count > 0) {
      const titles = (item.same_date_existing_titles || []).filter(Boolean).join(", ");
      notes.push(
        titles
          ? `Deler dato med eksisterende måltid (${titles}) · ingen konflikt`
          : "Deler dato med eksisterende måltid · ingen konflikt",
      );
    }
    if (item?.will_import_recipe) {
      notes.push("Ny oppskrift fra Cookidoo · importeres med original lenke");
    }
    return notes.join(" · ");
  }

  function formatEditSummary(item) {
    if (!item || item.action !== "edit") return "";
    const beforeTitle = item.before?.title || "Ukjent";
    const beforeDate = item.before?.date || "";
    const afterTitle = item.after?.title || item.title || "Ukjent";
    const afterDate = item.after?.date || item.date || "";
    return `Før: ${beforeTitle} (${beforeDate}) → Etter: ${afterTitle} (${afterDate})`;
  }

  root.KitchenCookidooPlanSync = {
    buildConfirmRequest,
    buildPreviewRequest,
    formatEditSummary,
    itemContextNote,
    normalizePreview,
    selectableIds,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenCookidooPlanSync;
  }
})(globalThis);
