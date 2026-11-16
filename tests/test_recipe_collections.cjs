"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { canSelect, entryStatus, importPayload, normalizeJob, progress } = require("../src/home_kitchen/static/recipe-collections.js");

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
