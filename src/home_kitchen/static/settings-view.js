(function (root) {
  "use strict";

  const DEFAULT_SHOPPING_CATEGORY_ORDER = [
    "Produce",
    "Bakery",
    "Meat",
    "Seafood",
    "Fish",
    "Dairy",
    "Chilled",
    "Pantry",
    "Spices",
    "Drinks",
    "Household",
    "Frozen",
    "Other",
  ];

  function moveShoppingCategory(categories, from, to) {
    if (
      !Array.isArray(categories) ||
      from < 0 ||
      from >= categories.length ||
      to < 0 ||
      to >= categories.length ||
      from === to
    )
      return [...(categories || [])];
    const moved = [...categories];
    moved.splice(to, 0, moved.splice(from, 1)[0]);
    return moved;
  }

  function normalizeShoppingCategoryOrder(categories) {
    if (!Array.isArray(categories)) return { value: null, error: "invalid" };
    const normalized = categories.map((category) => String(category).trim());
    if (normalized.length > 64) return { value: null, error: "too_many" };
    if (normalized.some((category) => !category || category.length > 100))
      return { value: null, error: "invalid_name" };
    if (new Set(normalized.map((category) => category.toLowerCase())).size !== normalized.length)
      return { value: null, error: "duplicate" };
    return { value: normalized, error: null };
  }

  function createShoppingCategoryRow(value, displayLabel) {
    return { value, originalValue: value, displayValue: displayLabel(value) };
  }

  function editShoppingCategoryRow(row, displayValue, displayLabel) {
    return {
      ...row,
      displayValue,
      value: displayValue === displayLabel(row.originalValue) ? row.originalValue : displayValue,
    };
  }

  function integrationStatusLabel(status) {
    return (
      {
        connected: "Tilkoblet",
        configured: "Satt opp",
        not_configured: "Oppsett kreves",
        unavailable: "Ikke tilgjengelig",
        expired: "Sesjon utløpt",
      }[status] || "Ukjent status"
    );
  }

  function integrationAuthLabel(mode, configured = false) {
    return (
      {
        delegated_user_pkce: "Oda-kontoen din (OAuth)",
        federation: "Føderert administrert identitet",
        api_key: configured ? "API-nøkkel er satt opp (hemmeligheten er skjult)" : "API-nøkkel",
        session_token: configured ? "Sesjonstoken er lagret (hemmeligheten er skjult)" : "Sesjonstoken",
        session_cookie: configured ? "Sesjonsinformasjonskapsel er lagret (hemmeligheten er skjult)" : "Sesjonsinformasjonskapsel",
        credentials: configured ? "Brukernavn og passord er lagret (hemmeligheten er skjult)" : "Brukernavn og passord",
        manual_session_token: configured ? "Sesjonstoken er lagret (hemmeligheten er skjult)" : "Sesjonstoken",
        manual_session_cookie: configured ? "Sesjonsinformasjonskapsel er lagret (hemmeligheten er skjult)" : "Sesjonsinformasjonskapsel",
      }[mode] || "Ikke oppgitt"
    );
  }

  function integrationExpiryNotice(integration, nowMs = Date.now()) {
    if (!integration) return null;
    if (integration.status === "expired") {
      return (
        integration.expiry_prompt ||
        "Sesjonen har utløpt. Koble til på nytt for å fortsette synkronisering."
      );
    }
    if (integration.expires_at) {
      const expiry = new Date(integration.expires_at).getTime();
      if (!isNaN(expiry)) {
        if (expiry <= nowMs) {
          return (
            integration.expiry_prompt ||
            "Sesjonen har utløpt. Koble til på nytt for å fortsette synkronisering."
          );
        }
        const diffDays = Math.ceil((expiry - nowMs) / (1000 * 60 * 60 * 24));
        if (diffDays <= 7) {
          return `Sesjonen utløper om ${diffDays} ${diffDays === 1 ? "dag" : "dager"}. Vurder å fornye tilkoblingen snart.`;
        }
      }
    }
    return null;
  }

  function normalizeIntegrationResponse(payload) {
    if (Array.isArray(payload)) return payload;
    return null;
  }

  function connectionStatusSnapshot(integrationsResult, odaResult) {
    const integrationResponse = integrationsResult?.status === "fulfilled"
      ? normalizeIntegrationResponse(integrationsResult.value)
      : null;
    const odaValue = odaResult?.value;
    const odaOk =
      odaResult?.status === "fulfilled" &&
      odaValue !== null &&
      typeof odaValue === "object" &&
      !Array.isArray(odaValue) &&
      typeof odaValue.configured === "boolean" &&
      typeof odaValue.connected === "boolean";
    return {
      integrations: integrationResponse || [],
      oda: odaOk ? odaValue : null,
      integrationsError: integrationResponse
        ? null
        : "Tilkoblingsstatus er ikke tilgjengelig. Du kan prøve igjen uten å forlate siden.",
      odaStatusError: odaOk
        ? null
        : "Oda-status er ikke tilgjengelig. De andre tilkoblingsdetaljene vises fortsatt.",
    };
  }

  function odaNeedsReconnect(message) {
    return /connect (?:your )?oda|oda (?:connection|authorization).*(?:expired|invalid|cannot be read)|authorization.*connect|connection requires review/iu.test(
      message || "",
    );
  }

  root.KitchenSettingsView = {
    DEFAULT_SHOPPING_CATEGORY_ORDER,
    moveShoppingCategory,
    normalizeShoppingCategoryOrder,
    createShoppingCategoryRow,
    editShoppingCategoryRow,
    integrationStatusLabel,
    integrationAuthLabel,
    normalizeIntegrationResponse,
    connectionStatusSnapshot,
    odaNeedsReconnect,
    integrationExpiryNotice,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenSettingsView;
})(typeof window === "undefined" ? globalThis : window);
