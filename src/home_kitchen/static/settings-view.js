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

  function integrationStatusLabel(status) {
    return (
      {
        connected: "Connected",
        configured: "Configured",
        not_configured: "Setup required",
        unavailable: "Unavailable",
      }[status] || "Status unknown"
    );
  }

  function integrationAuthLabel(mode, configured = false) {
    return (
      {
        delegated_user_pkce: "Your Oda account (OAuth)",
        federation: "Managed identity federation",
        api_key: configured ? "API key configured (secret hidden)" : "API key",
      }[mode] || "Not reported"
    );
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
        : "Integration status is unavailable. You can retry without leaving this page.",
      odaStatusError: odaOk
        ? null
        : "Oda status is unavailable. Other connection details remain available.",
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
    integrationStatusLabel,
    integrationAuthLabel,
    normalizeIntegrationResponse,
    connectionStatusSnapshot,
    odaNeedsReconnect,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenSettingsView;
})(typeof window === "undefined" ? globalThis : window);
