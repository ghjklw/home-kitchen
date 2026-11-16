"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeSession, safeUrl, statusLabel } = require("../src/home_kitchen/static/recipe-images.js");

const candidate = {
  provider: "wikimedia_commons",
  provider_id: "123",
  query: "suppe",
  title: "Suppe",
  image_url: "https://upload.wikimedia.org/wikipedia/commons/a/a1/soup.jpg",
  thumbnail_url: "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/soup.jpg/640px-soup.jpg",
  source_page_url: "https://commons.wikimedia.org/wiki/File:Soup.jpg",
  artist: "Example photographer",
  credit: null,
  license_name: "CC BY-SA 4.0",
  license_url: "https://creativecommons.org/licenses/by-sa/4.0/",
  attribution_required: true,
  attribution: "Example photographer / Wikimedia Commons",
  obligations: ["Credit the creator and link the license."],
};

test("keeps only license-reviewed Commons candidates with safe HTTPS media and source links", () => {
  const session = normalizeSession({
    search_id: "search-1",
    search: { status: "ok", results: [candidate, { ...candidate, provider_id: "unsafe", image_url: "https://images.example/soup.jpg" }] },
  });
  assert.equal(session.results.length, 1);
  assert.equal(session.results[0].license_name, "CC BY-SA 4.0");
  assert.equal(safeUrl("javascript:alert(1)", new Set(["commons.wikimedia.org"])), null);
});

test("retains provider continuation status and speaks license limitations clearly", () => {
  const session = normalizeSession({
    search_id: "search-2",
    search: { status: "rate_limited", detail: "ignored provider detail", results: [], continuation: "next" },
  });
  assert.equal(session.continuation, "next");
  assert.match(statusLabel(session.status), /midlertidig begrenset/);
  assert.throws(() => normalizeSession({ search_id: "bad", search: { status: "unknown", results: [] } }), /ugyldig søkestatus/);
});
