// Render the bundled glucose card with real Home Assistant states and report what it shows.
//
// Input (stdin JSON): { card, now, cases: [{ glucose: <state>, delta?: <state>, last: <state> }] }
// Output: [{ text, level, delta }]
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const input = JSON.parse(readFileSync(0, "utf8"));

let CardClass;
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define(_name, cls) { CardClass = cls; } };
globalThis.window = globalThis;
globalThis.window.customCards = [];
globalThis.document = { createElement: () => ({ textContent: "" }) };
globalThis.requestAnimationFrame = () => 0;
console.info = () => {};

const { logic } = await import(pathToFileURL(input.card).href);
const levelByColour = Object.fromEntries(
  ["stale", "urgent_low", "low", "in_range", "high", "urgent_high"].map((level) => [logic.colors(level, false).bg, level]),
);

function element() {
  return {
    attributes: {}, children: [], hidden: false, textContent: "",
    style: { setProperty(name, value) { this[name] = value; } },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

const realNow = Date.now;
Date.now = () => new Date(input.now).getTime();
const out = input.cases.map((c) => {
  const card = new CardClass();
  card.setConfig({ show_chart: false, show_footer: false, show_clock: false, night_mode: false });
  const names = ["card", "wrap", "chart", "clock", "name", "age", "value", "arrow", "delta", "bar", "fill", "footer", "missing"];
  card._el = Object.fromEntries(names.map((name) => [name, element()]));
  card._root = {};
  card._ids = { glucose: "sensor.glucose", last: "sensor.last", ...(c.delta ? { delta: "sensor.delta" } : {}) };
  card._hass = { states: { "sensor.glucose": c.glucose, "sensor.last": c.last, ...(c.delta ? { "sensor.delta": c.delta } : {}) } };
  card._render();
  return {
    text: card._el.value.textContent,
    level: levelByColour[card._el.wrap.style["--ngc-bg"]] ?? `unknown colour ${card._el.wrap.style["--ngc-bg"]}`,
    delta: card._el.delta.textContent,
  };
});
Date.now = realNow;
process.stdout.write(JSON.stringify(out));
