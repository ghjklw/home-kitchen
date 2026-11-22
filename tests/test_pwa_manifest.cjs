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
const styleSource = fs.readFileSync(path.join(staticDir, "style.css"), "utf8");

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
  assert.equal(cacheName, "kitchen-shell-v24");
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
    for (const purpose of ["any", "maskable"]) {
      const entry = icons.find(
        (icon) =>
          icon.src === expectedSrc &&
          icon.sizes === `${expectedSize}x${expectedSize}` &&
          icon.type === "image/png" &&
          icon.purpose === purpose,
      );
      assert.ok(
        entry,
        `manifest.webmanifest must include ${expectedSrc} (${expectedSize}x${expectedSize}) with purpose="${purpose}"`,
      );
    }
    const dims = readPngDimensions(path.join(staticDir, `icon-${expectedSize}.png`));
    assert.deepEqual(dims, { width: expectedSize, height: expectedSize });
  }
  assert.match(indexHtml, /<meta\s+name="viewport"[^>]*viewport-fit=cover/u);
});

test("offline shell fallback and Bokmål offline/online status copy are present", () => {
  assert.match(swSource, /caches\.match\('\/static\/index\.html'\)/u);
  assert.match(
    appSource,
    /Du er frakoblet\. Koble til igjen for å laste inn eller lagre endringer for husholdningen\./u,
  );
  assert.match(
    appSource,
    /Bufret app-skall betyr ikke at oppskrifter, ukeplan eller felles handleliste er tilgjengelige uten nett\./u,
  );
  assert.match(
    appSource,
    /Du er frakoblet\. Koble til igjen før du lagrer endringene\. Det åpne skjemaet blir liggende her\./u,
  );
  assert.match(appSource, /Tilkoblingen er gjenopprettet\./u);
});

test("Android phone and tablet responsive layout and touch targets are configured in style.css", () => {
  assert.match(styleSource, /grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\)/u);
  assert.match(styleSource, /env\(safe-area-inset-bottom\)/u);
  assert.match(styleSource, /\.list input\[type="checkbox"\]::after/u);
  assert.match(styleSource, /@media\s*\(pointer:\s*coarse\)/u);
  assert.match(styleSource, /@media\s*\(max-width:\s*820px\)/u);
  assert.match(styleSource, /overscroll-behavior:\s*contain/u);
  assert.match(styleSource, /\.sidebar\s*\{[^}]*overflow-y:\s*auto/su);
});

test("offline navigation renders accurate unavailable state instead of stale/empty list", () => {
  const fnMatch = appSource.match(
    /const OFFLINE_SHELL_CALLOUT =[\s\S]*?\nfunction renderUnavailableView\(error\) \{[\s\S]*?\n\}/u,
  );
  assert.ok(fnMatch, "offline helpers and renderUnavailableView exist");

  function makeNode(tag, text = "", className = "") {
    return {
      tagName: tag,
      textContent: text,
      className,
      children: [],
      attributes: {},
      dataset: {},
      setAttribute(k, v) { this.attributes[k] = v; },
      removeAttribute(k) { delete this.attributes[k]; },
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; },
      querySelector(sel) {
        if (sel === ".callout.offline") {
          return this.children.find((c) => c.className?.includes("callout offline")) || null;
        }
        return null;
      },
      prepend(node) { this.children.unshift(node); },
    };
  }

  const navButtons = [
    Object.assign(makeNode("button"), { dataset: { view: "week" } }),
    Object.assign(makeNode("button"), { dataset: { view: "shopping" } }),
  ];
  const main = makeNode("main");
  const context = {
    state: { view: "shopping", shoppingPanel: "single", initialLoaded: true, loadedViewRanges: {} },
    navigator: { onLine: false },
    document: { querySelectorAll: () => navButtons },
    $: (sel) => (sel === "#main" ? main : null),
    el: (tag, text, className) => makeNode(tag, text, className),
    add: (parent, ...children) => {
      children.filter(Boolean).forEach((child) => parent.append(child));
      return parent;
    },
    button: (text, action, className) => Object.assign(makeNode("button", text, className), { action }),
    empty: (heading, desc, action) => makeNode("div", `${heading} ${desc}`, "empty"),
    range: () => "?start=2026-11-16&end=2026-11-22",
    refreshView: () => {},
    start: () => {},
  };
  vm.runInNewContext(
    fnMatch[0] + "\nglobalThis.renderUnavailableView = renderUnavailableView; globalThis.viewLoadedForCurrentState = viewLoadedForCurrentState;",
    context,
  );
  assert.equal(context.viewLoadedForCurrentState("shopping"), false);
  context.renderUnavailableView(new Error("offline"));
  assert.equal(navButtons[0].attributes["aria-current"], undefined);
  assert.equal(navButtons[1].attributes["aria-current"], "page");
  const combinedText = main.children.map((c) => c.textContent).join(" ");
  assert.match(combinedText, /App-skallet er lastet fra lokal hurtigbuffer/u);
  assert.match(combinedText, /oppskrifter, ukeplan og felles handleliste lagres ikke uten nett/u);
});

test("app.js wires beforeinstallprompt and install button for Android/tablet PWA install", () => {
  assert.match(appSource, /window\.addEventListener\("beforeinstallprompt"/u);
  assert.match(appSource, /window\.addEventListener\("appinstalled"/u);
  assert.match(appSource, /Installer Kjøkken-appen/u);
});
