const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const appSource = fs.readFileSync(
  path.join(__dirname, "../src/home_kitchen/static/app.js"),
  "utf8",
);

function sourceFunction(name) {
  const match = appSource.match(new RegExp(`function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `app.js should define ${name}`);
  return match[0];
}

function buildAppLocaleHarness(language) {
  const context = {
    state: {
      household: language === undefined ? {} : { language },
      shoppingRefreshInFlight: false,
      shoppingLastRefreshedAt: "2026-11-16T14:05:00Z",
    },
    Intl,
    Date,
    Number,
    String,
  };
  const fnNames = [
    "uiLanguage",
    "uiLocale",
    "dateLabel",
    "formatUiCount",
    "formatUiTime",
    "shoppingRefreshLabel",
    "proposalExpiryText",
  ];
  vm.runInNewContext(fnNames.map(sourceFunction).join("\n"), context);
  return context;
}

test("dateLabel, formatUiCount, meal time, and timestamps respect household.language (nb, en, fr, unset)", () => {
  const sampleDate = new Date(2026, 10, 16, 12);
  const sampleTimestamp = "2026-11-16T14:05:00Z";

  // Default / unset -> nb-NO
  const unsetCtx = buildAppLocaleHarness(undefined);
  assert.equal(unsetCtx.uiLocale(), "nb-NO");
  assert.equal(unsetCtx.uiLanguage(), "nb");
  assert.equal(
    unsetCtx.dateLabel(sampleDate, { month: "short", day: "numeric", year: "numeric" }),
    sampleDate.toLocaleDateString("nb-NO", { month: "short", day: "numeric", year: "numeric" }),
  );
  assert.equal(unsetCtx.formatUiCount(1234), new Intl.NumberFormat("nb-NO").format(1234));
  assert.equal(unsetCtx.formatUiTime("18:30:00"), "18:30");
  assert.equal(
    unsetCtx.shoppingRefreshLabel(),
    "Oppdatert kl. " +
      new Intl.DateTimeFormat("nb-NO", { hour: "numeric", minute: "2-digit" }).format(
        new Date(sampleTimestamp),
      ),
  );
  assert.equal(
    unsetCtx.proposalExpiryText(sampleTimestamp),
    "Forslaget utløper " + new Date(Date.parse(sampleTimestamp)).toLocaleString("nb-NO") + ".",
  );

  // Explicit nb -> nb-NO
  const nbCtx = buildAppLocaleHarness("nb");
  assert.equal(nbCtx.uiLocale(), "nb-NO");
  assert.equal(nbCtx.formatUiTime("08:00"), "08:00");

  // English -> en
  const enCtx = buildAppLocaleHarness("en");
  assert.equal(enCtx.uiLocale(), "en");
  assert.equal(enCtx.uiLanguage(), "en");
  assert.equal(
    enCtx.dateLabel(sampleDate, { month: "short", day: "numeric", year: "numeric" }),
    sampleDate.toLocaleDateString("en", { month: "short", day: "numeric", year: "numeric" }),
  );
  assert.equal(enCtx.formatUiCount(1234), new Intl.NumberFormat("en").format(1234));
  assert.equal(
    enCtx.formatUiTime("18:30:00"),
    new Intl.DateTimeFormat("en", { hour: "numeric", minute: "2-digit" }).format(
      new Date(2000, 0, 1, 18, 30),
    ),
  );
  assert.equal(
    enCtx.shoppingRefreshLabel(),
    "Oppdatert kl. " +
      new Intl.DateTimeFormat("en", { hour: "numeric", minute: "2-digit" }).format(
        new Date(sampleTimestamp),
      ),
  );
  assert.equal(
    enCtx.proposalExpiryText(sampleTimestamp),
    "Forslaget utløper " + new Date(Date.parse(sampleTimestamp)).toLocaleString("en") + ".",
  );

  // French -> fr
  const frCtx = buildAppLocaleHarness("fr");
  assert.equal(frCtx.uiLocale(), "fr");
  assert.equal(frCtx.uiLanguage(), "fr");
  assert.equal(
    frCtx.dateLabel(sampleDate, { month: "short", day: "numeric", year: "numeric" }),
    sampleDate.toLocaleDateString("fr", { month: "short", day: "numeric", year: "numeric" }),
  );
  assert.equal(frCtx.formatUiCount(1234), new Intl.NumberFormat("fr").format(1234));
  assert.equal(
    frCtx.formatUiTime("18:30:00"),
    new Intl.DateTimeFormat("fr", { hour: "numeric", minute: "2-digit" }).format(
      new Date(2000, 0, 1, 18, 30),
    ),
  );
  assert.equal(
    frCtx.proposalExpiryText(sampleTimestamp),
    "Forslaget utløper " + new Date(Date.parse(sampleTimestamp)).toLocaleString("fr") + ".",
  );
});
