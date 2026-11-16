"use strict";
(function (root) {
  const SEARCH_STATUSES = new Set(["ok", "no_usable_results", "rate_limited", "unavailable", "invalid_continuation"]);

  function safeUrl(value, hosts) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && hosts.has(url.hostname) && !url.username && !url.password && !url.port
        ? url.href
        : null;
    } catch {
      return null;
    }
  }

  function normalizeSession(value) {
    const search = value?.search;
    if (!value || typeof value.search_id !== "string" || !search || !SEARCH_STATUSES.has(search.status) || !Array.isArray(search.results))
      throw new Error("Bildetjenesten svarte med ugyldig søkestatus.");
    const results = search.results.filter((candidate) =>
      candidate &&
      candidate.provider === "wikimedia_commons" &&
      typeof candidate.provider_id === "string" &&
      typeof candidate.title === "string" &&
      typeof candidate.license_name === "string" &&
      typeof candidate.license_url === "string" &&
      safeUrl(candidate.image_url, new Set(["upload.wikimedia.org"])) &&
      safeUrl(candidate.thumbnail_url, new Set(["upload.wikimedia.org"])) &&
      safeUrl(candidate.source_page_url, new Set(["commons.wikimedia.org"])) &&
      safeUrl(candidate.license_url, new Set(["creativecommons.org", "www.creativecommons.org", "commons.wikimedia.org"])),
    );
    return {
      search_id: value.search_id,
      status: search.status,
      detail: typeof search.detail === "string" ? search.detail : null,
      results,
      continuation: typeof search.continuation === "string" ? search.continuation : null,
    };
  }

  function statusLabel(status) {
    return ({
      no_usable_results: "Fant ingen bilder med en lisens som kan vises her.",
      rate_limited: "Bildesøket er midlertidig begrenset. Prøv igjen om litt.",
      unavailable: "Bildesøket er midlertidig utilgjengelig.",
      invalid_continuation: "Søket utløp. Start et nytt søk.",
      ok: "Velg et bilde for å se lisens og kreditering før du bruker det.",
    })[status] || "Søkestatus er ikke tilgjengelig.";
  }

  root.KitchenRecipeImages = { normalizeSession, safeUrl, statusLabel };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipeImages;
})(globalThis);
