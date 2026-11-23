// Compare the card's change text with Nightscout's own bgnow.calcDelta over a grid of readings.
// Input (stdin JSON): { lib, card, low, high, maxStep }
// Nightscout's lib/plugins/bgnow.js and lib/sandbox.js are loaded unchanged from the stock container (with its
// lodash copied beside lib/). Every pair (recent, previous) with low <= recent <= high and
// |recent - previous| <= maxStep is checked for both site units and both card display units:
//   - display unit == site unit: the card must show the server's delta.display as sent;
//   - display unit != site unit: the card computes from the bucket means and must equal what calcDelta shows
//     on a site set to the card's unit.
// Output: { checked, mismatches: [...first 50], mismatchCount }
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

const input = JSON.parse(readFileSync(0, "utf8"));
const require = createRequire(import.meta.url);

globalThis.customElements = { get: () => undefined, define() {} };
globalThis.window = globalThis;
globalThis.window.customCards = [];
globalThis.HTMLElement = class {};
console.info = () => {};
const { logic } = await import(pathToFileURL(input.card).href);
if (typeof logic?.deltaText !== "function") throw new Error("the card under test has no logic.deltaText");

const ctx = { language: { translate: (text) => text }, levels: {}, settings: {} };
const bgnow = require(path.join(input.lib, "plugins", "bgnow.js"))(ctx);
// sandbox.js defines scaleMgdl and roundBGToDisplayFormat in init(); both read only sbx.settings.units.
const sandboxes = Object.fromEntries(["mg/dl", "mmol"].map((units) => {
  const sbx = require(path.join(input.lib, "sandbox.js"))();
  sbx.settings = { units };
  return [units, sbx];
}));

function nightscout(units, recentMean, previousMean) {
  const sbx = sandboxes[units];
  // Two adjacent five-minute buckets, as fillBuckets produces for consecutive readings.
  return bgnow.calcDelta({ mean: recentMean, mills: 600000 }, { mean: previousMean, mills: 300000 }, sbx);
}

let checked = 0;
let mismatchCount = 0;
const mismatches = [];
for (let recent = input.low; recent <= input.high; recent++) {
  for (let previous = recent - input.maxStep; previous <= recent + input.maxStep; previous++) {
    const delta = { mgdl: nightscout("mg/dl", recent, previous), mmol: nightscout("mmol", recent, previous) };
    for (const site of ["mg/dl", "mmol"]) {
      const served = site === "mmol" ? delta.mmol : delta.mgdl;
      const attrs = {
        site_units: site, site_display: served.display,
        recent_mean_mgdl: recent, previous_mean_mgdl: previous,
      };
      for (const mmol of [false, true]) {
        checked++;
        const expected = (mmol ? delta.mmol : delta.mgdl).display;
        const card = logic.deltaText(attrs, mmol, served.mgdl / (mmol ? 18.016 : 1));
        if (card !== expected) {
          mismatchCount++;
          if (mismatches.length < 50) {
            mismatches.push({ what: "change text", recent, previous, site_units: site,
              display_units: mmol ? "mmol" : "mg/dl", nightscout: expected, card });
          }
        }
      }
    }
  }
}
process.stdout.write(JSON.stringify({ checked, mismatchCount, mismatches }));
