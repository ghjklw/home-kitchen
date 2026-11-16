"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("src/home_kitchen/static/index.html", "utf8");
const css = fs.readFileSync("src/home_kitchen/static/style.css", "utf8");

function luminance(hex) {
  const channels = hex.match(/[0-9a-f]{2}/gi).map((value) => parseInt(value, 16) / 255);
  const linear = channels.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrast(first, second) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

test("keyboard users can skip the repeated navigation to main content", () => {
  assert.match(html, /<a class="skip-link" href="#main">Hopp til hovedinnhold<\/a>/);
  assert.match(html, /<main id="main" tabindex="-1">/);
  assert.match(css, /\.skip-link\s*\{[^}]*transform:\s*translateY\(calc\(-100% - 16px\)\)/s);
  assert.match(css, /\.skip-link:focus\s*\{[^}]*transform:\s*translateY\(0\)/s);
});

test("keyboard focus indicator contrasts with the app's light and dark surfaces", () => {
  assert.match(css, /:focus-visible\s*\{[^}]*outline:\s*2px solid #fffef9/s);
  assert.match(css, /:focus-visible\s*\{[^}]*box-shadow:\s*0 0 0 5px #244c3e/s);
  for (const background of ["#fffef9", "#f8f6ef", "#f0f0e5"])
    assert.ok(contrast("#244c3e", background) >= 3, `${background} needs a contrasting dark ring`);
  assert.ok(contrast("#fffef9", "#244c3e") >= 3, "the light ring needs contrast on dark controls");
});
