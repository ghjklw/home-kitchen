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

  function completionCounts(job) {
    return (job?.entries || []).reduce((counts, entry) => {
      if (entry.status === "imported") counts.imported += 1;
      if (entry.status === "already_imported") counts.already += 1;
      if (entry.status === "failed") counts.failed += 1;
      return counts;
    }, { imported: 0, already: 0, failed: 0 });
  }

  function connectionGuidance(odaStatus, error, odaNeedsReconnect = () => false) {
    if (error?.status === 401 || error?.status === 403) return null;
    if (odaStatus?.configured === false) return "setup_required";
    if (odaStatus?.configured === true && odaStatus.connected === false) return "connect_required";
    if (odaNeedsReconnect(error?.detail || error?.message || "")) return "reconnect_required";
    return null;
  }

  function connectionMessage(guidance) {
    return ({
      setup_required: "Oda-tilkoblingen er ikke satt opp. Oppsettet administreres i Innstillinger.",
      connect_required: "Oda-kontoen er ikke tilkoblet. Koble til kontoen i Innstillinger, og prøv igjen.",
      reconnect_required: "Oda ber om ny godkjenning av tilkoblingen. Koble til på nytt i Innstillinger, og prøv igjen.",
    })[guidance] || "Oda-tilkoblingen kunne ikke bekreftes.";
  }

  function collectionErrorMessage(error) {
    if (!error || typeof error !== "string")
      return "Oda-innhentingen kunne ikke fullføres. Kontroller tilkoblingen og prøv på nytt.";
    const clean = error.trim();
    if (/could not finish liked on oda/i.test(clean))
      return "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.";
    if (/could not finish ingredients bought on oda/i.test(clean))
      return "Kunne ikke hente oppskrifter med bestilte ingredienser fra Oda. Prøv å hente på nytt.";
    if (/collection connection changed.*completed/i.test(clean))
      return "Oda-tilkoblingen ble endret. Allerede importerte oppskrifter er beholdt. Hent samlingene på nytt.";
    if (/collection connection changed|became unavailable/i.test(clean))
      return "Oda-tilkoblingen ble endret eller er utilgjengelig. Hent samlingene på nytt.";
    if (/connect your oda account|connect oda before/i.test(clean))
      return "Koble til Oda-kontoen din før du henter oppskriftsamlinger.";
    if (/connection expired|authorization expired/i.test(clean))
      return "Oda-tilkoblingen utløp. Koble til på nytt i Innstillinger.";
    return clean;
  }

  function errorMessages(job) {
    const raw = Array.isArray(job?.errors) ? job.errors : [];
    const translated = raw.map(collectionErrorMessage);
    return [...new Set(translated.filter(Boolean))];
  }

  function importPayload(jobId, selectedIds) {
    const unique = [...new Set(selectedIds)];
    if (!jobId || !unique.length || unique.length !== selectedIds.length)
      throw new Error("Velg minst én oppskrift én gang før import.");
    return { path: `/recipe-collections/${encodeURIComponent(jobId)}/import`, body: { selected_ids: unique, reviewed: true } };
  }

  root.KitchenRecipeCollections = { canSelect, collectionErrorMessage, completionCounts, connectionGuidance, connectionMessage, entryStatus, errorMessages, importPayload, normalizeJob, progress };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipeCollections;
})(globalThis);
