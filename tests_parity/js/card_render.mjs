// Render the bundled glucose card with real Home Assistant states and report what it shows.
//
// Input (stdin JSON): { card, now, cases: [{ glucose: <state>, delta?: <state>, last: <state> }] }
// Output: [{ text, paint: { background, foreground }, delta }]
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

await import(pathToFileURL(input.card).href);

function element() {
  return {
    attributes: {}, children: [], hidden: false, textContent: "",
    style: { setProperty(name, value) { this[name] = value; } },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

// Freeze both ways the card reads the clock, including its default local night-mode check.
process.env.TZ = "UTC";
const RealDate = Date;
const now = RealDate.parse(input.now);
if (!Number.isFinite(now)) throw new Error("Card render requires a valid observation time");
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
};
const out = input.cases.map((c) => {
  const card = new CardClass();
  card.setConfig({ show_chart: false, show_footer: false, show_clock: false });
  const names = ["card", "wrap", "chart", "clock", "name", "age", "value", "arrow", "delta", "bar", "fill", "footer", "missing"];
  card._el = Object.fromEntries(names.map((name) => [name, element()]));
  card._root = {};
  card._ids = { glucose: "sensor.glucose", last: "sensor.last", ...(c.delta ? { delta: "sensor.delta" } : {}) };
  card._hass = { states: { "sensor.glucose": c.glucose, "sensor.last": c.last, ...(c.delta ? { "sensor.delta": c.delta } : {}) } };
  card._render();
  const style = card._el.wrap.style;
  const paint = {
    background: style["--ngc-bg"] ?? null,
    foreground: style["--ngc-fg"] ?? null,
  };
  return {
    text: card._el.value.textContent,
    paint,
    delta: card._el.delta.textContent,
  };
});
globalThis.Date = RealDate;
process.stdout.write(JSON.stringify(out));
