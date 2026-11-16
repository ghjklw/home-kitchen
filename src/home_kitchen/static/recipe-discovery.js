(function (root) {
  "use strict";

  const SOURCES = [
    { id: "oda", label: "Oda" },
    { id: "marmiton", label: "Marmiton" },
    { id: "matprat", label: "Matprat" },
  ];
  const STATUSES = {
    ok: "Søket er fullført",
    connection_required: "Koble til kilden for å søke",
    unsupported: "Søk støttes ikke for denne kilden",
    unavailable: "Kilden er midlertidig utilgjengelig",
    timed_out: "Søket etter kilder tok for lang tid",
  };

  function safeHttpsUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password) return null;
      return value;
    } catch {
      return null;
    }
  }

  function buildRequest(queryValue, selectedSources = SOURCES.map((source) => source.id)) {
    const query = String(queryValue ?? "").trim();
    if (!query) throw new Error("Skriv inn ingredienser eller en rett for å finne oppskrifter.");
    if (query.length > 200) throw new Error("Søket kan ikke være lengre enn 200 tegn.");
    const sources = [...new Set(Array.isArray(selectedSources) ? selectedSources : [])].filter((source) =>
      SOURCES.some((item) => item.id === source),
    );
    if (!sources.length) throw new Error("Velg minst én oppskriftskilde.");
    return { query, sources, limit: 10 };
  }

  function normalizeResponse(response) {
    return {
      query: String(response?.query ?? ""),
      sources: (Array.isArray(response?.sources) ? response.sources : []).map((item) => {
        const source = SOURCES.find((entry) => entry.id === item.source);
        return {
          source: source?.id || "unknown",
          label: source?.label || "Oppskriftskilde",
          status: STATUSES[item.status] || "Kildestatus er ikke tilgjengelig",
          detail: typeof item.detail === "string" ? item.detail : "",
          resultCount: Number.isInteger(item.result_count) ? item.result_count : 0,
          indexUrl: safeHttpsUrl(item.index_url),
        };
      }),
      results: (Array.isArray(response?.results) ? response.results : []).flatMap((item) => {
        const label = SOURCES.find((source) => source.id === item.source)?.label;
        const url = safeHttpsUrl(item.url);
        if (!label || typeof item.title !== "string" || !item.title.trim()) return [];
        return [{
          source: item.source,
          sourceLabel: label,
          title: item.title,
          url,
          imageUrl: safeHttpsUrl(item.image_url),
          sourceAttribution: typeof item.source_attribution === "string" ? item.source_attribution : "",
          duration: typeof item.duration === "string" ? item.duration : "",
          difficulty: typeof item.difficulty === "string" ? item.difficulty : "",
          importable: Boolean(item.importable && url),
          detail: typeof item.detail === "string" ? item.detail : "",
        }];
      }),
    };
  }

  root.KitchenRecipeDiscovery = { SOURCES, buildRequest, normalizeResponse, safeHttpsUrl };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipeDiscovery;
})(globalThis);
