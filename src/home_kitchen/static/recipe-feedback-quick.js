(function (root) {
  "use strict";

  function formatRatingNumber(val) {
    const num = Number(val);
    if (Number.isNaN(num)) return "";
    const rounded = Math.round(num * 10) / 10;
    return String(rounded).replace(".", ",");
  }

  function formatRecipeStatsBadges(stats) {
    if (!stats || typeof stats !== "object") return [];
    const badges = [];

    // 1. times_planned
    const planned = Number(stats.times_planned);
    if (Number.isInteger(planned) && planned > 0) {
      badges.push(planned === 1 ? "Planlagt 1 gang" : `Planlagt ${planned} ganger`);
    }

    // 2. times_cooked
    const cooked = Number(stats.times_cooked);
    if (Number.isInteger(cooked) && cooked > 0) {
      badges.push(cooked === 1 ? "Tilberedt 1 gang" : `Tilberedt ${cooked} ganger`);
    }

    // 3. times_ordered
    const ordered = Number(stats.times_ordered);
    if (Number.isInteger(ordered) && ordered > 0) {
      badges.push(ordered === 1 ? "Bestilt 1 gang" : `Bestilt ${ordered} ganger`);
    }

    // 4. Rating (average or latest)
    const count = Number(stats.feedback_count);
    const countSuffix = Number.isInteger(count) && count > 0 ? ` (${count})` : "";
    if (stats.average_rating != null) {
      const formattedAvg = formatRatingNumber(stats.average_rating);
      if (formattedAvg) {
        badges.push(`${formattedAvg} ★${countSuffix}`);
      }
    } else if (stats.latest_rating != null) {
      const formattedLatest = formatRatingNumber(stats.latest_rating);
      if (formattedLatest) {
        badges.push(`${formattedLatest} ★${countSuffix}`);
      }
    }

    // 5. would_make_again
    if (stats.would_make_again === true) {
      badges.push("👍 Anbefalt");
    } else if (stats.would_make_again === false) {
      badges.push("👎 Ikke anbefalt");
    }

    return badges;
  }

  function renderQuickFeedbackBar(recipe, onSubmit, options = {}) {
    if (!recipe || !recipe.id) {
      throw new Error("Oppskrift med ID kreves for hurtigtilbakemelding.");
    }

    const doc = options.document || (typeof document !== "undefined" ? document : null);
    if (!doc) {
      throw new Error("DOM document er ikke tilgjengelig.");
    }

    const stats = recipe.activity_stats || recipe.stats || {};
    let currentRating = stats.latest_rating ?? null;
    let currentWouldMakeAgain = stats.would_make_again ?? null;
    let busy = false;

    const bar = doc.createElement("div");
    bar.className = "recipe-quick-feedback-bar";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", "Hurtigvurdering for oppskrift");

    // Star ratings container
    const starsGroup = doc.createElement("div");
    starsGroup.className = "quick-feedback-stars";
    starsGroup.setAttribute("role", "group");
    starsGroup.setAttribute("aria-label", "Gi terningkast eller stjernevurdering fra 1 til 5");

    const starButtons = [];
    for (let i = 1; i <= 5; i++) {
      const starBtn = doc.createElement("button");
      starBtn.type = "button";
      starBtn.className = "quick-feedback-star";
      starBtn.dataset.rating = String(i);
      starBtn.setAttribute("aria-label", `${i} ${i === 1 ? "stjerne" : "stjerner"} av 5`);
      starBtn.setAttribute("aria-pressed", currentRating === i ? "true" : "false");
      starBtn.textContent = "★";
      starBtn.addEventListener("click", () => handleRatingClick(i));
      starButtons.push(starBtn);
      starsGroup.append(starBtn);
    }

    // Thumbs container
    const thumbsGroup = doc.createElement("div");
    thumbsGroup.className = "quick-feedback-thumbs";
    thumbsGroup.setAttribute("role", "group");
    thumbsGroup.setAttribute("aria-label", "Anbefal eller fraråd oppskrift");

    const thumbUpBtn = doc.createElement("button");
    thumbUpBtn.type = "button";
    thumbUpBtn.className = "quick-feedback-thumb quick-feedback-thumb-up";
    thumbUpBtn.dataset.feedback = "thumbs_up";
    thumbUpBtn.setAttribute("aria-label", "Tommel opp: Anbefal oppskrift");
    thumbUpBtn.setAttribute("aria-pressed", currentWouldMakeAgain === true ? "true" : "false");
    thumbUpBtn.textContent = "👍";
    thumbUpBtn.addEventListener("click", () => handleThumbClick(true));

    const thumbDownBtn = doc.createElement("button");
    thumbDownBtn.type = "button";
    thumbDownBtn.className = "quick-feedback-thumb quick-feedback-thumb-down";
    thumbDownBtn.dataset.feedback = "thumbs_down";
    thumbDownBtn.setAttribute("aria-label", "Tommel ned: Fraråd oppskrift");
    thumbDownBtn.setAttribute("aria-pressed", currentWouldMakeAgain === false ? "true" : "false");
    thumbDownBtn.textContent = "👎";
    thumbDownBtn.addEventListener("click", () => handleThumbClick(false));

    thumbsGroup.append(thumbUpBtn, thumbDownBtn);

    // Live status message for screen readers
    const status = doc.createElement("span");
    status.className = "quick-feedback-status";
    status.setAttribute("aria-live", "polite");

    // Error alert
    const error = doc.createElement("span");
    error.className = "quick-feedback-error";
    error.setAttribute("role", "alert");
    error.hidden = true;

    bar.append(starsGroup, thumbsGroup, status, error);

    function setDisabled(disabled) {
      busy = disabled;
      for (const btn of starButtons) {
        btn.disabled = disabled;
      }
      thumbUpBtn.disabled = disabled;
      thumbDownBtn.disabled = disabled;
    }

    async function sendApi(payload) {
      if (typeof options.api === "function") {
        return options.api(`/recipes/${encodeURIComponent(recipe.id)}/feedback`, "POST", payload);
      }
      if (typeof window !== "undefined" && typeof window.api === "function") {
        return window.api(`/recipes/${encodeURIComponent(recipe.id)}/feedback`, "POST", payload);
      }
      const fetchFn = options.fetch || (typeof fetch !== "undefined" ? fetch : null);
      if (!fetchFn) {
        throw new Error("Ingen API- eller fetch-klient tilgjengelig.");
      }
      const resp = await fetchFn(`/api/recipes/${encodeURIComponent(recipe.id)}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        throw new Error(`Feil ved lagring (${resp.status}): ${text}`);
      }
      return resp.json();
    }

    async function handleRatingClick(rating) {
      if (busy) return;
      error.hidden = true;
      error.textContent = "";
      setDisabled(true);
      try {
        const payload = { rating };
        const result = await sendApi(payload);
        currentRating = rating;
        for (const btn of starButtons) {
          const btnRating = Number(btn.dataset.rating);
          btn.setAttribute("aria-pressed", btnRating === rating ? "true" : "false");
        }
        status.textContent = `Vurdering lagret: ${rating} av 5 stjerner`;
        if (typeof onSubmit === "function") {
          onSubmit(result, payload);
        }
      } catch (err) {
        error.hidden = false;
        error.textContent = "Kunne ikke lagre vurdering. Prøv igjen.";
      } finally {
        setDisabled(false);
      }
    }

    async function handleThumbClick(wouldMakeAgain) {
      if (busy) return;
      error.hidden = true;
      error.textContent = "";
      setDisabled(true);
      try {
        const payload = { would_make_again: wouldMakeAgain };
        const result = await sendApi(payload);
        currentWouldMakeAgain = wouldMakeAgain;
        thumbUpBtn.setAttribute("aria-pressed", wouldMakeAgain ? "true" : "false");
        thumbDownBtn.setAttribute("aria-pressed", !wouldMakeAgain ? "true" : "false");
        status.textContent = wouldMakeAgain
          ? "Anbefaling lagret: Tommel opp"
          : "Anbefaling lagret: Tommel ned";
        if (typeof onSubmit === "function") {
          onSubmit(result, payload);
        }
      } catch (err) {
        error.hidden = false;
        error.textContent = "Kunne ikke lagre anbefaling. Prøv igjen.";
      } finally {
        setDisabled(false);
      }
    }

    bar.starButtons = starButtons;
    bar.thumbUpButton = thumbUpBtn;
    bar.thumbDownButton = thumbDownBtn;
    bar.statusElement = status;
    bar.errorElement = error;

    return bar;
  }

  root.KitchenRecipeQuickFeedback = {
    formatRatingNumber,
    formatRecipeStatsBadges,
    renderQuickFeedbackBar,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.KitchenRecipeQuickFeedback;
  }
})(typeof window === "undefined" ? globalThis : window);
