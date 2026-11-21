// An actual card instance with the same minimal DOM approach as tests_js/card.test.mjs.
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";

const input = JSON.parse(readFileSync(0, "utf8"));
assert.ok(Number.isSafeInteger(input.now_ms));
const NativeDate = globalThis.Date;
globalThis.Date = class extends NativeDate {
  constructor(...args) {
    super(...(args.length ? args : [input.now_ms]));
  }
  static now() {
    return input.now_ms;
  }
};

function element() {
  return {
    textContent: "",
    innerHTML: "",
    hidden: false,
    children: [],
    attrs: {},
    style: {
      setProperty(name, value) { this[name] = value; },
    },
    setAttribute(name, value) { this.attrs[name] = value; },
    replaceChildren(...children) { this.children = children; },
  };
}

globalThis.HTMLElement = class {
  attachShadow() {
    const nodes = new Map();
    return {
      querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, element());
        return nodes.get(selector);
      },
    };
  }
};
globalThis.window = globalThis;
globalThis.window.addEventListener = () => {};
globalThis.window.removeEventListener = () => {};
globalThis.requestAnimationFrame = (callback) => callback();
globalThis.document = { createElement: element };
const registered = new Map();
globalThis.customElements = {
  get: (name) => registered.get(name),
  define: (name, cls) => registered.set(name, cls),
};

let product;
const savedInfo = console.info;
try {
  console.info = () => {};
  product = await import("../../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
} finally {
  console.info = savedInfo;
}
let classification = null;
const classify = product.logic.classify;
product.logic.classify = (mgdl, ageMin, staleMin, thresholds) => {
  const level = classify(mgdl, ageMin, staleMin, thresholds);
  classification = { mgdl, age_min: ageMin, stale_minutes: staleMin, thresholds, level };
  return level;
};

const card = new (registered.get("nightscout-glucose-card"))();
card.setConfig(input.config);
const calls = [];
let badCall = false;
try {
  card.connectedCallback();
  card.hass = {
    states: input.ha.states,
    entities: input.ha.entities,
    connected: true,
    async callWS(message) {
      calls.push(message);
      if (message.type !== "call_service" || message.domain !== "nightscout_extended" ||
          message.service !== "get_glucose_history" || message.return_response !== true ||
          JSON.stringify(message.service_data) !== JSON.stringify(input.history_request)) {
        badCall = true;
        throw new Error("Unexpected card history request");
      }
      return { response: input.history };
    },
  };
  await setImmediate();
  assert.ok(!badCall, "The card did not use the expected HA history action and range");
  assert.equal(calls.length, input.config.show_chart === false ? 0 : 1);
  assert.ok(!card._history.busy, "Card history is unsettled");
  const els = card._el;
  const svg = els.chart.innerHTML;
  const paths = [...svg.matchAll(/<path d="([^"]*)"/g)].map((match) => match[1]);
  const snapshot = {
    value: els.value.textContent,
    arrow: els.arrow.textContent,
    delta: els.delta.textContent,
    age: els.age.textContent,
    footer: els.footer.children.map((child) => child.textContent),
    aria_label: els.wrap.attrs["aria-label"] ?? null,
    background: els.wrap.style["--ngc-bg"] ?? null,
    foreground: els.wrap.style["--ngc-fg"] ?? null,
    classification,
    chart: {
      svg,
      measured_path: paths[0] ?? null,
      forecast_path: paths[1] ?? null,
      history_mode: card._history.mode,
      status: card._history.status,
      points: card._history.pts,
      missing: card._history.missing,
      calls,
    },
    now_ms: input.now_ms,
    card_version: product.CARD_VERSION,
  };
  process.stdout.write(JSON.stringify(snapshot) + "\n");
} finally {
  product.logic.classify = classify;
  card.disconnectedCallback();
  globalThis.Date = NativeDate;
}
