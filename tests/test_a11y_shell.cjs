"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("src/home_kitchen/static/index.html", "utf8");
const css = fs.readFileSync("src/home_kitchen/static/style.css", "utf8");

test("keyboard users can skip the repeated navigation to main content", () => {
  assert.match(html, /<a class="skip-link" href="#main">Hopp til hovedinnhold<\/a>/);
  assert.match(html, /<main id="main" tabindex="-1">/);
  assert.match(css, /\.skip-link\s*\{[^}]*transform:\s*translateY\(calc\(-100% - 16px\)\)/s);
  assert.match(css, /\.skip-link:focus\s*\{[^}]*transform:\s*translateY\(0\)/s);
});
