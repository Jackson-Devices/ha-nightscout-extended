// Nightscout's own current-reading display and non-alarming paint from the pinned stock server.
//
// Input (stdin JSON): { lib, css?, settings: { units, thresholds, theme }, cases: [mgdl, ...] }
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

const client = {
  settings: input.settings,
  timeago: { checkStatus: () => "current" },
  sbx: {},
  errorcodes: { toDisplay: (value) => `error code ${value}` },
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

const out = input.cases.map((mgdl) => {
  currentBG.out = "";
  updateCurrentSGV({ mgdl });
  const { rangeClass } = container;
  const display = { mgdl, text: currentBG.out, numericText: String(scaleBg(mgdl)), rangeClass };
  if (!idlePaint) return display;
  const paint = rangeClass
    ? { ...idlePaint, foreground: declaration(`.${rangeClass} .bgButton`, "color") }
    : idlePaint;
  return { ...display, paint };
});
process.stdout.write(JSON.stringify(out));
