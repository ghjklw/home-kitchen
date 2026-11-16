"use strict";
(function (root) {
  const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
  const MIME_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

  function bytesToBase64(bytes) {
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
  }

  async function buildUpload(file, attribution = {}) {
    if (!file || !(file instanceof Blob)) throw new Error("Velg et bilde av en oppskriftsside.");
    if (!MIME_TYPES.has(file.type)) throw new Error("Bruk et JPEG-, PNG- eller WebP-bilde.");
    if (file.size < 1 || file.size > MAX_PHOTO_BYTES)
      throw new Error("Bildet må være mindre enn 8 MiB.");
    return {
      mime_type: file.type,
      data_base64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
      book_title: String(attribution.book_title || "").trim() || null,
      page: String(attribution.page || "").trim() || null,
    };
  }

  root.KitchenRecipePhoto = { buildUpload, MAX_PHOTO_BYTES };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipePhoto;
})(globalThis);
