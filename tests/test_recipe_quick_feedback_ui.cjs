"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  formatRatingNumber,
  formatRecipeStatsBadges,
  renderQuickFeedbackBar,
} = require("../src/home_kitchen/static/recipe-feedback-quick.js");

class DomElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.className = "";
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.hidden = false;
    this.type = "";
    this._textContent = "";
  }

  set textContent(value) {
    this._textContent = String(value);
    this.children = [];
  }

  get textContent() {
    return this._textContent + this.children.map((child) => child.textContent).join("");
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name];
  }

  append(...nodes) {
    this.children.push(...nodes);
  }

  addEventListener(eventName, handler) {
    this.listeners[eventName] = handler;
  }

  async click() {
    if (this.disabled) return;
    if (typeof this.listeners.click === "function") {
      await this.listeners.click();
    }
  }
}

function createFakeDocument() {
  return {
    createElement(tagName) {
      return new DomElement(tagName);
    },
  };
}

test("formatRatingNumber formats integers and Norwegian decimal ratings", () => {
  assert.equal(formatRatingNumber(4), "4");
  assert.equal(formatRatingNumber(4.5), "4,5");
  assert.equal(formatRatingNumber(4.36), "4,4");
  assert.equal(formatRatingNumber("not-a-number"), "");
});

test("formatRecipeStatsBadges builds localized Option B activity and feedback badges", () => {
  assert.deepEqual(formatRecipeStatsBadges(null), []);
  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 0,
      times_cooked: 0,
      times_ordered: 0,
      latest_rating: null,
      average_rating: null,
      would_make_again: null,
      feedback_count: 0,
    }),
    [],
  );

  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 1,
      times_cooked: 2,
      times_ordered: 3,
      average_rating: 4.5,
      latest_rating: 5,
      would_make_again: true,
      feedback_count: 4,
    }),
    [
      "Planlagt 1 gang",
      "Tilberedt 2 ganger",
      "Bestilt 3 ganger",
      "4,5 ★ (4)",
      "👍 Anbefalt",
    ],
  );

  assert.deepEqual(
    formatRecipeStatsBadges({
      times_planned: 2,
      times_cooked: 1,
      times_ordered: 1,
      average_rating: null,
      latest_rating: 3,
      would_make_again: false,
      feedback_count: 1,
    }),
    [
      "Planlagt 2 ganger",
      "Tilberedt 1 gang",
      "Bestilt 1 gang",
      "3 ★ (1)",
      "👎 Ikke anbefalt",
    ],
  );
});

test("renderQuickFeedbackBar validates required recipe ID and document", () => {
  const doc = createFakeDocument();
  assert.throws(() => renderQuickFeedbackBar({}, null, { document: doc }), /Oppskrift med ID/);
  assert.throws(() => renderQuickFeedbackBar({ id: "r1" }, null, {}), /DOM document/);
});

test("1-click star and thumbs buttons submit Option B feedback and update aria-pressed", async () => {
  const doc = createFakeDocument();
  const apiCalls = [];
  const submitted = [];

  const bar = renderQuickFeedbackBar(
    {
      id: "recipe-1",
      activity_stats: { latest_rating: 3, would_make_again: null },
    },
    (result, payload) => submitted.push({ result, payload }),
    {
      document: doc,
      api: async (endpoint, method, payload) => {
        apiCalls.push({ endpoint, method, payload });
        return { id: `entry-${apiCalls.length}`, structured: payload };
      },
    },
  );

  assert.equal(bar.starButtons.length, 5);
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "true");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");

  // 1-click 5-star rating
  await bar.starButtons[4].click();
  assert.deepEqual(apiCalls[0], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { rating: 5 },
  });
  assert.equal(bar.starButtons[2].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[4].getAttribute("aria-pressed"), "true");
  assert.equal(bar.statusElement.textContent, "Vurdering lagret: 5 av 5 stjerner");
  assert.equal(bar.errorElement.hidden, true);

  // 1-click thumbs up
  await bar.thumbUpButton.click();
  assert.deepEqual(apiCalls[1], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { would_make_again: true },
  });
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.statusElement.textContent, "Anbefaling lagret: Tommel opp");

  // 1-click thumbs down
  await bar.thumbDownButton.click();
  assert.deepEqual(apiCalls[2], {
    endpoint: "/recipes/recipe-1/feedback",
    method: "POST",
    payload: { would_make_again: false },
  });
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.statusElement.textContent, "Anbefaling lagret: Tommel ned");
  assert.equal(submitted.length, 3);
});

test("quick feedback bar locks controls while busy and recovers cleanly after failed save", async () => {
  const doc = createFakeDocument();
  let resolvePending;
  let shouldFail = false;
  const fetchCalls = [];

  const bar = renderQuickFeedbackBar(
    {
      id: "recipe 2",
      stats: { latest_rating: 2, would_make_again: true },
    },
    null,
    {
      document: doc,
      fetch: async (url, init) => {
        fetchCalls.push({ url, init });
        if (shouldFail) {
          return {
            ok: false,
            status: 500,
            text: async () => "server error",
          };
        }
        return new Promise((resolve) => {
          resolvePending = () =>
            resolve({
              ok: true,
              json: async () => ({ saved: true }),
            });
        });
      },
    },
  );

  // Start async click and verify buttons are disabled while in-flight
  const firstClick = bar.starButtons[3].click();
  assert.equal(bar.starButtons.every((btn) => btn.disabled), true);
  assert.equal(bar.thumbUpButton.disabled, true);
  assert.equal(bar.thumbDownButton.disabled, true);

  // Second click while busy should be ignored
  await bar.thumbDownButton.click();
  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "/api/recipes/recipe%202/feedback");

  resolvePending();
  await firstClick;
  assert.equal(bar.starButtons.every((btn) => !btn.disabled), true);
  assert.equal(bar.starButtons[3].getAttribute("aria-pressed"), "true");

  // Failed thumb click preserves prior state and shows alert
  shouldFail = true;
  await bar.thumbDownButton.click();
  assert.equal(bar.errorElement.hidden, false);
  assert.equal(bar.errorElement.textContent, "Kunne ikke lagre anbefaling. Prøv igjen.");
  assert.equal(bar.thumbUpButton.getAttribute("aria-pressed"), "true");
  assert.equal(bar.thumbDownButton.getAttribute("aria-pressed"), "false");
  assert.equal(bar.thumbDownButton.disabled, false);

  // Failed star click shows rating error
  await bar.starButtons[0].click();
  assert.equal(bar.errorElement.hidden, false);
  assert.equal(bar.errorElement.textContent, "Kunne ikke lagre vurdering. Prøv igjen.");
  assert.equal(bar.starButtons[0].getAttribute("aria-pressed"), "false");
  assert.equal(bar.starButtons[3].getAttribute("aria-pressed"), "true");
});
