"use strict";
(function (root) {
  const STATUSES = new Set(["previewing", "ready", "importing", "completed", "cancelled", "failed"]);
  const ENTRY_STATUSES = new Set(["pending", "importing", "imported", "already_imported", "failed", "missing_url"]);
  const LABELS = {
    previewing: "Henter oppskrifter fra Oda …",
    ready: "Klar for gjennomgang",
    importing: "Importerer valgte oppskrifter …",
    completed: "Importen er fullført",
    cancelled: "Importen er avbrutt",
    failed: "Importen kunne ikke fullføres",
    pending: "Klar for valg",
    importing_entry: "Importerer …",
    imported: "Importert",
    already_imported: "Finnes allerede i samlingen",
    failed_entry: "Kunne ikke importeres · kan prøves på nytt",
    missing_url: "Mangler offentlig oppskriftslenke",
  };

  function normalizeJob(value) {
    if (!value || typeof value !== "object" || typeof value.job_id !== "string")
      throw new Error("Oda svarte med en ugyldig importstatus.");
    if (!STATUSES.has(value.status) || !Array.isArray(value.kinds) || !Array.isArray(value.entries))
      throw new Error("Oda svarte med en ugyldig importstatus.");
    const entries = value.entries.map((entry) => {
      if (!entry || typeof entry.id !== "string" || typeof entry.title !== "string" || !ENTRY_STATUSES.has(entry.status))
        throw new Error("Oda svarte med en ugyldig oppskriftsrad.");
      return {
        ...entry,
        badges: Array.isArray(entry.badges) ? entry.badges.filter((badge) => typeof badge === "string") : [],
        metadata: entry.metadata && typeof entry.metadata === "object" && !Array.isArray(entry.metadata) ? entry.metadata : {},
      };
    });
    return {
      ...value,
      kinds: value.kinds.filter((kind) => kind === "liked" || kind === "purchased"),
      entries,
      errors: Array.isArray(value.errors) ? value.errors.filter((error) => typeof error === "string") : [],
      truncated_kinds: Array.isArray(value.truncated_kinds) ? value.truncated_kinds : [],
    };
  }

  function canSelect(entry) {
    return entry?.status === "pending" || entry?.status === "failed";
  }

  function entryStatus(status) {
    return LABELS[status === "importing" ? "importing_entry" : status] || "Status ikke tilgjengelig";
  }

  function progress(job) {
    if (job.status === "previewing")
      return job.pages_read
        ? `Hentet ${job.pages_read} Oda-sider · ${job.entries.length} oppskrifter funnet …`
        : LABELS.previewing;
    if (job.status === "importing")
      return `Importert eller behandlet ${job.processed} av ${job.total} valgte oppskrifter …`;
    return LABELS[job.status] || "Importstatus ikke tilgjengelig";
  }

  function importPayload(jobId, selectedIds) {
    const unique = [...new Set(selectedIds)];
    if (!jobId || !unique.length || unique.length !== selectedIds.length)
      throw new Error("Velg minst én oppskrift én gang før import.");
    return { path: `/recipe-collections/${encodeURIComponent(jobId)}/import`, body: { selected_ids: unique, reviewed: true } };
  }

  root.KitchenRecipeCollections = { canSelect, entryStatus, importPayload, normalizeJob, progress };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipeCollections;
})(globalThis);
