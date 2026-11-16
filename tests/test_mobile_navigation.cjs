"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/home_kitchen/static/app.js"), "utf8");
const handler = source.match(/window\.addEventListener\("hashchange", \(\) => \{[\s\S]*?\n\}\);(?=\nwindow\.addEventListener\("offline")/u);
assert.ok(handler, "the app handles view navigation through the URL hash");

test("changing views starts the next screen at the top", () => {
  let onHashChange;
  let scrollPosition = [0, 540];
  let refreshes = 0;
  const state = { view: "shopping", shoppingPanel: "single" };
  const location = { hash: "#recipes" };
  const context = {
    state,
    location,
    window: {
      addEventListener: (name, callback) => { if (name === "hashchange") onHashChange = callback; },
      scrollTo: (...position) => { scrollPosition = position; },
    },
    refreshView: () => { refreshes += 1; },
  };
  vm.runInNewContext(handler[0], context);
  onHashChange();
  assert.equal(state.view, "recipes");
  assert.deepEqual(scrollPosition, [0, 0]);
  assert.equal(refreshes, 1);
});
