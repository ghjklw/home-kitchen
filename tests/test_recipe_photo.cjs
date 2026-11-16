"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { applyIfActive, buildUpload, MAX_PHOTO_BYTES } = require("../src/home_kitchen/static/recipe-photo.js");

test("photo upload encodes image bytes and retains optional book and page evidence", async () => {
  const file = new Blob([Uint8Array.from([0, 1, 2, 253, 254, 255])], { type: "image/jpeg" });
  const payload = await buildUpload(file, { book_title: "God mat", page: "17" });
  assert.deepEqual(payload, {
    mime_type: "image/jpeg",
    data_base64: "AAEC/f7/",
    book_title: "God mat",
    page: "17",
  });
});

test("photo upload rejects unsupported MIME and files over the service limit", async () => {
  await assert.rejects(buildUpload(new Blob(["x"], { type: "image/gif" })), /JPEG-, PNG- eller WebP/);
  await assert.rejects(
    buildUpload(new Blob([new Uint8Array(MAX_PHOTO_BYTES + 1)], { type: "image/png" })),
    /8 MiB/,
  );
});

test("late photo extraction cannot replace the screen after its dialog closes", async () => {
  let active = true;
  let applied = false;
  let resolveResponse;
  const pending = new Promise((resolve) => { resolveResponse = resolve; });
  const completion = applyIfActive(pending, () => active, () => { applied = true; });
  active = false;
  resolveResponse({ status: "draft_ready", draft: { title: "Soup" } });
  assert.equal(await completion, false);
  assert.equal(applied, false);
});

test("current photo extraction applies its editable draft to the open dialog", async () => {
  let applied = null;
  const result = await applyIfActive(
    Promise.resolve({ status: "draft_ready", draft: { title: "Soup" } }),
    () => true,
    (value) => { applied = value; },
  );
  assert.equal(result, true);
  assert.equal(applied.draft.title, "Soup");
});
