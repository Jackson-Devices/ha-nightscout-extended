// Nightscout's own current-reading display and non-alarming paint from the pinned stock server.
//
// Input (stdin JSON): { lib, css?, settings: { units, thresholds, theme, alarmTimeago* }, now?,
//                       cases: [mgdl | { mgdl, ageMs }, ...] }
// Freshness comes from Nightscout's own lib/plugins/timeago.js checkStatus, and error-code text from
// lib/plugins/errorcodes.js, both from the stock container. A bare number is a reading taken at `now`.
// Output: [{ mgdl, text, numericText, rangeClass, paint?: { background, foreground } }]
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const input = JSON.parse(readFileSync(0, "utf8"));
const require = createRequire(import.meta.url);
const units = require(path.join(input.lib, "units.js"))();
const source = readFileSync(path.join(input.lib, "client", "index.js"), "utf8");

function extract(name) {
  const start = source.indexOf(`function ${name} (`);
  if (start < 0) throw new Error(`Nightscout function ${name} not found in lib/client/index.js`);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

// Read the pinned main.css declarations applied to the idle page and its current BG button.
// The button's text inherits body color when the stock script applies no range class.
// Refuse changes to these selectors/declarations rather than guessing a replacement palette.
const rules = input.css === undefined
  ? null
  : [...input.css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{}]+)\{([^{}]*)\}/g)];
function declaration(selector, property) {
  const matches = rules.filter(([, selectors]) => selectors.trim() === selector);
  if (matches.length !== 1) throw new Error(`Expected a single Nightscout style rule for ${selector}`);
  const value = matches[0][2].match(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`))?.[1].trim();
  if (!value) throw new Error(`Missing ${property} for Nightscout style rule ${selector}`);
  return value;
}
const idlePaint = rules && { background: declaration("body", "background"), foreground: declaration("body", "color") };

// lib/plugins/errorcodes.js: extract code2Display and toDisplay verbatim (the module itself needs lodash).
const errorSource = readFileSync(path.join(input.lib, "plugins", "errorcodes.js"), "utf8");
const tableStart = errorSource.indexOf("var code2Display = {");
const tableEnd = errorSource.indexOf("};", tableStart) + 2;
const toDisplayStart = errorSource.indexOf("function toDisplay (");
let toDisplayEnd = errorSource.indexOf("{", toDisplayStart);
for (let depth = 0; toDisplayEnd < errorSource.length; toDisplayEnd++) {
  if (errorSource[toDisplayEnd] === "{") depth++;
  else if (errorSource[toDisplayEnd] === "}" && --depth === 0) { toDisplayEnd++; break; }
}
if (tableStart < 0 || toDisplayStart < 0) throw new Error("Nightscout errorcodes.js layout not recognised");
const toDisplay = new Function(
  `${errorSource.slice(tableStart, tableEnd)}\n${errorSource.slice(toDisplayStart, toDisplayEnd)}\nreturn toDisplay;`,
)();

// lib/plugins/timeago.js needs only lib/times.js; its checkStatus is Nightscout's freshness rule.
const timeago = require(path.join(input.lib, "plugins", "timeago.js"))({
  language: { translate: (text) => text }, levels: {},
});
const nowMs = input.now === undefined ? Date.now() : new Date(input.now).getTime();

const client = {
  settings: input.settings,
  timeago,
  sbx: {},
  errorcodes: { toDisplay },
};
const container = {
  rangeClass: "",
  removeClass() { this.rangeClass = ""; },
  addClass(value) { this.rangeClass = value; },
};
const currentBG = {
  out: "",
  text(value) { this.out = String(value); return this; },
  html(value) { this.out = String(value); return this; },
  toggleClass() {},
};
const bgStatus = { toggleClass() {} };
const make = new Function(
  "client", "units", "currentBG", "container", "bgStatus", "inRetroMode", "alarmingNow",
  [
    extract("scaleBg"),
    extract("sgvToColoredRange"),
    extract("adjustCurrentSGVClasses"),
    extract("updateCurrentSGV"),
    "return { updateCurrentSGV, scaleBg };",
  ].join("\n"),
);
const { updateCurrentSGV, scaleBg } = make(client, units, currentBG, container, bgStatus, () => false, () => false);

const out = input.cases.map((item) => {
  const { mgdl, ageMs = 0 } = typeof item === "number" ? { mgdl: item } : item;
  const entry = { mgdl, mills: nowMs - ageMs };
  // checkStatus reads sbx.time, sbx.settings and sbx.lastSGVEntry(); "server" disables browser-only
  // hibernation detection, which depends on wall-clock time between calls.
  client.sbx = { runtimeEnvironment: "server", time: nowMs, settings: input.settings, lastSGVEntry: () => entry };
  currentBG.out = "";
  updateCurrentSGV(entry);
  const { rangeClass } = container;
  const status = timeago.checkStatus(client.sbx);
  const display = { mgdl, ageMs, status, text: currentBG.out, numericText: String(scaleBg(mgdl)), rangeClass };
  if (!idlePaint) return display;
  const paint = rangeClass
    ? { ...idlePaint, foreground: declaration(`.${rangeClass} .bgButton`, "color") }
    : idlePaint;
  return { ...display, paint };
});
process.stdout.write(JSON.stringify(out));
