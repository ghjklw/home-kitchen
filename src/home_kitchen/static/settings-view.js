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

  function integrationDisplayName(integration) {
    const id = integration?.id;
    const rawName = String(integration?.name || "").trim();
    if (id === "anthropic" && (!rawName || rawName === "Recipe assistant")) {
      return "Oppskriftsassistent";
    }
    return rawName || String(id || "");
  }

  function integrationDisplayDetail(integration, odaState = null) {
    const id = integration?.id;
    const rawDetail =
      id === "oda" && odaState
        ? String(odaState.detail || integration?.detail || "").trim()
        : String(integration?.detail || "").trim();

    if (id === "oda") {
      if (rawDetail === "Connected") return "Tilkoblet";
      if (rawDetail === "Not connected") return "Ikke tilkoblet";
      if (
        rawDetail ===
        "Personal recipes and shopping require your Oda consent. Checkout and payment stay at Oda."
      ) {
        return "Personlige oppskrifter og handel krever samtykket ditt hos Oda. Kasse og betaling skjer hos Oda.";
      }
      if (rawDetail.startsWith("Configure the public URL (HTTP loopback")) {
        return "Konfigurer offentlig URL (HTTP loopback er tillatt i lokal utvikling) og Oda-krypteringsnøkkel for å koble til";
      }
      if (rawDetail.startsWith("Configure the public HTTPS URL")) {
        return "Konfigurer offentlig HTTPS-URL og Oda-krypteringsnøkkel for å koble til";
      }
      return rawDetail || "Ingen tilkoblingsdetaljer for Oda er tilgjengelige.";
    }

    if (id === "anthropic") {
      if (
        rawDetail ===
        "Configured by the operator. Federation is preferred where supported; configuration does not prove a successful model call."
      ) {
        return "Konfigurert av operatør. Føderering foretrekkes der det støttes; konfigurasjon beviser ikke et vellykket modellkall.";
      }
    }

    if (id === "home_assistant" && rawDetail.includes("Kitchen forblir fasit.")) {
      return rawDetail.replace("Kitchen forblir fasit.", "Kjøkken forblir fasit.");
    }

    return rawDetail || "Ingen tilkoblingsdetaljer er tilgjengelige.";
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
        oauth_pkce: configured ? "OAuth-innlogging er lagret (hemmeligheten er skjult)" : "OAuth-innlogging (PKCE)",
        oauth_password: configured ? "OAuth-økt er lagret (hemmeligheten er skjult)" : "E-post og passord",
        session_token: configured ? "Sesjonstoken er lagret (hemmeligheten er skjult)" : "Sesjonstoken",
        session_cookie: configured ? "Sesjonsinformasjonskapsel er lagret (hemmeligheten er skjult)" : "Sesjonsinformasjonskapsel",
        credentials: configured ? "Brukernavn og passord er lagret (hemmeligheten er skjult)" : "Brukernavn og passord",
        manual_session_token: configured ? "Sesjonstoken er lagret (hemmeligheten er skjult)" : "Sesjonstoken",
        manual_session_cookie: configured ? "Sesjonsinformasjonskapsel er lagret (hemmeligheten er skjult)" : "Sesjonsinformasjonskapsel",
      }[mode] || "Ikke oppgitt"
    );
  }

  function accountDisplayName(account) {
    if (!account) return "Standardkonto";
    const label = String(account.account_label || "").trim();
    if (label) return label;
    return account.account_id === "default" || !account.account_id
      ? "Standardkonto"
      : String(account.account_id);
  }

  function resolveIntlLocale(locale = "nb") {
    const raw = String(locale ?? "").trim();
    const lower = raw.toLowerCase();
    if (!lower || lower === "nb" || lower === "nb-no" || lower === "und") return "nb-NO";
    if (lower === "en" || lower === "fr") return lower;
    return raw;
  }

  function formatAccountExpiry(account, locale = "nb") {
    const raw = account?.token_expires_at || account?.expires_at;
    if (!raw) return null;
    const date = new Date(raw);
    if (isNaN(date.getTime())) return null;
    try {
      return date.toLocaleDateString(resolveIntlLocale(locale), {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    } catch {
      return date.toISOString().slice(0, 10);
    }
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
      }
    }
    return null;
  }

  function providerDisplayName(providerId) {
    return (
      {
        oda: "Oda",
        rema1000: "Rema 1000",
        coop: "Coop",
        trumf: "Trumf",
        cookidoo: "Cookidoo",
        kassal: "Kassal.app",
        home_assistant: "Home Assistant",
        anthropic: "Oppskriftsassistent",
      }[String(providerId || "").trim().toLowerCase()] || String(providerId || "").trim()
    );
  }

  function formatSyncErrorMessage(rawError) {
    const text = String(rawError || "").trim();
    if (!text) return "";

    const truncatedMatch = /^(?:([A-Za-z0-9 .]+?)\s+history scan\s+)?[Tt]runcated at page\s+(\d+)\b(.*)$/.exec(
      text,
    );
    if (truncatedMatch) {
      const providerPart = truncatedMatch[1] ? ` for ${truncatedMatch[1].trim()}` : "";
      const pageNum = truncatedMatch[2];
      const tail = truncatedMatch[3] || "";
      if (/backfill is incomplete/i.test(tail)) {
        return `Kvitteringssøk${providerPart} ble avbrutt ved side ${pageNum} (uferdig historikk). Tilgjengelige kvitteringer er lagret, og neste søk starter på side 1.`;
      }
      return `Kvitteringssøk${providerPart} ble avbrutt ved side ${pageNum}.`;
    }

    const dbReadMatch = /^Database read failure\s*:\s*(.+)$/i.exec(text);
    if (dbReadMatch) {
      return `Kunne ikke lese fra databasen: ${dbReadMatch[1].trim()}`;
    }

    const prefixMap = [
      [/^Session expired(?:\s*[:;-]\s*(.*))?$/i, "Sesjonen har utløpt"],
      [/^Token expired(?:\s*[:;-]\s*(.*))?$/i, "Tilgangstokenet har utløpt"],
      [/^(?:Authentication failed|Unauthorized|Invalid credentials)(?:\s*[:;-]\s*(.*))?$/i, "Autentisering feilet"],
      [/^(?:Connection failed|Network error)(?:\s*[:;-]\s*(.*))?$/i, "Tilkoblingsfeil"],
      [/^(?:Request timed out|Timeout(?: error)?)(?:\s*[:;-]\s*(.*))?$/i, "Forespørselen fikk tidsavbrudd"],
      [/^(?:Sync failed|Receipt sync failed)(?:\s*[:;-]\s*(.*))?$/i, "Synkronisering feilet"],
      [/^Refresh failed(?:\s*[:;-]\s*(.*))?$/i, "Fornying av tilkobling feilet"],
      [/^Rate limited(?:\s*[:;-]\s*(.*))?$/i, "For mange forespørsler"],
      [/^Your Oda connection (?:expired|cannot be read).*$/i, "Oda-tilkoblingen har utløpt eller kan ikke leses. Koble til på nytt."],
      [/^Connect your Oda account first$/i, "Koble til Oda-kontoen din først."],
    ];

    for (const [pattern, localizedPrefix] of prefixMap) {
      const m = pattern.exec(text);
      if (m) {
        const rest = String(m[1] || "").trim();
        return rest ? `${localizedPrefix}: ${rest}` : localizedPrefix;
      }
    }

    return text;
  }

  function formatIntegrationIssueMessage(rawIssue) {
    const text = String(rawIssue || "").trim();
    if (!text) return "";

    const receiptSyncMatch = /^Kvitteringssynk\s*\(([^)]+)\)\s*:\s*(.+)$/i.exec(text);
    if (receiptSyncMatch) {
      const providerName = providerDisplayName(receiptSyncMatch[1]);
      const localizedBody = formatSyncErrorMessage(receiptSyncMatch[2]);
      return `Kvitteringssynk (${providerName}): ${localizedBody}`;
    }

    const prefixedMatch = /^([^:]{2,40}):\s+(.+)$/.exec(text);
    if (prefixedMatch) {
      const prefix = prefixedMatch[1].trim();
      const body = prefixedMatch[2].trim();
      const localizedPrefix =
        prefix === "Recipe assistant" ? "Oppskriftsassistent" : prefix;
      const localizedBody = formatSyncErrorMessage(body);
      return `${localizedPrefix}: ${localizedBody}`;
    }

    return formatSyncErrorMessage(text);
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

  function cookidooNeedsReconnect(message) {
    return /cookidoo.*(?:utløpt|ikke tilkoblet|ugyldig|koble til)/iu.test(message || "");
  }

  function parsePkceCompletionPayload(input) {
    const raw = String(input || "").trim();
    if (!raw) return null;
    const isUrl = /^https?:\/\//i.test(raw) || /^[a-z0-9+.-]+:\/\//i.test(raw);
    return isUrl ? { redirect_url: raw } : { code: raw };
  }

  function buildCookidooLoginPayload(email, password, country = "no") {
    const trimmedEmail = String(email || "").trim();
    const trimmedPassword = String(password || "").trim();
    const trimmedCountry = String(country || "").trim().toLowerCase() || "no";
    if (!trimmedEmail || !trimmedPassword) return null;
    return {
      email: trimmedEmail,
      password: trimmedPassword,
      country: trimmedCountry,
    };
  }

  function buildTrumfSessionCookie(cookieOrHeader) {
    const full = String(cookieOrHeader || "")
      .trim()
      .replace(/^(?:cookie|set-cookie):\s*/i, "")
      .trim();

    const chunkMap = new Map();
    if (full) {
      for (const part of full.split(/[\r\n;]+/)) {
        const trimmed = part.trim();
        if (!trimmed) continue;
        const eqIdx = trimmed.indexOf("=");
        if (eqIdx > 0) {
          const name = trimmed.slice(0, eqIdx).trim();
          const val = trimmed.slice(eqIdx + 1).trim();
          if (name) chunkMap.set(name, val);
        }
      }
    }

    if (chunkMap.size > 0) {
      return Array.from(chunkMap.entries())
        .map(([name, val]) => `${name}=${val}`)
        .join("; ");
    }
    return full;
  }

  function buildTrumfConnectPayload(cookieOrToken, expiryDate) {
    const trimmed = buildTrumfSessionCookie(cookieOrToken);
    if (!trimmed) return null;
    const payload = {
      auth_mode: "session_cookie",
      session_cookie: trimmed,
    };
    if (expiryDate) {
      payload.expires_at = new Date(
        expiryDate + (String(expiryDate).length === 10 ? "T23:59:59Z" : "")
      ).toISOString();
    }
    return payload;
  }

  function buildKassalConnectPayload(apiKey) {
    const trimmed = String(apiKey || "").trim();
    if (!trimmed) return null;
    return {
      auth_mode: "api_key",
      api_key: trimmed,
    };
  }

  function buildHomeAssistantConnectPayload(baseUrl, apiKey, entityId = "todo.shopping_list") {
    const trimmedUrl = String(baseUrl || "").trim().replace(/\/+$/, "");
    const trimmedKey = String(apiKey || "").trim();
    const trimmedEntity = String(entityId ?? "").trim();
    if (!trimmedUrl || !trimmedKey) return null;
    return {
      auth_mode: "api_key",
      base_url: trimmedUrl,
      api_key: trimmedKey,
      entity_id: trimmedEntity || "todo.shopping_list",
    };
  }

  root.KitchenSettingsView = {
    DEFAULT_SHOPPING_CATEGORY_ORDER,
    moveShoppingCategory,
    normalizeShoppingCategoryOrder,
    createShoppingCategoryRow,
    editShoppingCategoryRow,
    integrationDisplayName,
    integrationDisplayDetail,
    integrationStatusLabel,
    integrationAuthLabel,
    accountDisplayName,
    formatAccountExpiry,
    normalizeIntegrationResponse,
    connectionStatusSnapshot,
    odaNeedsReconnect,
    cookidooNeedsReconnect,
    integrationExpiryNotice,
    providerDisplayName,
    formatSyncErrorMessage,
    formatIntegrationIssueMessage,
    parsePkceCompletionPayload,
    buildCookidooLoginPayload,
    buildTrumfSessionCookie,
    buildTrumfConnectPayload,
    buildKassalConnectPayload,
    buildHomeAssistantConnectPayload,
  };
  if (typeof module !== "undefined" && module.exports)
    module.exports = root.KitchenSettingsView;
})(typeof window === "undefined" ? globalThis : window);
