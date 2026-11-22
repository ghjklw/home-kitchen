"use strict";
(function (root) {
  const MAX_PHOTO_BYTES = 8 * 1024 * 1024;
  const MAX_PHOTO_PIXELS = 20_000_000;
  const MIME_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);

  function canvasToBlob(canvas, type, quality) {
    return new Promise((resolve) => {
      if (typeof canvas?.toBlob === "function") {
        canvas.toBlob((blob) => resolve(blob), type, quality);
        return;
      }
      resolve(null);
    });
  }

  async function preparePhotoFile(file, deps = {}) {
    if (!file || !(file instanceof Blob)) throw new Error("Velg et bilde av en oppskriftsside.");
    const createBitmap = deps.createImageBitmap || globalThis.createImageBitmap;
    const doc = deps.document || globalThis.document;
    if (typeof createBitmap !== "function" || typeof doc?.createElement !== "function") {
      return file;
    }

    const needsFormatOrSizeFix = !MIME_TYPES.has(file.type) || file.size > MAX_PHOTO_BYTES;
    let bitmap = null;
    try {
      bitmap = await createBitmap(file);
      const srcWidth = Number(bitmap?.width || 0);
      const srcHeight = Number(bitmap?.height || 0);
      if (!srcWidth || !srcHeight) return file;
      const exceedsPixels = srcWidth * srcHeight > MAX_PHOTO_PIXELS;
      if (!needsFormatOrSizeFix && !exceedsPixels) return file;

      const maxEdge = 3200;
      const pixelRatio = Math.sqrt(Math.min(1, (MAX_PHOTO_PIXELS * 0.8) / (srcWidth * srcHeight)));
      const edgeRatio = Math.min(1, maxEdge / Math.max(srcWidth, srcHeight));
      let scale = Math.min(pixelRatio, edgeRatio);

      for (let attempt = 0; attempt < 4; attempt += 1) {
        const width = Math.max(1, Math.round(srcWidth * scale));
        const height = Math.max(1, Math.round(srcHeight * scale));
        const canvas = doc.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext?.("2d");
        if (!ctx) return file;
        ctx.drawImage(bitmap, 0, 0, width, height);
        for (const quality of [0.88, 0.76, 0.64]) {
          const blob = await canvasToBlob(canvas, "image/jpeg", quality);
          if (blob && blob.size > 0 && blob.size <= MAX_PHOTO_BYTES) {
            return blob;
          }
        }
        scale *= 0.75;
      }
    } catch {
      return file;
    } finally {
      if (typeof bitmap?.close === "function") bitmap.close();
    }
    return file;
  }

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

  async function applyIfActive(promise, isActive, apply) {
    const result = await promise;
    if (!isActive()) return false;
    apply(result);
    return true;
  }

  async function applyDiscardIfActive(promise, isActive, actions) {
    const result = await promise;
    if (!isActive()) return false;
    if (result?.deleted === true) actions.deleted();
    else if (result?.deleted === false) actions.retained();
    else actions.unconfirmed();
    return true;
  }

  root.KitchenRecipePhoto = {
    applyDiscardIfActive,
    applyIfActive,
    buildUpload,
    preparePhotoFile,
    MAX_PHOTO_BYTES,
    MAX_PHOTO_PIXELS,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.KitchenRecipePhoto;
})(globalThis);
