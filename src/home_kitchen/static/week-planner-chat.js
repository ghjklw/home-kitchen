(function (root) {
  "use strict";

  const QUICK_PROMPTS = [
    "Foreslå en variert ukeplan",
    "Raske middager på hverdagene",
    "Bruk ingredienser vi har hjemme",
    "Bytt ut et måltid med noe nytt",
  ];

  function ensureState(state) {
    if (!state || typeof state !== "object") {
      throw new Error("Applikasjonstilstand mangler for ukeplan-chatten.");
    }
    if (!state.weekChat || typeof state.weekChat !== "object") {
      state.weekChat = {
        expanded: true,
        applyChanges: true,
        sending: false,
        applyingTurnIndex: null,
        draft: "",
        error: "",
        errorIsConfig: false,
        status: "",
        turns: [],
      };
    }
    if (!Array.isArray(state.weekChat.turns)) state.weekChat.turns = [];
    if (typeof state.weekChat.expanded !== "boolean") state.weekChat.expanded = true;
    if (typeof state.weekChat.applyChanges !== "boolean") state.weekChat.applyChanges = true;
    if (typeof state.weekChat.sending !== "boolean") state.weekChat.sending = false;
    if (typeof state.weekChat.draft !== "string") state.weekChat.draft = "";
    if (typeof state.weekChat.error !== "string") state.weekChat.error = "";
    if (typeof state.weekChat.status !== "string") state.weekChat.status = "";
    return state.weekChat;
  }

  function buildChatPayload({ weekStartIso, days = 7, message, turns = [], applyChanges = true }) {
    const trimmed = String(message ?? "").trim();
    if (!trimmed) {
      throw new Error("Skriv inn en melding til ukeplan-assistenten.");
    }
    if (trimmed.length > 4000) {
      throw new Error("Meldingen kan ikke være lengre enn 4000 tegn.");
    }
    const history = (Array.isArray(turns) ? turns : [])
      .filter(
        (turn) =>
          turn &&
          (turn.role === "user" || turn.role === "assistant") &&
          typeof turn.content === "string" &&
          turn.content.trim(),
      )
      .slice(-40)
      .map((turn) => ({ role: turn.role, content: turn.content.trim() }));
    return {
      start: weekStartIso,
      days,
      message: trimmed,
      history,
      apply_changes: Boolean(applyChanges),
    };
  }

  function changeActionLabel(change) {
    const applied = change?.status === "applied";
    if (change?.action === "add") return applied ? "Lagt til" : "Foreslått lagt til";
    if (change?.action === "replace") return applied ? "Byttet ut" : "Foreslått byttet ut";
    if (change?.action === "remove") return applied ? "Fjernet" : "Foreslått fjernet";
    return applied ? "Oppdatert" : "Foreslått";
  }

  function formatServingsText(servings) {
    const count = Number(servings);
    if (!Number.isFinite(count) || count <= 0) return "";
    return count === 1 ? "1 porsjon" : `${count} porsjoner`;
  }

  function formatChangeSummary(change, { formatDate, formatTime } = {}) {
    const dateText =
      typeof formatDate === "function" && change?.date
        ? formatDate(change.date)
        : String(change?.date || "");
    const timeText =
      change?.scheduled_time && typeof formatTime === "function"
        ? formatTime(change.scheduled_time)
        : change?.scheduled_time || "";
    const title =
      change?.action === "replace" && change?.previous_recipe_title
        ? `${change.previous_recipe_title} → ${change.recipe_title || "Ukjent oppskrift"}`
        : change?.recipe_title || "Ukjent oppskrift";
    const meta = [dateText, change?.label, timeText, formatServingsText(change?.servings)]
      .filter(Boolean)
      .join(" · ");
    return { title, meta };
  }

  function makeNode(doc, tag, text = "", className = "") {
    const node = doc.createElement(tag);
    if (text !== undefined && text !== null && text !== "") node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function renderSection(deps) {
    const {
      state,
      weekStartIso,
      days = 7,
      api,
      refreshView,
      dateLabel,
      formatUiTime,
      connectionSettingsButton,
    } = deps;
    const doc = deps.document || (typeof document !== "undefined" ? document : null);
    if (!doc) throw new Error("DOM document er ikke tilgjengelig.");
    const chat = ensureState(state);

    const formatDraftDate = (dateText) => {
      if (!dateText) return "";
      if (typeof dateLabel !== "function") return String(dateText);
      const parsed = new Date(dateText + "T12:00:00");
      return Number.isNaN(parsed.valueOf())
        ? String(dateText)
        : dateLabel(parsed, { weekday: "short", day: "numeric", month: "short" });
    };

    const panel = makeNode(doc, "section", "", "week-chat-panel");
    panel.setAttribute("aria-labelledby", "week-chat-heading");

    const header = makeNode(doc, "div", "", "week-chat-header");
    const titleWrap = makeNode(doc, "div", "", "week-chat-title-wrap");
    const heading = makeNode(doc, "h2", "KI-planlegger", "");
    heading.id = "week-chat-heading";
    const subtitle = makeNode(
      doc,
      "p",
      "Chat for å planlegge uken ut fra preferanser, tidskrav, råvarer hjemme og tidligere vurderinger.",
      "muted",
    );
    titleWrap.append(heading, subtitle);

    const headerActions = makeNode(doc, "div", "", "week-chat-header-actions");
    const resetBtn = makeNode(doc, "button", "Ny samtale", "quiet small");
    resetBtn.type = "button";
    resetBtn.disabled = chat.sending || chat.applyingTurnIndex !== null || !chat.turns.length;

    const toggleBtn = makeNode(
      doc,
      "button",
      chat.expanded ? "Skjul KI-chat" : "Vis KI-chat",
      "quiet small",
    );
    toggleBtn.type = "button";
    toggleBtn.setAttribute("aria-expanded", chat.expanded ? "true" : "false");

    headerActions.append(resetBtn, toggleBtn);
    header.append(titleWrap, headerActions);
    panel.append(header);

    const body = makeNode(doc, "div", "", "week-chat-body");
    body.hidden = !chat.expanded;

    const controlsRow = makeNode(doc, "div", "", "week-chat-controls");
    const applyCheckbox = makeNode(doc, "input", "", "");
    applyCheckbox.type = "checkbox";
    applyCheckbox.id = "week-chat-apply-toggle";
    applyCheckbox.checked = chat.applyChanges;
    applyCheckbox.disabled = chat.sending;
    const applyLabel = makeNode(doc, "label", "Bruk endringer direkte i ukeplanen", "week-chat-toggle");
    applyLabel.setAttribute("for", "week-chat-apply-toggle");
    applyLabel.prepend
      ? applyLabel.prepend(applyCheckbox)
      : (() => {
          const text = applyLabel.textContent;
          applyLabel.textContent = "";
          applyLabel.append(applyCheckbox, makeNode(doc, "span", text));
        })();
    controlsRow.append(applyLabel);

    const chips = makeNode(doc, "div", "", "week-chat-chips");
    chips.setAttribute("role", "group");
    chips.setAttribute("aria-label", "Hurtigforslag til ukeplanlegging");

    const log = makeNode(doc, "div", "", "week-chat-log");
    log.setAttribute("role", "log");
    log.setAttribute("aria-live", "polite");
    log.setAttribute("aria-label", "Samtale med ukeplan-assistenten");

    const statusNode = makeNode(doc, "p", chat.status, "muted week-chat-status");
    statusNode.setAttribute("role", "status");
    statusNode.setAttribute("aria-live", "polite");

    const errorNode = makeNode(doc, "div", "", "week-chat-error");
    errorNode.setAttribute("role", "alert");

    const form = makeNode(doc, "form", "", "week-chat-form");
    const inputLabel = makeNode(doc, "label", "Skriv til ukeplan-assistenten", "");
    inputLabel.setAttribute("for", "week-chat-input");
    const textarea = makeNode(doc, "textarea", "", "");
    textarea.id = "week-chat-input";
    textarea.name = "message";
    textarea.rows = 2;
    textarea.maxLength = 4000;
    textarea.placeholder =
      "F.eks. «Legg til noe raskt med laks på onsdag, og bytt fredag til pizza»";
    textarea.value = chat.draft;
    textarea.disabled = chat.sending;

    const formActions = makeNode(doc, "div", "", "week-chat-form-actions");
    const sendBtn = makeNode(
      doc,
      "button",
      chat.sending ? "Planlegger …" : "Send",
      "primary",
    );
    sendBtn.type = "submit";
    sendBtn.disabled = chat.sending;
    formActions.append(sendBtn);
    form.append(inputLabel, textarea, formActions);

    QUICK_PROMPTS.forEach((promptText) => {
      const chip = makeNode(doc, "button", promptText, "quiet small week-chat-chip");
      chip.type = "button";
      chip.disabled = chat.sending;
      chip.addEventListener("click", () => {
        chat.draft = promptText;
        textarea.value = promptText;
        chat.status = "Forslag lagt inn i tekstfeltet. Trykk «Send» når du er klar.";
        statusNode.textContent = chat.status;
        textarea.focus?.();
      });
      chips.append(chip);
    });

    function renderTurns() {
      log.replaceChildren();
      if (!chat.turns.length) {
        log.append(
          makeNode(
            doc,
            "p",
            "Ingen meldinger ennå. Skriv hva du ønsker for uken, eller velg et hurtigforslag over.",
            "muted week-chat-empty",
          ),
        );
        return;
      }
      chat.turns.forEach((turn, index) => {
        const bubble = makeNode(
          doc,
          "article",
          "",
          `week-chat-turn week-chat-turn-${turn.role}`,
        );
        const roleHeading = makeNode(
          doc,
          "span",
          turn.role === "user" ? "Deg" : "Ukeplan-assistent",
          "week-chat-role",
        );
        const content = makeNode(doc, "p", turn.content, "week-chat-content");
        bubble.append(roleHeading, content);

        if (turn.role === "assistant" && Array.isArray(turn.notes) && turn.notes.length) {
          const notesList = makeNode(doc, "ul", "", "week-chat-notes");
          turn.notes.forEach((note) => notesList.append(makeNode(doc, "li", String(note), "")));
          bubble.append(notesList);
        }

        if (turn.role === "assistant" && Array.isArray(turn.changes) && turn.changes.length) {
          const changesList = makeNode(doc, "ul", "", "week-chat-changes");
          turn.changes.forEach((change) => {
            const item = makeNode(doc, "li", "", `week-chat-change week-chat-change-${change.action}`);
            const badge = makeNode(doc, "span", changeActionLabel(change), "week-chat-change-badge");
            const summary = formatChangeSummary(change, {
              formatDate: formatDraftDate,
              formatTime: formatUiTime,
            });
            const textWrap = makeNode(doc, "div", "", "week-chat-change-copy");
            textWrap.append(makeNode(doc, "strong", summary.title, ""));
            if (summary.meta) textWrap.append(makeNode(doc, "span", summary.meta, "muted"));
            item.append(badge, textWrap);
            changesList.append(item);
          });
          bubble.append(changesList);

          const hasPendingProposals =
            !turn.changesApplied && turn.changes.some((change) => change.status === "proposed");
          if (hasPendingProposals) {
            const applyTurnBtn = makeNode(
              doc,
              "button",
              chat.applyingTurnIndex === index
                ? "Lagrer endringer …"
                : "Bruk foreslåtte endringer",
              "primary small",
            );
            applyTurnBtn.type = "button";
            applyTurnBtn.disabled = chat.sending || chat.applyingTurnIndex !== null;
            applyTurnBtn.addEventListener("click", () => applyTurnChanges(index));
            bubble.append(applyTurnBtn);
          }
        }
        log.append(bubble);
      });
    }

    function renderFeedback() {
      statusNode.textContent = chat.status;
      errorNode.replaceChildren();
      if (chat.error) {
        errorNode.append(makeNode(doc, "p", chat.error, "form-error"));
        if (chat.errorIsConfig && typeof connectionSettingsButton === "function") {
          errorNode.append(connectionSettingsButton());
        }
      }
      resetBtn.disabled = chat.sending || chat.applyingTurnIndex !== null || !chat.turns.length;
      applyCheckbox.disabled = chat.sending;
      textarea.disabled = chat.sending;
      sendBtn.disabled = chat.sending;
      sendBtn.textContent = chat.sending ? "Planlegger …" : "Send";
    }

    async function applyTurnChanges(turnIndex) {
      const turn = chat.turns[turnIndex];
      if (!turn || chat.sending || chat.applyingTurnIndex !== null) return;
      chat.applyingTurnIndex = turnIndex;
      chat.error = "";
      chat.errorIsConfig = false;
      chat.status = "Lagrer foreslåtte endringer i ukeplanen …";
      renderTurns();
      renderFeedback();
      try {
        const res = await api("/ai/week-chat/apply", "POST", {
          start: weekStartIso,
          days,
          changes: turn.changes,
          reviewed: true,
        });
        turn.changes = Array.isArray(res?.applied_changes) ? res.applied_changes : turn.changes;
        turn.changesApplied = true;
        chat.status = "De foreslåtte endringene er lagt inn i ukeplanen.";
        if (typeof refreshView === "function") await refreshView();
      } catch (err) {
        chat.error = err?.message || "Kunne ikke lagre endringene.";
        chat.status = "";
      } finally {
        chat.applyingTurnIndex = null;
        renderTurns();
        renderFeedback();
      }
    }

    async function submitChat() {
      if (chat.sending) return;
      let payload;
      try {
        payload = buildChatPayload({
          weekStartIso,
          days,
          message: textarea.value,
          turns: chat.turns,
          applyChanges: chat.applyChanges,
        });
      } catch (err) {
        chat.error = err.message;
        chat.errorIsConfig = false;
        renderFeedback();
        return;
      }

      chat.sending = true;
      chat.error = "";
      chat.errorIsConfig = false;
      chat.status = "Planleggeren tenker …";
      renderTurns();
      renderFeedback();

      try {
        const res = await api("/ai/week-chat", "POST", payload);
        chat.turns.push({ role: "user", content: payload.message });
        const changes = Array.isArray(res?.changes) ? res.changes : [];
        const applied = Boolean(res?.applied || changes.some((c) => c.status === "applied"));
        chat.turns.push({
          role: "assistant",
          content: String(res?.reply || "Ukeplanen er oppdatert."),
          notes: Array.isArray(res?.notes) ? res.notes : [],
          changes,
          applied,
          changesApplied: applied,
        });
        chat.draft = "";
        textarea.value = "";
        chat.status = applied
          ? "Ukeplanen er oppdatert."
          : changes.length
            ? "Foreslåtte endringer er klare til gjennomgang."
            : "Svar mottatt.";
        if (applied && typeof refreshView === "function") {
          await refreshView();
        }
      } catch (err) {
        chat.draft = payload.message;
        chat.error = err?.message || "Ukeplan-assistenten kunne ikke svare akkurat nå.";
        chat.errorIsConfig = err?.status === 503 || /konfigurer|oppsett|setup/i.test(chat.error);
        chat.status = "";
      } finally {
        chat.sending = false;
        renderTurns();
        renderFeedback();
      }
    }

    applyCheckbox.addEventListener("change", () => {
      chat.applyChanges = Boolean(applyCheckbox.checked);
    });

    toggleBtn.addEventListener("click", () => {
      chat.expanded = !chat.expanded;
      body.hidden = !chat.expanded;
      toggleBtn.textContent = chat.expanded ? "Skjul KI-chat" : "Vis KI-chat";
      toggleBtn.setAttribute("aria-expanded", chat.expanded ? "true" : "false");
    });

    resetBtn.addEventListener("click", () => {
      if (chat.sending || chat.applyingTurnIndex !== null) return;
      chat.turns = [];
      chat.error = "";
      chat.errorIsConfig = false;
      chat.status = "Startet en ny samtale.";
      renderTurns();
      renderFeedback();
    });

    textarea.addEventListener("input", () => {
      chat.draft = textarea.value;
    });

    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        return submitChat();
      }
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      return submitChat();
    });

    renderTurns();
    renderFeedback();
    body.append(controlsRow, chips, log, statusNode, errorNode, form);
    panel.append(body);
    return panel;
  }

  function focusOrOpen(state) {
    if (state) {
      const chat = ensureState(state);
      chat.expanded = true;
    }
    if (typeof document !== "undefined") {
      const body = document.querySelector(".week-chat-body");
      if (body) body.hidden = false;
      const toggle = document.querySelector(".week-chat-header-actions button[aria-expanded]");
      if (toggle) {
        toggle.textContent = "Skjul KI-chat";
        toggle.setAttribute("aria-expanded", "true");
      }
      const input = document.getElementById("week-chat-input");
      input?.scrollIntoView?.({ behavior: "smooth", block: "center" });
      input?.focus?.();
    }
  }

  root.KitchenWeekPlannerChat = {
    QUICK_PROMPTS,
    ensureState,
    buildChatPayload,
    changeActionLabel,
    formatChangeSummary,
    renderSection,
    focusOrOpen,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenWeekPlannerChat;
  }
})(globalThis);
