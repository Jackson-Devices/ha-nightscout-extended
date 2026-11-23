// Render the bundled card with real HA states through its normal setup and update callbacks.
// Input (stdin JSON): { card, now, cases: [{ glucose, delta?, last, now? }] }
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const input = JSON.parse(readFileSync(0, "utf8"));
let CardClass;
globalThis.customElements = { get: () => undefined, define(_name, cls) { CardClass = cls; } };
globalThis.window = globalThis;
globalThis.window.customCards = [];
globalThis.window.addEventListener = () => {};
globalThis.window.removeEventListener = () => {};
globalThis.document = { createElement: () => element() };
globalThis.requestAnimationFrame = () => 0;
console.info = () => {};

function element(attributes = {}) {
  return {
    attributes, children: [], hidden: "hidden" in attributes, textContent: "",
    style: { setProperty(name, value) { this[name] = value; } },
    replaceChildren(...children) { this.children = children; },
    setAttribute(name, value) { this.attributes[name] = value; },
  };
}

// Resolve only the tag/class selectors the card uses from its own markup. A
// selector without an element fails when the card tries to render into it.
globalThis.HTMLElement = class {
  isConnected = true;
  attachShadow() {
    return {
      set innerHTML(markup) {
        const body = markup.replace(/<style>[\s\S]*?<\/style>/g, "");
        this.nodes = [...body.matchAll(/<([\w-]+)([^<>]*)>/g)].map(([, tag, raw]) => {
          const attrs = Object.fromEntries(
            [...raw.matchAll(/([\w-]+)(?:="([^"]*)")?/g)].map(([, key, value]) => [key, value ?? ""]),
          );
          return { tag, classes: (attrs.class ?? "").split(/\s+/), el: element(attrs) };
        });
      },
      querySelector(selector) {
        const [tag, cls] = selector.split(".");
        return this.nodes.find((node) =>
          (!tag || node.tag === tag) && (!cls || node.classes.includes(cls)))?.el ?? null;
      },
    };
  }
};

await import(pathToFileURL(input.card).href);

// Freeze both ways the card reads the clock, including its local night-mode check.
process.env.TZ = "UTC";
const RealDate = Date;
let now;
globalThis.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [now])); }
  static now() { return now; }
};
const out = input.cases.map((c) => {
  now = RealDate.parse(c.now ?? input.now);
  if (!Number.isFinite(now)) throw new Error("Card render requires a valid observation time");

  const card = new CardClass();
  card.setConfig({ entity: "sensor.parity_glucose", show_chart: false, show_footer: false, show_clock: false });
  card.connectedCallback();
  try {
    card.hass = {
      states: {
        "sensor.parity_glucose": c.glucose,
        ...(c.last ? { "sensor.parity_last_reading": c.last } : {}),
        ...(c.delta ? { "sensor.parity_glucose_change": c.delta } : {}),
      },
    };
    const wrap = card._root.querySelector(".wrap");
    const alarm = card._root.querySelector(".alarm");
    return {
      text: card._root.querySelector(".value").textContent,
      paint: { background: wrap.style["--ngc-bg"] ?? null, foreground: wrap.style["--ngc-fg"] ?? null },
      delta: card._root.querySelector(".delta").textContent,
      ariaLabel: wrap.attributes["aria-label"] ?? null,
      hasAlarmElement: alarm !== null,
      alarm: alarm && !alarm.hidden ? alarm.textContent : null,
    };
  } finally {
    card.disconnectedCallback();
  }
});
globalThis.Date = RealDate;
process.stdout.write(JSON.stringify(out));
