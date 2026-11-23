// Observe the 15.0.6 threshold plugin's requested alarm without a notifier.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const { lib, cases } = JSON.parse(readFileSync(0, "utf8"));
const require = createRequire(import.meta.url);
const native = (...parts) => path.join(lib, ...parts);
const units = require(native("units.js"))();
const sandbox = readFileSync(native("sandbox.js"), "utf8");
const levelsSource = readFileSync(native("levels.js"), "utf8");
const levelsModule = { exports: {} };
// Localization affects only the ignored title. Keep stock constants, severity
// checks and the plugin itself in charge of the alarm.
new Function("require", "module", levelsSource)((name) => {
  if (name === "./constants") return require(native("constants.json"));
  if (name === "./language") return () => ({ translate: (text) => text });
  throw new Error(`Unexpected stock levels dependency: ${name}`);
}, levelsModule);
const levels = levelsModule.exports;
const plugin = require(native("plugins", "simplealarms.js"))({ levels });

function sandboxFunction(name) {
  const start = sandbox.indexOf(`sbx.${name} = function ${name} (`);
  if (start < 0) throw new Error(`Nightscout sandbox function missing: ${name}`);
  const end = sandbox.indexOf("\n  };", start);
  if (end < 0) throw new Error(`Nightscout sandbox function has no end: ${name}`);
  return sandbox.slice(start, end + "\n  };".length);
}
const installScaling = new Function("sbx", "units", [
  sandboxFunction("scaleEntry"), sandboxFunction("scaleMgdl"),
].join("\n"));

const out = cases.map(({ settings, entry, now }) => {
  const requested = [];
  const sbx = {
    settings, time: now,
    // Input deliberately supplies the known latest raw reading. Acquisition
    // and rejection of an invalid latest wire row are exercised in HA.
    lastSGVEntry: () => entry,
    buildDefaultMessage: () => "",
    notifications: { requestNotify(args) { requested.push(args); } },
  };
  installScaling(sbx, units);
  plugin.checkNotifications(sbx);
  if (requested.length === 0) return "none";
  if (requested.length !== 1) throw new Error("Expected at most one stock request per reading");
  const [{ level, eventName }] = requested;
  if (![levels.WARN, levels.URGENT].includes(level) || !["high", "low"].includes(eventName)) {
    throw new Error("Unexpected stock alarm level or event");
  }
  return level === levels.URGENT ? `urgent_${eventName}` : eventName;
});
process.stdout.write(JSON.stringify(out));
