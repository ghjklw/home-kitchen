const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const source = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);
const helper = source.match(/function agendaMeals\(meals, date\) \{([\s\S]*?)\n\}/);
assert.ok(helper, "app.js should define the selected-date agenda selector");
const agendaMeals = new Function("meals", "date", helper[1]);
function sourceFunction(name) {
  const match = source.match(new RegExp(`function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `app.js should define ${name}`);
  return match[0];
}
const mealSlotLabel = new Function(
  `return (${sourceFunction("mealSlotLabel")})`,
)();
const mealAgendaMetadata = new Function(
  "formatServings",
  "mealSlotLabel",
  `return (${sourceFunction("mealAgendaMetadata")})`,
)((value) => String(value) + (Number(value) === 1 ? " porsjon" : " porsjoner"), mealSlotLabel);
const buildMealWriteRequest = new Function(
  `return (${sourceFunction("buildMealWriteRequest")})`,
)();

test("selects all existing meals for one date without sorting or mutating them", () => {
  const rows = [
    { id: "late", date: "2026-11-18", scheduled_time: "19:00" },
    { id: "other-day", date: "2026-11-19", slot: "dinner" },
    { id: "early", date: "2026-11-18", scheduled_time: "08:00" },
    { id: "without-slot", date: "2026-11-18" },
  ];

  assert.deepEqual(
    agendaMeals(rows, "2026-11-18").map((meal) => meal.id),
    ["late", "early", "without-slot"],
  );
  assert.deepEqual(rows.map((meal) => meal.id), ["late", "other-day", "early", "without-slot"]);
});

test("formats selected-date rows using labels and scheduled times, with legacy slots localized", () => {
  const rows = agendaMeals(
    [
      { id: "a", date: "2026-11-18", label: "Matpakke", scheduled_time: "12:30", servings: 3 },
      { id: "b", date: "2026-11-19", label: "Middag", scheduled_time: null, servings: 2 },
      { id: "c", date: "2026-11-18", slot: "breakfast", servings: 1 },
    ],
    "2026-11-18",
  );
  assert.deepEqual(rows.map((meal) => meal.id), ["a", "c"]);
  assert.equal(mealAgendaMetadata(rows[0]), "Matpakke · 12:30 · 3 porsjoner");
  assert.equal(mealAgendaMetadata(rows[1]), "Frokost · 1 porsjon");
});

test("creates append-only meal requests and updates only the selected meal ID", () => {
  const values = {
    date: "2026-11-18",
    label: "  Matpakke  ",
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: "3",
  };
  const added = buildMealWriteRequest(null, values);
  assert.equal(added.path, "/meals");
  assert.equal(added.method, "POST");
  assert.deepEqual(added.payload, {
    date: "2026-11-18",
    label: "Matpakke",
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: 3,
  });
  const updated = buildMealWriteRequest({ id: "meal/1" }, { ...values, label: "  " });
  assert.equal(updated.path, "/meals/meal%2F1");
  assert.equal(updated.method, "PUT");
  assert.deepEqual(updated.payload, {
    date: "2026-11-18",
    label: null,
    scheduled_time: "12:30",
    recipe_id: "recipe-1",
    servings: 3,
  });
  assert.equal("slot" in updated.payload, false);
});

test("unnamed flexible meals have no hidden default meal type", () => {
  assert.equal(mealAgendaMetadata({ servings: 2 }), "2 porsjoner");
  const request = buildMealWriteRequest(null, {
    date: "2026-11-18",
    label: "",
    scheduled_time: "",
    recipe_id: "recipe-1",
    servings: 2,
  });
  assert.equal(request.payload.label, null);
  assert.equal(request.payload.scheduled_time, null);
  assert.equal("slot" in request.payload, false);
});
