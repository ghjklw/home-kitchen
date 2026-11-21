"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticDir = path.join(__dirname, "../src/home_kitchen/static");
const indexHtml = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");
const swSource = fs.readFileSync(path.join(staticDir, "sw.js"), "utf8");
const manifestSource = fs.readFileSync(path.join(staticDir, "manifest.webmanifest"), "utf8");
const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");

function loadServiceWorkerConfig() {
  const listeners = {};
  const context = {
    URL,
    self: {
      location: { origin: "https://kitchen.local" },
      addEventListener(name, fn) {
        listeners[name] = fn;
      },
      skipWaiting() {},
      clients: { claim() {} },
    },
    caches: {
      open: async () => ({ addAll: async () => {}, put: async () => {} }),
      keys: async () => [],
      delete: async () => true,
      match: async () => null,
    },
    fetch: async () => ({ ok: true }),
  };
  vm.runInNewContext(
    `${swSource}\nglobalThis.__CACHE = CACHE; globalThis.__ASSETS = ASSETS;`,
    context,
  );
  return {
    cacheName: context.__CACHE,
    assets: context.__ASSETS,
    listeners,
  };
}

function readPngDimensions(filePath) {
  const buf = fs.readFileSync(filePath);
  const signature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.ok(buf.length >= 24, `${path.basename(filePath)} must be a valid PNG (>=24 bytes)`);
  assert.ok(buf.subarray(0, 8).equals(signature), `${path.basename(filePath)} must have PNG signature`);
  assert.equal(buf.subarray(12, 16).toString("ascii"), "IHDR", `${path.basename(filePath)} must start with IHDR`);
  return {
    width: buf.readUInt32BE(16),
    height: buf.readUInt32BE(20),
  };
}

test("sw.js precaches every /static/*.js script loaded by index.html and static PWA icons", () => {
  const { cacheName, assets, listeners } = loadServiceWorkerConfig();
  assert.equal(cacheName, "kitchen-shell-v23");
  assert.ok(listeners.install && listeners.activate && listeners.fetch);

  const scriptMatches = [...indexHtml.matchAll(/<script\b[^>]*\bsrc="(\/static\/[^"]+\.js)"/gu)].map(
    (match) => match[1],
  );
  assert.ok(scriptMatches.length > 0, "index.html must load static scripts");
  assert.ok(
    scriptMatches.includes("/static/cookidoo-plan-sync.js"),
    "index.html should load /static/cookidoo-plan-sync.js",
  );

  for (const scriptPath of scriptMatches) {
    assert.ok(assets.includes(scriptPath), `sw.js ASSETS is missing script ${scriptPath}`);
  }

  for (const requiredAsset of [
    "/static/index.html",
    "/static/style.css",
    "/static/icon.svg",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/manifest.webmanifest",
  ]) {
    assert.ok(assets.includes(requiredAsset), `sw.js ASSETS is missing ${requiredAsset}`);
  }

  for (const asset of assets) {
    assert.ok(asset.startsWith("/static/"), `unexpected asset path ${asset}`);
    const filePath = path.join(staticDir, asset.slice("/static/".length));
    assert.ok(fs.existsSync(filePath), `precached asset does not exist on disk: ${filePath}`);
  }
});

test("manifest.webmanifest has Bokmål metadata and valid 192x192 & 512x512 PNG icons", () => {
  assert.match(indexHtml, /<html\s+lang="nb">/u);
  const manifest = JSON.parse(manifestSource);
  assert.equal(manifest.short_name, "Kjøkken");
  assert.match(manifest.name, /Kjøkken.*måltider.*handling/u);
  assert.match(manifest.description, /Måltidsplanlegging.*oppskrifter.*handleliste/u);
  assert.equal(manifest.display, "standalone");
  assert.equal(manifest.start_url, "/");
  assert.equal(manifest.scope, "/");

  const icons = Array.isArray(manifest.icons) ? manifest.icons : [];
  for (const expectedSize of [192, 512]) {
    const expectedSrc = `/static/icon-${expectedSize}.png`;
    const entry = icons.find(
      (icon) =>
        icon.src === expectedSrc &&
        icon.sizes === `${expectedSize}x${expectedSize}` &&
        icon.type === "image/png",
    );
    assert.ok(entry, `manifest.webmanifest must include ${expectedSrc} (${expectedSize}x${expectedSize})`);
    const dims = readPngDimensions(path.join(staticDir, `icon-${expectedSize}.png`));
    assert.deepEqual(dims, { width: expectedSize, height: expectedSize });
  }
});

test("offline shell fallback and Bokmål offline/online status copy are present", () => {
  assert.match(swSource, /caches\.match\('\/static\/index\.html'\)/u);
  assert.match(
    appSource,
    /Du er frakoblet\. Koble til igjen for å laste inn eller lagre endringer for husholdningen\./u,
  );
  assert.match(
    appSource,
    /Du er frakoblet\. Koble til igjen før du lagrer endringene\. Det åpne skjemaet blir liggende her\./u,
  );
  assert.match(appSource, /Tilkoblingen er gjenopprettet\./u);
});
