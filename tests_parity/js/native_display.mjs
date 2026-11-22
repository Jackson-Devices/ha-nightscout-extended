// Nightscout's own current-reading display, run from the stock server's source.
//
// Input (stdin JSON): { lib, settings: { units, thresholds, theme }, cases: [mgdl, ...] }
// Output: [{ mgdl, text, cls }]
//
// The functions are extracted verbatim from lib/client/index.js of the running stock container and evaluated
// with only the objects they reference stubbed out. Nothing here re-implements Nightscout's rules.
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

const client = {
  settings: input.settings,
  timeago: { checkStatus: () => "current" },
  sbx: {},
  errorcodes: { toDisplay: (value) => `error code ${value}` },
};
const currentBG = {
  out: "",
  text(value) { this.out = String(value); return this; },
  html(value) { this.out = String(value); return this; },
};
const make = new Function(
  "client", "units", "currentBG", "adjustCurrentSGVClasses",
  [extract("scaleBg"), extract("sgvToColoredRange"), extract("updateCurrentSGV"),
    "return { sgvToColoredRange, updateCurrentSGV };"].join("\n"),
);
const native = make(client, units, currentBG, () => {});

const out = input.cases.map((mgdl) => {
  currentBG.out = "";
  native.updateCurrentSGV({ mgdl });
  return { mgdl, text: currentBG.out, cls: native.sgvToColoredRange(mgdl) };
});
process.stdout.write(JSON.stringify(out));
