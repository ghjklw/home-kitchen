"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  applyDiscardIfActive,
  applyIfActive,
  buildUpload,
  MAX_PHOTO_BYTES,
} = require("../src/home_kitchen/static/recipe-photo.js");

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

test("explicit photo discard closes the draft only after confirmed deletion", async () => {
  let closed = false;
  let retained = false;
  let unconfirmed = false;
  const applied = await applyDiscardIfActive(
    Promise.resolve({ deleted: true }),
    () => true,
    {
      deleted: () => { closed = true; },
      retained: () => { retained = true; },
      unconfirmed: () => { unconfirmed = true; },
    },
  );
  assert.equal(applied, true);
  assert.equal(closed, true);
  assert.equal(retained, false);
  assert.equal(unconfirmed, false);
});

test("protected photo stays attached and leaves the editable draft unchanged", async () => {
  const draft = {
    title: "Suppe",
    ingredients: [{ name: "gulrot", quantity: "2", unit: "stk" }],
    photo_source: { id: "photo-id", book_title: "God mat", page: "17" },
  };
  const before = structuredClone(draft);
  let closed = false;
  let status = "";
  await applyDiscardIfActive(
    Promise.resolve({ deleted: false }),
    () => true,
    {
      deleted: () => { closed = true; },
      retained: () => { status = "protected"; },
      unconfirmed: () => { status = "unknown"; },
    },
  );
  assert.equal(closed, false);
  assert.equal(status, "protected");
  assert.deepEqual(draft, before);
});

test("missing or malformed photo discard response remains unconfirmed", async () => {
  let status = "";
  await applyDiscardIfActive(
    Promise.resolve({}),
    () => true,
    {
      deleted: () => { status = "deleted"; },
      retained: () => { status = "retained"; },
      unconfirmed: () => { status = "unconfirmed"; },
    },
  );
  assert.equal(status, "unconfirmed");
});

test("late photo discard response cannot close a different or closed dialog", async () => {
  let active = true;
  let closed = false;
  let resolveResponse;
  const pending = new Promise((resolve) => { resolveResponse = resolve; });
  const completion = applyDiscardIfActive(pending, () => active, {
    deleted: () => { closed = true; },
    retained: () => {},
    unconfirmed: () => {},
  });
  active = false;
  resolveResponse({ deleted: true });
  assert.equal(await completion, false);
  assert.equal(closed, false);
});

test("failed photo discard leaves the editable draft available", async () => {
  const draft = { title: "Suppe", instructions: ["Kok"], photo_source: { id: "photo-id" } };
  const before = structuredClone(draft);
  let changed = false;
  await assert.rejects(
    applyDiscardIfActive(
      Promise.reject(new Error("not found")),
      () => true,
      {
        deleted: () => { changed = true; },
        retained: () => { changed = true; },
        unconfirmed: () => { changed = true; },
      },
    ),
    /not found/,
  );
  assert.equal(changed, false);
  assert.deepEqual(draft, before);
});
