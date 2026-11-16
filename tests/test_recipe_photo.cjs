"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { buildUpload, MAX_PHOTO_BYTES } = require("../src/home_kitchen/static/recipe-photo.js");

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
