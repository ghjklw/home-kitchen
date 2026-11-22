(function (root, factory) {
  const assistantChat = factory();
  if (typeof module === "object" && module.exports) module.exports = assistantChat;
  if (root && root.document) assistantChat.initialize(root);
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const MAX_VISIBLE_TEXT = 4000;
  const MAX_SELECTION = 500;
  const MAX_ACTIVITY = 800;
  const MAX_HISTORY_TURNS = 20;
  const MAX_HISTORY_CHARS = 24000;
  const MAX_MESSAGE_LENGTH = 4000;
  const EXCLUDED_TAGS = new Set([
    "INPUT",
    "TEXTAREA",
    "SELECT",
    "OPTION",
    "SCRIPT",
    "STYLE",
    "NOSCRIPT",
    "SVG",
  ]);

  function clip(value, maxLength) {
    return String(value ?? "").replace(/\s+/gu, " ").trim().slice(0, maxLength);
  }

  function isRecipeId(value) {
    return typeof value === "string" &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu.test(value);
  }

  function redactCredentials(value) {
    return String(value ?? "")
      .replace(/\bBearer\s+[^\s,;]+/giu, "Bearer [skjult]")
      .replace(/\b(?:sk-ant-api|sk-proj|sk_live|ghp_|gho_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}/giu, "[skjult token]")
      .replace(/\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/gu, "[skjult token]");
  }

  function isElementVisible(element, documentRef) {
    if (!element || element.nodeType !== 1) return true;
    if (EXCLUDED_TAGS.has(String(element.tagName || "").toUpperCase())) return false;
    if (element.hidden || element.inert || element.isContentEditable) return false;
    if (element.getAttribute?.("hidden") !== null && element.getAttribute?.("hidden") !== undefined) return false;
    if (element.getAttribute?.("aria-hidden") === "true") return false;

    const style = documentRef?.defaultView?.getComputedStyle?.(element);
    return !style || (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      style.visibility !== "collapse" &&
      style.contentVisibility !== "hidden" &&
      style.opacity !== "0"
    );
  }

  function visibleText(rootNode, documentRef, maxLength = MAX_VISIBLE_TEXT) {
    if (!rootNode) return "";
    const pieces = [];
    function visit(node, ancestorsVisible) {
      if (!node) return;
      if (node.nodeType === 3) {
        if (ancestorsVisible) pieces.push(node.nodeValue ?? node.textContent ?? "");
        return;
      }
      if (node.nodeType === 1 && !isElementVisible(node, documentRef)) return;
      const children = node.childNodes || [];
      for (let index = 0; index < children.length; index += 1)
        visit(children[index], ancestorsVisible);
    }
    const children = rootNode.childNodes || [];
    for (let index = 0; index < children.length; index += 1) visit(children[index], true);
    return clip(pieces.join(" "), maxLength);
  }

  function buildClientContext({ rootNode, documentRef, locationRef, selection = "", activity = null } = {}) {
    const heading = rootNode?.querySelector?.("h1");
    const page = redactCredentials(visibleText(heading || rootNode, documentRef, 180)) || "Kjøkken";
    const content = redactCredentials(visibleText(rootNode, documentRef, MAX_VISIBLE_TEXT));
    const hash = String(locationRef?.hash || "");
    const route = /^#[a-z-]+$/iu.test(hash) ? hash : clip(locationRef?.pathname || "/", 120);
    const activitySummary = [
      activity ? `Siste aktivitet: ${clip(activity, 300)}` : "",
      selection ? `Markert tekst: ${clip(selection, MAX_SELECTION)}` : "",
    ].filter(Boolean).join(". ");
    return {
      route,
      page,
      title: clip(documentRef?.title || "Kjøkken", 180),
      visible_text: content ? `Synlig innhold på siden: ${content}` : "Ingen synlig sidetekst.",
      activity: redactCredentials(clip(activitySummary, MAX_ACTIVITY)),
    };
  }

  function boundedHistory(history) {
    const bounded = (Array.isArray(history) ? history : [])
      .filter((turn) => turn && (turn.role === "user" || turn.role === "assistant"))
      .map((turn) => ({ role: turn.role, content: clip(turn.content, MAX_MESSAGE_LENGTH) }))
      .filter((turn) => turn.content)
      .slice(-MAX_HISTORY_TURNS * 2);
    let totalCharacters = bounded.reduce((total, turn) => total + turn.content.length, 0);
    while (totalCharacters > MAX_HISTORY_CHARS && bounded.length > 1) {
      const removeCount = bounded.length >= 2 ? 2 : 1;
      for (const turn of bounded.splice(0, removeCount)) totalCharacters -= turn.content.length;
    }
    return bounded;
  }

  function requestError(status, kind) {
    if (status === 401 || status === 403)
      return "Logg inn via husholdningens godkjente tilgangslenke, og prøv igjen.";
    if (kind === "delete" && status === 404)
      return "Oppskriften finnes ikke lenger. Den kan ha blitt slettet tidligere.";
    if (kind === "delete" && status === 422)
      return "Oppskriften kan ikke slettes før planlagte måltider er fjernet.";
    if (kind === "delete" && status >= 500)
      return "Kjøkken kunne ikke bekrefte om oppskriften ble slettet. Kontroller Oppskrifter før du prøver igjen.";
    if (kind === "review" && status === 404)
      return "Forslaget er ikke tilgjengelig. Det kan ha utløpt.";
    if (kind === "confirm" && status === 404)
      return "Forslaget er ikke tilgjengelig. Det kan ha utløpt.";
    if (kind === "confirm" && status === 409)
      return "Forslaget er utdatert. Ingen endringer ble lagret. Be om et nytt forslag.";
    if (kind === "confirm" && status >= 500)
      return "Kjøkken kunne ikke avklare om forslaget ble lagret. Kontroller status før du prøver igjen.";
    if (status === 503 && kind === "assistant")
      return "Kjøkken-assistenten er ikke konfigurert ennå.";
    return kind === "review"
      ? "Forslaget kunne ikke hentes. Prøv igjen."
      : "Forespørselen kunne ikke fullføres. Prøv igjen.";
  }

  function formatDate(value) {
    const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/u);
    return match ? `${match[3]}.${match[2]}.${match[1]}` : "";
  }

  function actionDescription(action) {
    const summary = clip(action.summary, 500);
    const proposalType = ({
      meal_plan_proposal: "Ukeplan",
      oda_cart_proposal: "Oda-handlekurv",
      recipe_draft: "Oppskriftsutkast",
      recipe_delete_proposal: "Sletteforslag",
      recipe_saved: "Lagret oppskrift",
      basket_refinement: "Handlelisteforslag",
    })[action.type] || "";
    const label = action.status === "applied" ? "Utført" : "Forslag";
    if (summary) return `${label}${proposalType ? ` · ${proposalType}` : ""}: ${summary}`;
    const verb = ({ add: "Legg til", remove: "Fjern", replace: "Bytt ut" })[action.action] || "Endring";
    const subject = clip(
      action.recipe_title || action.previous_recipe_title || action.label || "måltid",
      180,
    );
    const date = formatDate(action.date || action.previous_date);
    return [label + (proposalType ? ` · ${proposalType}:` : ":"), verb, subject, date ? `(${date})` : ""].filter(Boolean).join(" ");
  }

  function responseNotices(response) {
    const notices = [];
    const actions = Array.isArray(response?.changes)
      ? response.changes
      : Array.isArray(response?.actions)
        ? response.actions
        : [];
    const hasConfirmableProposal = actions.some((action) => action?.confirm_endpoint);
    const hasPendingAction = actions.some((action) =>
      action?.status !== "applied" && action?.type !== "recipe_saved",
    );
    const hasSavedRecipe = actions.some((action) => action?.type === "recipe_saved");
    if (response?.confirmation_required === true || response?.requires_confirmation === true) {
      notices.push("Krever bekreftelse. Endringen er ikke lagret.");
    } else if (hasConfirmableProposal) {
      notices.push("Dette er forslag. Ingen endringer er lagret før du har sett dem over og bekreftet dem.");
    } else if (hasPendingAction) {
      notices.push("Dette er forslag til gjennomgang. Ingenting er lagret.");
    } else if (response?.applied === true || hasSavedRecipe) {
      notices.push("Endringene er lagret.");
    } else if (actions.length) {
      notices.push("Dette er forslag til gjennomgang. Ingenting er lagret.");
    }

    actions.slice(0, 20).forEach((action) => {
      if (action && typeof action === "object") notices.push(actionDescription(action));
    });

    const review = response?.review || response?.action_review;
    if (review && typeof review === "object") {
      const label = clip(review.title || review.status || "Gjennomgang", 120);
      const detail = clip(review.summary || review.message || review.detail, 500);
      notices.push([label, detail].filter(Boolean).join(": "));
    }
    if (Array.isArray(response?.notes))
      response.notes.slice(0, 10).forEach((note) => {
        if (typeof note === "string" && clip(note, 500)) notices.push(clip(note, 500));
      });
    return notices;
  }

  function createMessage(documentRef, role, content, className = "") {
    const article = documentRef.createElement("article");
    article.className = `assistant-message assistant-message-${role}${className ? " " + className : ""}`;
    const label = documentRef.createElement("p");
    label.className = "assistant-message-label";
    label.textContent = role === "user" ? "Du" : role === "error" ? "Feil" : "Kjøkken-assistenten";
    const text = documentRef.createElement("p");
    text.className = "assistant-message-text";
    text.textContent = content;
    article.append(label, text);
    return article;
  }

  function initialize(windowRef) {
    const documentRef = windowRef.document;
    const toggle = documentRef.querySelector("#assistant-toggle");
    const panel = documentRef.querySelector("#assistant-panel");
    const closeButton = documentRef.querySelector("#assistant-close");
    const messagesNode = documentRef.querySelector("#assistant-messages");
    const form = documentRef.querySelector("#assistant-form");
    const input = documentRef.querySelector("#assistant-input");
    const sendButton = documentRef.querySelector("#assistant-send");
    const status = documentRef.querySelector("#assistant-status");
    if (!toggle || !panel || !messagesNode || !form || !input || !sendButton) return null;

    const history = [];
    let busy = false;
    let lastAction = "";
    const main = documentRef.querySelector("#main");

    function setOpen(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) input.focus();
      else toggle.focus();
    }

    function appendMessage(role, content, className) {
      const article = createMessage(documentRef, role, content, className);
      messagesNode.append(article);
      article.scrollIntoView?.({ block: "nearest" });
      return article;
    }

    function appendNotices(notices, actions = []) {
      if (!notices.length && !actions.length) return;
      const section = documentRef.createElement("section");
      section.className = "assistant-review";
      section.setAttribute("aria-label", "Handlinger og gjennomgang");
      const heading = documentRef.createElement("h3");
      heading.textContent = "Handlinger og gjennomgang";
      const list = documentRef.createElement("ul");
      notices.forEach((notice) => {
        const item = documentRef.createElement("li");
        item.textContent = notice;
        list.append(item);
      });
      section.append(heading, list);
      actions.slice(0, 10).forEach((action) => {
        if (action?.type === "meal_plan_proposal" && action.proposal_id) {
          const details = documentRef.createElement("details");
          details.className = "assistant-action-review";
          const summary = documentRef.createElement("summary");
          summary.textContent = "Se gjennom ukeplanforslaget";
          const content = documentRef.createElement("div");
          const status = documentRef.createElement("p");
          status.className = "muted";
          status.textContent = "Forslaget er ikke lagret. Se gjennom detaljene før du bestemmer deg.";
          const load = documentRef.createElement("button");
          load.type = "button";
          load.className = "assistant-followup";
          load.textContent = "Hent forslagsdetaljer";
          load.addEventListener("click", async () => {
            load.disabled = true;
            load.textContent = "Henter forslag …";
            let proposal = null;
            let recipes = [];
            let confirmationBusy = false;
            let confirmationUnknown = false;
            let statusMessage = "";

            const render = () => renderMealProposalDetails(documentRef, content, proposal, recipes, {
              busy: confirmationBusy,
              uncertain: confirmationUnknown,
              message: statusMessage,
              onConfirm: confirmProposal,
              onRefresh: refreshProposal,
            });

            async function refreshProposal() {
              try {
                proposal = await fetchReview(windowRef, "/api/meal-plan-proposals/" + encodeURIComponent(action.proposal_id));
                try {
                  recipes = await fetchReview(windowRef, "/api/recipes?include_stats=true");
                } catch {
                  recipes = [];
                }
                confirmationUnknown = false;
                statusMessage = "";
                load.hidden = true;
                render();
              } catch (error) {
                status.textContent = error?.message || "Forslaget kunne ikke hentes. Prøv igjen.";
                load.disabled = false;
                load.textContent = "Prøv å hente forslaget igjen";
              }
            }

            async function confirmProposal() {
              if (confirmationBusy || confirmationUnknown || proposal?.status !== "pending") return;
              confirmationBusy = true;
              statusMessage = "Lagrer ukeplanen …";
              render();
              let response;
              try {
                response = await windowRef.fetch(
                  "/api/meal-plan-proposals/" + encodeURIComponent(action.proposal_id) + "/confirm",
                  {
                    method: "POST",
                    credentials: "same-origin",
                    headers: { "Content-Type": "application/json" },
                    cache: "no-store",
                    body: JSON.stringify({ reviewed: true }),
                  },
                );
              } catch {
                confirmationUnknown = true;
                statusMessage = "Fikk ikke svar på bekreftelsen. Kontroller forslagsstatus før du eventuelt prøver igjen.";
                confirmationBusy = false;
                render();
                return;
              }

              let result = null;
              try { result = await response.json(); } catch {}
              confirmationBusy = false;
              if (!response.ok) {
                statusMessage = requestError(response.status, "confirm");
                if (response.status === 409) proposal.status = "stale";
                if (response.status === 404) proposal.status = "expired";
                if (response.status >= 500) confirmationUnknown = true;
                render();
                return;
              }

              proposal = result || proposal;
              if (proposal.status === "applied") {
                statusMessage = "Ukeplanforslaget er bekreftet og lagret.";
                confirmationUnknown = false;
                render();
                if (windowRef.location?.hash === "#week") {
                  try { await windowRef.refreshView?.(); } catch {}
                }
              } else if (proposal.status === "stale" || proposal.status === "expired") {
                statusMessage = proposal.status === "stale"
                  ? "Forslaget er utdatert. Ingen endringer ble lagret. Be om et nytt forslag."
                  : "Forslaget har utløpt. Ingen endringer ble lagret. Be om et nytt forslag.";
                render();
              } else {
                statusMessage = "Forslaget venter fortsatt på bekreftelse. Ingen endringer er lagret.";
                render();
              }
            }

            try {
              await refreshProposal();
            } catch (error) {
              status.textContent = error?.message || "Forslaget kunne ikke hentes. Prøv igjen.";
              load.disabled = false;
              load.textContent = "Prøv å hente forslaget igjen";
            }
          });
          const openPlan = documentRef.createElement("a");
          openPlan.href = "#week";
          openPlan.textContent = "Åpne Plan";
          content.append(status, load, openPlan);
          details.append(summary, content);
          section.append(details);
        } else if (action?.type === "recipe_delete_proposal") {
          const card = documentRef.createElement("article");
          card.className = "assistant-delete-review";
          card.setAttribute("aria-label", "Gjennomgang av sletting av oppskrift");
          const title = typeof action.recipe_title === "string"
            ? action.recipe_title.trim().slice(0, 500)
            : "";
          const recipeHeading = documentRef.createElement("h4");
          recipeHeading.textContent = title || "Oppskrift uten navn";
          const warning = documentRef.createElement("p");
          warning.className = "assistant-delete-warning";
          warning.textContent = title
            ? `Sletting fjerner «${title}» fra oppskriftsamlingen. Handlingen kan ikke angres.`
            : "Oppskriften kan ikke identifiseres med navn. Ingen sletting er tilgjengelig.";
          const deleteStatus = documentRef.createElement("p");
          deleteStatus.className = "assistant-proposal-status";
          deleteStatus.setAttribute("role", "status");
          deleteStatus.setAttribute("aria-live", "polite");
          deleteStatus.textContent = "Se over oppskriften og advarselen før du velger å slette.";
          const remove = documentRef.createElement("button");
          remove.type = "button";
          remove.className = "danger assistant-delete-button";
          remove.textContent = "Slett oppskriften";
          const recipeId = action.recipe_id;
          remove.disabled = !title || !isRecipeId(recipeId);
          if (!title || !isRecipeId(recipeId))
            deleteStatus.textContent = "Sletteforslaget mangler en gyldig oppskriftsreferanse eller tittel. Ingen sletting er tilgjengelig.";
          let deleting = false;
          let terminal = false;
          remove.addEventListener("click", async () => {
            if (deleting || terminal || !isRecipeId(recipeId) || !title) return;
            deleting = true;
            remove.disabled = true;
            remove.textContent = "Sletter oppskriften …";
            deleteStatus.textContent = `Sletter «${title}» …`;
            let response;
            try {
              response = await windowRef.fetch("/api/recipes/" + encodeURIComponent(recipeId), {
                method: "DELETE",
                credentials: "same-origin",
                cache: "no-store",
              });
            } catch {
              terminal = true;
              deleteStatus.textContent = "Fikk ikke svar på slettingen. Oppskriften kan allerede være slettet. Kontroller Oppskrifter før du gjør noe mer.";
              remove.textContent = "Kontroller Oppskrifter";
              deleting = false;
              return;
            }

            let result = null;
            try { result = await response.json(); } catch {}
            deleting = false;
            if (response.status === 404) {
              terminal = true;
              deleteStatus.textContent = "Oppskriften finnes ikke lenger. Den kan ha blitt slettet tidligere.";
              remove.textContent = "Oppskriften finnes ikke";
              return;
            }
            if (response.status >= 500) {
              terminal = true;
              deleteStatus.textContent = requestError(response.status, "delete");
              remove.textContent = "Kontroller Oppskrifter";
              return;
            }
            if (!response.ok) {
              deleteStatus.textContent = response.status === 422
                ? requestError(response.status, "delete")
                : response.status === 401 || response.status === 403
                  ? requestError(response.status, "delete")
                  : "Oppskriften kunne ikke slettes. Kjøkken avviste forespørselen.";
              remove.disabled = false;
              remove.textContent = "Slett oppskriften";
              return;
            }
            if (result?.deleted !== true) {
              terminal = true;
              deleteStatus.textContent = "Kjøkken bekreftet ikke slettingen. Kontroller Oppskrifter før du gjør noe mer.";
              remove.textContent = "Kontroller Oppskrifter";
              return;
            }

            terminal = true;
            deleteStatus.textContent = `Oppskriften «${title}» er slettet.`;
            remove.textContent = "Oppskriften er slettet";
            if (typeof windowRef.navigate === "function") windowRef.navigate("recipes");
            else windowRef.location.hash = "#recipes";
          });
          card.append(recipeHeading, warning, deleteStatus, remove);
          section.append(card);
        } else if (action?.type === "oda_cart_proposal" && action.proposal_id) {
          const review = documentRef.createElement("button");
          review.type = "button";
          review.className = "assistant-followup";
          review.textContent = "Se gjennom Oda-forslaget";
          review.addEventListener("click", async () => {
            review.disabled = true;
            review.textContent = "Henter Oda-forslaget …";
            try {
              const proposal = await fetchReview(windowRef, "/api/oda/cart/proposals/" + encodeURIComponent(action.proposal_id));
              if (typeof windowRef.renderOdaProposal !== "function")
                throw new Error("Oda-gjennomgangen er ikke tilgjengelig. Oppdater siden og prøv igjen.");
              windowRef.renderOdaProposal(proposal, { additions: [] });
              review.disabled = false;
              review.textContent = "Se gjennom Oda-forslaget";
            } catch (error) {
              review.disabled = false;
              review.textContent = error?.message || "Oda-forslaget kunne ikke hentes. Prøv igjen.";
            }
          });
          section.append(review);
        }
      });
      messagesNode.append(section);
    }

    async function fetchReview(windowObject, path) {
      let response;
      try {
        response = await windowObject.fetch(path, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
        });
      } catch {
        throw new Error("Fikk ikke kontakt med tjenesten. Sjekk tilkoblingen og prøv igjen.");
      }
      let data = null;
      try { data = await response.json(); } catch {}
      if (!response.ok) throw new Error(requestError(response.status, "review"));
      return data;
    }

    function renderMealProposalDetails(documentObject, host, proposal, recipes = [], options = {}) {
      host.replaceChildren();
      const heading = documentObject.createElement("h3");
      heading.textContent = proposal?.status === "applied"
        ? "Ukeplanforslag – lagret"
        : "Ukeplanforslag – ikke lagret";
      const entries = Array.isArray(proposal?.meal_entries)
        ? proposal.meal_entries
        : Array.isArray(proposal?.additions) || Array.isArray(proposal?.edits)
          ? [...(proposal.additions || []), ...(proposal.edits || [])]
          : [];
      const recipeById = new Map((Array.isArray(recipes) ? recipes : []).map((recipe) => [String(recipe.id), recipe]));
      const existingMeals = proposal?.existing_meals && !Array.isArray(proposal.existing_meals)
        ? proposal.existing_meals
        : {};
      const statusLabels = {
        pending: "Venter på gjennomgang og bekreftelse",
        applied: "Endringene er lagret",
        expired: "Forslaget har utløpt",
        stale: "Forslaget er utdatert",
      };
      const expiresAt = Date.parse(proposal?.expires_at || "");
      const isExpired = proposal?.status === "expired" ||
        (Number.isFinite(expiresAt) && expiresAt <= Date.now());
      const status = documentObject.createElement("p");
      status.className = "assistant-proposal-status";
      status.setAttribute("role", "status");
      status.textContent = options.message || (isExpired
        ? "Forslaget har utløpt. Ingen endringer er lagret. Be om et nytt forslag."
        : statusLabels[proposal?.status] || "Forslaget er ikke bekreftet." );

      const list = documentObject.createElement("ul");
      entries.slice(0, 90).forEach((entry) => {
        const item = documentObject.createElement("li");
        const recipe = recipeById.get(String(entry.recipe_id));
        const recipeId = clip(entry.recipe_id, 80);
        const recipeTitle = clip(
          recipe?.title || entry.recipe_title || (recipeId ? `Oppskrift ${recipeId}` : "Oppskrift uten tittel"),
          180,
        );
        const existing = entry.meal_id ? existingMeals[String(entry.meal_id)] : null;
        const oldRecipe = existing ? recipeById.get(String(existing.recipe_id)) : null;
        const change = documentObject.createElement("strong");
        change.textContent = entry.meal_id
          ? `Endre ${clip(oldRecipe?.title || "eksisterende måltid", 180)} til ${recipeTitle}`
          : `Legg til ${recipeTitle}`;
        const metadata = documentObject.createElement("p");
        const parts = [
          formatDate(entry.date),
          entry.scheduled_time ? `kl. ${clip(entry.scheduled_time, 20)}` : "",
          entry.servings ? `${entry.servings} porsjoner` : "",
          entry.label ? clip(entry.label, 80) : "",
        ];
        metadata.textContent = parts.filter(Boolean).join(" · ");
        item.append(change, metadata);
        if (existing) {
          const current = documentObject.createElement("p");
          const existingDetails = [
            "Nåværende måltid",
            formatDate(existing.date),
            existing.scheduled_time ? `kl. ${clip(existing.scheduled_time, 20)}` : "",
            existing.servings ? `${existing.servings} porsjoner` : "",
            existing.label ? clip(existing.label, 80) : "",
          ];
          current.textContent = existingDetails.filter(Boolean).join(" · ");
          item.append(current);
        }
        list.append(item);
      });
      if (!entries.length) {
        const empty = documentObject.createElement("li");
        empty.textContent = "Forslaget inneholder ingen måltider som kan vises.";
        list.append(empty);
      }
      host.append(heading, status, list);
      if (entries.length > 90) {
        const limit = documentObject.createElement("p");
        limit.className = "muted";
        limit.textContent = "Viser de første 90 måltidene.";
        host.append(limit);
      }
      if (proposal?.created_at) {
        const created = documentObject.createElement("p");
        created.className = "muted";
        created.textContent = "Opprettet: " + formatTimestamp(proposal.created_at);
        host.append(created);
      }
      if (proposal?.expires_at) {
        const expiry = documentObject.createElement("p");
        expiry.className = "muted";
        expiry.textContent = "Utløper: " + formatTimestamp(proposal.expires_at);
        host.append(expiry);
      }
      if (options.uncertain) {
        const check = documentObject.createElement("button");
        check.type = "button";
        check.className = "assistant-followup";
        check.textContent = "Kontroller forslagsstatus";
        check.addEventListener("click", () => options.onRefresh?.());
        host.append(check);
      } else if (proposal?.status === "pending" && !isExpired) {
        const confirm = documentObject.createElement("button");
        confirm.type = "button";
        confirm.className = "primary assistant-proposal-confirm";
        confirm.textContent = options.busy ? "Lagrer ukeplan …" : "Bekreft og lagre ukeplan";
        confirm.disabled = options.busy === true;
        confirm.addEventListener("click", () => options.onConfirm?.());
        host.append(confirm);
      }
      const openPlan = documentObject.createElement("a");
      openPlan.href = "#week";
      openPlan.textContent = "Åpne Plan";
      host.append(openPlan);
    }

    function formatTimestamp(value) {
      const timestamp = Date.parse(String(value || ""));
      if (!Number.isFinite(timestamp)) return clip(value, 80);
      return new Intl.DateTimeFormat("nb-NO", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(timestamp);
    }

    function appendFollowups(followups) {
      if (!Array.isArray(followups) || !followups.length) return;
      const section = documentRef.createElement("section");
      section.className = "assistant-followups";
      const heading = documentRef.createElement("h3");
      heading.textContent = "Du kan også spørre om";
      section.append(heading);
      followups.slice(0, 5).forEach((item) => {
        if (typeof item !== "string" || !clip(item, 300)) return;
        const followup = documentRef.createElement("button");
        followup.type = "button";
        followup.className = "assistant-followup";
        followup.textContent = clip(item, 300);
        followup.addEventListener("click", () => {
          input.value = item;
          input.focus();
        });
        section.append(followup);
      });
      messagesNode.append(section);
    }

    function selectionText() {
      const selection = windowRef.getSelection?.();
      if (!selection || !selection.toString() || !main) return "";
      const anchor = selection.anchorNode;
      if (!anchor || !(anchor === main || main.contains(anchor))) return "";
      let element = anchor.nodeType === 1 ? anchor : anchor.parentElement || anchor.parentNode;
      while (element && element !== main) {
        if (!isElementVisible(element, documentRef)) return "";
        element = element.parentElement || element.parentNode;
      }
      return element === main && isElementVisible(main, documentRef) ? selection.toString() : "";
    }

    function contextForTurn() {
      return buildClientContext({
        rootNode: main,
        documentRef,
        locationRef: windowRef.location,
        selection: selectionText(),
        activity: lastAction,
      });
    }

    function interactionLabel(target) {
      if (!target || !main?.contains(target)) return "";
      const element = target.closest?.("button,a,input,textarea,select,[role='button'],[data-assistant-context]") || target;
      if (!isElementVisible(element, documentRef)) return "";
      const type = String(element.type || "").toLowerCase();
      if (type === "password" || element.autocomplete === "current-password" || element.autocomplete === "new-password") return "";
      const label = element.getAttribute?.("aria-label") || element.getAttribute?.("title") ||
        element.labels?.[0]?.textContent || visibleText(element, documentRef, MAX_ACTIVITY);
      if (/passord|password|hemmelig|secret|token|credential|legitimasjon|tilgangsnøkkel/iu.test(label)) return "";
      return clip(label, MAX_ACTIVITY);
    }

    main?.addEventListener("click", (event) => {
      const label = interactionLabel(event.target);
      if (label) lastAction = `Valgte: ${label}`;
    });
    main?.addEventListener("change", (event) => {
      const label = interactionLabel(event.target);
      if (label) lastAction = `Endret felt: ${label}`;
    });
    windowRef.addEventListener?.("hashchange", () => { lastAction = ""; });

    toggle.addEventListener("click", () => setOpen(panel.hidden));
    closeButton?.addEventListener("click", () => setOpen(false));
    documentRef.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !panel.hidden) setOpen(false);
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      const message = clip(input.value, MAX_MESSAGE_LENGTH);
      if (!message) return;

      busy = true;
      sendButton.disabled = true;
      input.disabled = true;
      if (status) status.textContent = "Assistenten tenker …";
      appendMessage("user", message);
      input.value = "";
      try {
        const response = await windowRef.fetch("/api/ai/assistant/chat", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          cache: "no-store",
          body: JSON.stringify({
            message,
            history: boundedHistory(history),
            client_context: contextForTurn(),
          }),
        });
        let data = null;
        try { data = await response.json(); } catch {}
        if (!response.ok) {
          throw new Error(requestError(response.status, "assistant"));
        }
        const reply = clip(data?.reply, 6000);
        if (!reply) throw new Error("Assistenten returnerte ikke et svar. Prøv igjen.");
        appendMessage("assistant", reply);
        appendNotices(responseNotices(data), data?.actions);
        appendFollowups(data?.suggested_followups);
        history.push({ role: "user", content: message }, { role: "assistant", content: reply });
        history.splice(0, Math.max(0, history.length - MAX_HISTORY_TURNS * 2));
        if (status) status.textContent = "Svar mottatt.";
      } catch (error) {
        appendMessage("error", error?.name === "TypeError"
          ? "Fikk ikke kontakt med assistenten. Sjekk tilkoblingen og prøv igjen."
          : error?.message || "Forespørselen kunne ikke fullføres. Prøv igjen.");
        if (status) status.textContent = "Meldingen kunne ikke sendes.";
      } finally {
        busy = false;
        sendButton.disabled = false;
        input.disabled = false;
        input.focus();
      }
    });

    return { history, contextForTurn, setOpen };
  }

  return { MAX_VISIBLE_TEXT, boundedHistory, buildClientContext, initialize, responseNotices, visibleText };
});
