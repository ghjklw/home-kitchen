"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { canSelect, collectionErrorMessage, completionCounts, connectionGuidance, connectionMessage, entryStatus, errorMessages, importPayload, normalizeJob, progress } = require("../src/home_kitchen/static/recipe-collections.js");

const fixture = {
  job_id: "job-1",
  status: "ready",
  kinds: ["liked", "purchased"],
  entries: [
    { id: "10", provider: "oda", title: "Fiskesuppe", badges: ["Liked on Oda"], status: "pending" },
    { id: "11", provider: "oda", title: "Pannekaker", badges: ["Ingredients bought on Oda"], status: "already_imported", recipe_id: "recipe-1" },
    { id: "12", provider: "oda", title: "Tomatsaus", status: "missing_url" },
  ],
  pages_read: 4,
  processed: 0,
  total: 3,
  truncated: false,
  truncated_kinds: [],
  errors: [],
};

test("normalizes a bounded Oda collection review and exposes only importable entries", () => {
  const job = normalizeJob(fixture);
  assert.equal(job.entries.length, 3);
  assert.equal(canSelect(job.entries[0]), true);
  assert.equal(canSelect(job.entries[1]), false);
  assert.equal(canSelect(job.entries[2]), false);
  assert.equal(entryStatus("already_imported"), "Finnes allerede i samlingen");
  assert.equal(progress({ ...job, status: "previewing" }), "Hentet 4 Oda-sider · 3 oppskrifter funnet …");
});

test("builds an explicit reviewed import request from unique selected source IDs", () => {
  assert.deepEqual(importPayload("job/1", ["10"]), {
    path: "/recipe-collections/job%2F1/import",
    body: { selected_ids: ["10"], reviewed: true },
  });
  assert.throws(() => importPayload("job-1", []), /Velg minst én/);
  assert.throws(() => importPayload("job-1", ["10", "10"]), /Velg minst én/);
});

test("rejects malformed jobs instead of showing misleading collection state", () => {
  assert.throws(() => normalizeJob({ ...fixture, status: "unknown" }), /ugyldig importstatus/);
  assert.throws(() => normalizeJob({ ...fixture, entries: [{ id: "1", title: "No status" }] }), /ugyldig oppskriftsrad/);
});

test("reports completed, pre-existing, and failed recipes separately after partial cancellation", () => {
  const job = normalizeJob({
    ...fixture,
    status: "cancelled",
    entries: [
      { id: "1", title: "A", status: "imported" },
      { id: "2", title: "B", status: "already_imported" },
      { id: "3", title: "C", status: "failed" },
      { id: "4", title: "D", status: "pending" },
    ],
  });
  assert.deepEqual(completionCounts(job), { imported: 1, already: 1, failed: 1 });
});

test("shows Settings guidance only for known Oda setup or authorization states", () => {
  const reconnect = (message) => /Oda authorization expired/iu.test(message);
  assert.equal(connectionGuidance({ configured: false, connected: false }, new Error("failed"), reconnect), "setup_required");
  assert.equal(connectionGuidance({ configured: true, connected: false }, new Error("failed"), reconnect), "connect_required");
  assert.equal(connectionGuidance({ configured: true, connected: true }, new Error("Oda authorization expired"), reconnect), "reconnect_required");
  assert.equal(connectionGuidance({ configured: true, connected: true }, Object.assign(new Error("Login required"), { status: 401 }), reconnect), null);
  assert.equal(connectionGuidance({ configured: true, connected: true }, new Error("Fikk ikke kontakt med kjøkkenet"), reconnect), null);
  assert.match(connectionMessage("connect_required"), /Oda-kontoen er ikke tilkoblet/u);
  assert.match(connectionMessage("reconnect_required"), /ny godkjenning/u);
});

test("translates collection error messages and deduplicates rendered errors", () => {
  assert.equal(
    collectionErrorMessage("Could not finish Liked on Oda; preview again to retry."),
    "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Could not finish Ingredients bought on Oda; preview again to retry."),
    "Kunne ikke hente oppskrifter med bestilte ingredienser fra Oda. Prøv å hente på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Collection connection changed or became unavailable; preview again."),
    "Oda-tilkoblingen ble endret eller er utilgjengelig. Hent samlingene på nytt.",
  );
  assert.equal(
    collectionErrorMessage("Your Oda connection expired; connect again"),
    "Oda-tilkoblingen utløp. Koble til på nytt i Innstillinger.",
  );

  const jobWithDualFailures = {
    errors: [
      "Could not finish Liked on Oda; preview again to retry.",
      "Could not finish Ingredients bought on Oda; preview again to retry.",
    ],
  };
  const translated = errorMessages(jobWithDualFailures);
  assert.equal(translated.length, 2);
  assert.equal(translated[0], "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.");
  assert.equal(translated[1], "Kunne ikke hente oppskrifter med bestilte ingredienser fra Oda. Prøv å hente på nytt.");
  assert.notEqual(translated[0], translated[1]);

  const jobWithDuplicates = {
    errors: [
      "Could not finish Liked on Oda; preview again to retry.",
      "Could not finish Liked on Oda; preview again to retry.",
      "Collection connection changed or became unavailable; preview again.",
    ],
  };
  const deduplicated = errorMessages(jobWithDuplicates);
  assert.equal(deduplicated.length, 2);
  assert.deepEqual(deduplicated, [
    "Kunne ikke hente likte oppskrifter fra Oda. Prøv å hente på nytt.",
    "Oda-tilkoblingen ble endret eller er utilgjengelig. Hent samlingene på nytt.",
  ]);
});
