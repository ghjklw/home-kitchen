const assert = require("node:assert/strict");
const test = require("node:test");

const { buildRequest, normalizeResponse } = require("../src/home_kitchen/static/recipe-discovery.js");

test("builds a trimmed, bounded source search request", () => {
  assert.deepEqual(buildRequest("  tomato soup  ", ["oda", "marmiton", "oda"]), {
    query: "tomato soup",
    sources: ["oda", "marmiton"],
    limit: 10,
  });
  assert.throws(() => buildRequest("  ", ["oda"]), /Skriv inn ingredienser/);
  assert.throws(() => buildRequest("soup", []), /Velg minst én/);
});

test("keeps source failure states while accepting safe, source-backed results", () => {
  const view = normalizeResponse({
    query: "soup",
    sources: [
      { source: "oda", status: "connection_required", detail: "Sign in first", result_count: 0 },
      { source: "marmiton", status: "ok", result_count: 1, index_url: "https://www.marmiton.org/recettes/" },
    ],
    results: [
      { source: "marmiton", title: "Tomato soup", url: "https://www.marmiton.org/recettes/recette_soupe_123.aspx", image_url: "https://assets.afcdn.com/soup.jpg", importable: true },
      { source: "oda", title: "Unsafe", url: "http://example.test/recipe", importable: true },
      { source: "unknown", title: "Unknown source", url: "https://example.test/recipe", importable: true },
    ],
  });

  assert.equal(view.sources[0].status, "Koble til kilden for å søke");
  assert.equal(view.sources[0].detail, "Sign in first");
  assert.equal(view.sources[1].indexUrl, "https://www.marmiton.org/recettes/");
  assert.deepEqual(view.results, [{
    source: "marmiton",
    sourceLabel: "Marmiton",
    title: "Tomato soup",
    url: "https://www.marmiton.org/recettes/recette_soupe_123.aspx",
    imageUrl: "https://assets.afcdn.com/soup.jpg",
    sourceAttribution: "",
    duration: "",
    difficulty: "",
    importable: true,
    detail: "",
  }, {
    source: "oda",
    sourceLabel: "Oda",
    title: "Unsafe",
    url: null,
    imageUrl: null,
    sourceAttribution: "",
    duration: "",
    difficulty: "",
    importable: false,
    detail: "",
  }]);
});
