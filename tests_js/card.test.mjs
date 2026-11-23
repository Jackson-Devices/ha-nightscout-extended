// Unit tests for the glucose card's pure logic and rendered behavior. Run: node --test tests_js
import { test } from "node:test";
import assert from "node:assert/strict";

let CardClass;
// Just enough DOM for the module to load and render outside a browser.
globalThis.HTMLElement = class {
  attachShadow() {
    const nodes = new Map();
    return {
      innerHTML: "",
      querySelector(selector) {
        const [tag, className] = selector.split(".");
        const markup = [...this.innerHTML.matchAll(/<([a-z][a-z0-9-]*)([^>]*)>/gi)]
          .find(([, name, attributes]) => {
            const classes = attributes.match(/class="([^"]*)"/)?.[1].split(/\s+/) || [];
            return (!tag || tag === name) && (!className || classes.includes(className));
          });
        if (!markup) return null;
        if (!nodes.has(selector)) {
          nodes.set(selector, Object.assign(element(), { hidden: /\bhidden\b/.test(markup[2]) }));
        }
        return nodes.get(selector);
      },
    };
  }
};
globalThis.customElements = {
  get: () => undefined,
  define(_name, constructor) { CardClass = constructor; },
};
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ textContent: "" }) };

const { logic } = await import("../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
const T = { urgent_low: 55, low: 80, high: 180, urgent_high: 260 };

function element() {
  return {
    attributes: {},
    children: [],
    hidden: false,
    style: {
      setProperty(name, value) { this[name] = value; },
    },
    textContent: "",
    replaceChildren(...children) {
      this.children = children;
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
  };
}

function sensor(state, unit, attributes = {}) {
  return {
    state: String(state),
    attributes: { unit_of_measurement: unit, ...attributes },
  };
}

const GLUCOSE = "sensor.example_glucose";
const SOURCE = "2026-01-01T12:00:00Z";
const NOW = "2026-01-01T12:05:00Z";
const ALARM_UNTIL = "2026-01-01T12:10:00Z";

function setupCard(config = {}) {
  const card = new CardClass();
  card.setConfig({
    show_chart: false,
    show_footer: true,
    show_clock: false,
    night_mode: false,
    stale_minutes: 15,
    // These tests check classification through the card's 5-colour palette; colour modes have their own tests.
    colour_mode: "five",
    ...config,
  });
  return card;
}

function hassState({ glucose, delta, forecast, last = "2026-01-01T11:30:00Z", callWS }) {
  return {
    states: {
      ...(glucose ? { [GLUCOSE]: glucose } : {}),
      "sensor.example_last_reading": { state: last, attributes: {} },
      ...(delta ? { "sensor.example_glucose_change": delta } : {}),
      ...(forecast ? { "sensor.example_forecast_in_30_minutes": forecast } : {}),
    },
    callWS,
  };
}

function renderCard({ showChart = false, now = NOW, config, ...states }) {
  const card = setupCard({ entity: GLUCOSE, show_chart: showChart, ...config });
  card._build();
  const realNow = Date.now;
  Date.now = () => Date.parse(now);
  try {
    card.hass = hassState(states);
  } finally {
    Date.now = realNow;
  }
  return card._el;
}

function glucose(state, unit, nativeMgdl, sourceTime = "2026-01-01T12:00:00Z") {
  return sensor(state, unit, {
    thresholds_mgdl: T,
    native_mgdl: nativeMgdl,
    source_time: sourceTime,
  });
}

function footerText(el) {
  return el.footer.children.map((child) => child.textContent);
}

test("classify uses Nightscout thresholds with inclusive target range", () => {
  assert.equal(logic.classify(54, 1, 15, T), "urgent_low");
  assert.equal(logic.classify(55, 1, 15, T), "low");
  assert.equal(logic.classify(79, 1, 15, T), "low");
  assert.equal(logic.classify(80, 1, 15, T), "in_range");
  assert.equal(logic.classify(180, 1, 15, T), "in_range");
  assert.equal(logic.classify(181, 1, 15, T), "high");
  assert.equal(logic.classify(260, 1, 15, T), "high");
  assert.equal(logic.classify(261, 1, 15, T), "urgent_high");
});

test("rendered threshold band follows canonical mg/dL in both display units", () => {
  const cases = [
    { native: 180, mgdl: "180", mmol: "10.0", color: "#3d9a5b" },
    { native: 180.1, mgdl: "180", mmol: "10.0", color: "#f2df74" },
    { native: 260, mgdl: "260", mmol: "14.4", color: "#f2df74" },
    { native: 260.1, mgdl: "260", mmol: "14.4", color: "#ef7a78" },
  ];
  for (const item of cases) {
    for (const [unit, displayed] of [["mg/dL", item.mgdl], ["mmol/L", item.mmol]]) {
      const el = renderCard({ glucose: glucose(displayed, unit, item.native) });
      assert.equal(el.wrap.style["--ngc-bg"], item.color, unit + " at " + item.native + " mg/dL");
    }
  }
});

test("current value stays numeric across Nightscout display limits in both units", () => {
  const cases = [
    { native: 39, mgdl: "39", mmol: "2.2" },
    { native: 40, mgdl: "40", mmol: "2.2" },
    { native: 400, mgdl: "400", mmol: "22.2" },
    { native: 401, mgdl: "401", mmol: "22.3" },
  ];
  for (const { native, mgdl, mmol } of cases) {
    const mg = renderCard({ glucose: glucose(mgdl, "mg/dL", native) });
    const m = renderCard({ glucose: glucose(mmol, "mmol/L", native) });
    assert.equal(mg.value.textContent, mgdl);
    assert.equal(m.value.textContent, mmol);
    assert.ok(mg.wrap.attributes["aria-label"].startsWith(`Glucose ${mgdl} mg/dL,`));
    assert.ok(m.wrap.attributes["aria-label"].startsWith(`Glucose ${mmol} mmol/L,`));
  }
});

test("native mmol/L display rounds using Nightscout, including change and forecast", () => {
  // 181.058 mg/dL falls between the Nightscout and HA 10.05 mmol/L boundaries.
  const el = renderCard({
    glucose: glucose("10.04984458", "mmol/L", 181.058),
    delta: sensor("181.058", "mg/dL", { native_mgdl: 181.058 }),
    forecast: sensor("181.058", "mg/dL", { native_mgdl: 181.058 }),
  });
  assert.equal(el.value.textContent, "10.1");
  assert.equal(el.delta.textContent, "+10.1");
  assert.ok(footerText(el).includes("30 min ≈ 10.1"));
});

test("older glucose entities keep numeric values in their selected unit", () => {
  for (const [mgdl, mmol] of [[39, "2.2"], [40, "2.2"], [400, "22.2"], [401, "22.3"]]) {
    const mg = renderCard({ glucose: sensor(mgdl, "mg/dL"), last: SOURCE });
    const m = renderCard({ glucose: sensor(mgdl / 18.016, "mmol/L"), last: SOURCE });
    assert.equal(mg.value.textContent, String(mgdl));
    assert.equal(m.value.textContent, mmol);
  }
});

function chartPath(chart) {
  const [, path] = chart.innerHTML.match(/<path d="([^"]*)"/);
  return [...path.matchAll(/[ML]([\d.-]+),([\d.-]+)/g)].map(([, x, y]) => [+x, +y]);
}

test("Recorder history and native current readings remain distinguishable in the SVG", async (t) => {
  t.mock.method(Date, "now", () => Date.parse(NOW));
  const card = setupCard({ show_chart: true });
  card._build();
  const history = [38, 39, 300, 400].map((value, index) => ({
    lu: Date.parse(SOURCE) / 1000 - (4 - index) * 60,
    s: String(value / 18.016),
  }));
  const queries = [];
  card.hass = hassState({
    glucose: glucose("22.3", "mmol/L", 401),
    callWS: async (query) => {
      queries.push(query);
      return { [GLUCOSE]: history };
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(queries.map((query) => query.entity_ids), [[GLUCOSE]]);
  const points = chartPath(card._el.chart);
  assert.deepEqual(points, [
    [814.3, 400],
    [819, 398.9],
    [823.8, 111.3],
    [828.6, 1.1],
    [833.3, 0],
  ]);
  assert.equal(card._el.value.textContent, "22.3");
  card.hass = hassState({ glucose: glucose("22.5", "mmol/L", 405, "2026-01-01T12:01:00Z") });
  const updated = chartPath(card._el.chart);
  assert.deepEqual(updated.at(-1), [838.1, 0]);
  assert.ok(updated.at(-2)[1] > updated.at(-1)[1]);
});

test("normal chart values retain the default scale; target and visible forecast can expand it", () => {
  const normal = renderCard({ glucose: glucose("250", "mg/dL", 250), showChart: true });
  assert.deepEqual(chartPath(normal.chart), [[833.3, 76.9]]);

  const el = renderCard({
    glucose: sensor(300, "mg/dL", {
      native_mgdl: 300,
      source_time: SOURCE,
      thresholds_mgdl: { ...T, high: 320 },
    }),
    forecast: sensor(401, "mg/dL", {
      forecast: [{ time: "2026-01-01T12:30:00Z", mgdl: 401 }],
    }),
    showChart: true,
  });
  assert.deepEqual(chartPath(el.chart), [[833.3, 111.9]]);
  assert.match(el.chart.innerHTML, /<path d="M833\.3,111\.9L976\.2,0\.0"/);

  const targetOnly = renderCard({
    glucose: sensor(300, "mg/dL", {
      native_mgdl: 300,
      source_time: SOURCE,
      thresholds_mgdl: { ...T, high: 320 },
    }),
    showChart: true,
  });
  assert.deepEqual(chartPath(targetOnly.chart), [[833.3, 28.6]]);
});

function withAlarm(value, unit, mgdl, alarm, until = ALARM_UNTIL) {
  const state = glucose(value, unit, mgdl);
  state.attributes.simple_alarm = alarm;
  state.attributes.simple_alarm_until = until;
  return state;
}

test("configured card uses entity alarm state, independently of range colour and display unit", (t) => {
  let now = Date.parse(NOW);
  t.mock.method(Date, "now", () => now);
  let timeout;
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    timeout = { callback, delay };
    return timeout;
  });
  t.mock.method(globalThis, "clearTimeout", (scheduled) => {
    if (scheduled === timeout) timeout = undefined;
  });
  t.mock.method(globalThis, "setInterval", () => 0);
  globalThis.addEventListener = () => {};
  globalThis.removeEventListener = () => {};
  globalThis.requestAnimationFrame = (callback) => callback();

  const card = setupCard();
  card.isConnected = true;
  card.connectedCallback();
  t.after(() => {
    card.disconnectedCallback();
    delete globalThis.addEventListener;
    delete globalThis.removeEventListener;
    delete globalThis.requestAnimationFrame;
  });
  for (const [value, unit, mgdl, alarm, label, background] of [
    [250, "mg/dL", 250, "high", "High alarm", "#f2df74"], // Nightscout threshold 180.
    [250, "mg/dL", 250, "none", "", "#f2df74"], // Home Assistant threshold 300.
    ["17.2", "mmol/L", 310, "high", "High alarm", "#ef7a78"],
    [40, "mg/dL", 40, "urgent_low", "Urgent low alarm", "#c62828"],
    ["3.3", "mmol/L", 60, "low", "Low alarm", "#e8615a"],
    [401, "mg/dL", 401, "urgent_high", "Urgent high alarm", "#ef7a78"],
  ]) {
    card.hass = hassState({ glucose: withAlarm(value, unit, mgdl, alarm) });
    const { value: reading, wrap, alarm: badge } = card._el;
    assert.equal(reading.textContent, String(value));
    assert.equal(wrap.style["--ngc-bg"], background);
    assert.ok(badge, "the built card includes an alarm badge");
    assert.equal(badge.textContent, label);
    assert.equal(badge.hidden, !label);
    if (label) assert.ok(wrap.attributes["aria-label"].includes(label));
    else assert.doesNotMatch(wrap.attributes["aria-label"], /alarm/i);
  }

  assert.equal(timeout.delay, 5 * 60 * 1000);
  now += timeout.delay;
  const expire = timeout.callback;
  timeout = undefined;
  expire();
  assert.equal(card._el.alarm.hidden, true);
  assert.equal(card._el.alarm.textContent, "");
  assert.equal(card._el.value.textContent, "401");
  assert.doesNotMatch(card._el.wrap.attributes["aria-label"], /alarm/i);
});

test("missing, disabled, unknown, stale or unavailable readings have no active alarm", () => {
  const cases = [
    {},
    { glucose: glucose(401, "mg/dL", 401) },
    { glucose: withAlarm(401, "mg/dL", 401, "none") },
    { glucose: withAlarm(401, "mg/dL", 401, "unknown") },
    { glucose: withAlarm(401, "mg/dL", 401, "constructor") },
    { glucose: withAlarm(401, "mg/dL", 401, "urgent_high", null) },
    { glucose: withAlarm(401, "mg/dL", 401, "urgent_high", "not-a-timestamp") },
    { glucose: withAlarm(401, "mg/dL", 401, "urgent_high", NOW) },
    { glucose: withAlarm(401, "mg/dL", 401, "urgent_high"), config: { stale_minutes: 2 } },
    { glucose: withAlarm("unknown", "mg/dL", 401, "urgent_high") },
    { glucose: withAlarm("unavailable", "mg/dL", 401, "urgent_high") },
  ];
  for (const states of cases) {
    const { alarm, wrap } = renderCard(states);
    assert.ok(alarm, "the built card includes an alarm badge");
    assert.equal(alarm.hidden, true);
    assert.equal(alarm.textContent, "");
    assert.doesNotMatch(wrap.attributes["aria-label"] || "", /alarm/i);
  }
});

test("old, missing or non-numeric readings are stale", () => {
  assert.equal(logic.classify(120, 16, 15, T), "stale");
  assert.equal(logic.classify(NaN, 1, 15, T), "stale");
  assert.equal(logic.classify(120, NaN, 15, T), "stale");
});

test("unknown and unavailable states are not revived by canonical attributes", () => {
  for (const [state, retained] of [["unknown", 39], ["unavailable", 401]]) {
    const el = renderCard({ glucose: glucose(state, "mmol/L", retained) });
    assert.equal(el.value.textContent, "---");
    assert.equal(el.wrap.style["--ngc-bg"], "#5f6368");
    assert.equal(el.delta.textContent, "");
    assert.ok(footerText(el).includes("⚠ No recent reading"));
  }
});

test("glucose source timestamp controls rendered age before lagging last-reading fallback", () => {
  const el = renderCard({
    glucose: glucose("180", "mg/dL", 180, "2026-01-01T12:01:00Z"),
    last: "2026-01-01T11:30:00Z",
    now: "2026-01-01T12:05:00Z",
  });
  assert.equal(el.age.textContent, "4 min ago");
  assert.equal(el.wrap.style["--ngc-bg"], "#3d9a5b");
});

test("cached primary mmol glucose and invalid source time use entity fallbacks", () => {
  const el = renderCard({
    glucose: sensor(String(180 / 18.016), "mmol/L", {
      thresholds_mgdl: T,
      source_time: "not-a-timestamp",
    }),
    last: "2026-01-01T12:02:00Z",
    now: "2026-01-01T12:05:00Z",
  });
  assert.equal(el.wrap.style["--ngc-bg"], "#3d9a5b");
  assert.equal(el.age.textContent, "3 min ago");
});

test("mmol/L fallback preserves threshold equality and nearby fractional readings", () => {
  const thresholds_mgdl = { urgent_low: 48, low: 96, high: 183, urgent_high: 208 };
  const cases = [
    [48, "#e8615a"],       // low at the urgent low threshold
    [96, "#3d9a5b"],       // in range at the low threshold
    [183, "#3d9a5b"],      // in range at the high threshold
    [208, "#f2df74"],      // high at the urgent high threshold
    [95.9999, "#e8615a"],  // low even though display rounding hides the difference
    [183.0001, "#f2df74"], // high even though display rounding hides the difference
  ];
  const actual = cases.map(([mgdl]) => {
    const el = renderCard({
      glucose: sensor(mgdl / (180.16 / 10), "mmol/L", { thresholds_mgdl }),
      last: "2026-01-01T12:00:00Z",
    });
    return [mgdl, el.wrap.style["--ngc-bg"]];
  });
  assert.deepEqual(actual, cases);
});

test("mixed-unit siblings render from canonical mg/dL in the primary display unit", () => {
  const mgdl = renderCard({
    glucose: glucose("180", "mg/dL", 180),
    delta: sensor("0.5", "mmol/L", { native_mgdl: 9.5 }),
    forecast: sensor("10.0", "mmol/L", { native_mgdl: 180.5 }),
  });
  assert.equal(mgdl.delta.textContent, "+10");
  assert.ok(footerText(mgdl).includes("30 min ≈ 181"));

  const mmol = renderCard({
    glucose: glucose("10.0", "mmol/L", 180),
    delta: sensor("9.5", "mg/dL", { native_mgdl: 9.5 }),
    forecast: sensor("180.5", "mg/dL", { native_mgdl: 180.5 }),
  });
  assert.equal(mmol.delta.textContent, "+0.5");
  assert.ok(footerText(mmol).includes("30 min ≈ 10.0"));
});

test("missing or malformed canonical sibling attributes fall back through each sibling unit", () => {
  const el = renderCard({
    glucose: glucose("180", "mg/dL", 180),
    delta: sensor("1", "mmol/L"),
    forecast: sensor("10", "mmol/L", { native_mgdl: "bad" }),
  });
  assert.equal(el.delta.textContent, "+18");
  assert.ok(footerText(el).includes("30 min ≈ 180"));
});

test("night window wraps midnight and can be turned off", () => {
  const cfg = { night_mode: true, night_start: 21, night_end: 7 };
  assert.equal(logic.isNight(21, cfg), true);
  assert.equal(logic.isNight(3, cfg), true);
  assert.equal(logic.isNight(7, cfg), false);
  assert.equal(logic.isNight(12, cfg), false);
  assert.equal(logic.isNight(13, { night_mode: true, night_start: 13, night_end: 15 }), true);
  assert.equal(logic.isNight(3, { ...cfg, night_mode: false }), false);
  assert.equal(logic.isNight(3, { night_mode: true, night_start: 5, night_end: 5 }), false);
});

test("delta formatting in both units", () => {
  // Nightscout's delta.display: (v >= 0 ? '+' : '') + v (lib/plugins/bgnow.js).
  assert.equal(logic.formatDelta(6, false), "+6");
  assert.equal(logic.formatDelta(-18, false), "-18");
  assert.equal(logic.formatDelta(0.4, false), "+0");
  assert.equal(logic.formatDelta(0.33, true), "+0.3");
  assert.equal(logic.formatDelta(-1.05, true), "-1");
  assert.equal(logic.formatDelta(NaN, false), "");
});

test("age text", () => {
  assert.equal(logic.formatAge(0.5), "now");
  assert.equal(logic.formatAge(4.9), "4 min ago");
  assert.equal(logic.formatAge(125), "2 h ago");
  assert.equal(logic.formatAge(NaN), "no data");
});

test("thresholds fall back to Nightscout defaults per key", () => {
  assert.deepEqual(logic.thresholds(undefined), T);
  // Served numbers are used as served (stock serves numbers, and BG_LOW=0 is valid); non-numbers are not served.
  assert.deepEqual(logic.thresholds({ urgent_low: 0, low: 70, high: "200", urgent_high: -1 }),
    { urgent_low: 0, low: 70, high: 180, urgent_high: -1 });
});

test("night colours are coloured text on black", () => {
  assert.deepEqual(logic.colors("in_range", true), { bg: "#000000", fg: "#f3e7a6" });
  assert.equal(logic.colors("high", false).fg, "#1f1f1f");
});

test("card version matches manifest.json, so browsers reload it after an update", async () => {
  const { readFile } = await import("node:fs/promises");
  const { CARD_VERSION } = await import("../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
  const manifest = JSON.parse(await readFile(new URL("../custom_components/nightscout_extended/manifest.json", import.meta.url)));
  assert.equal(CARD_VERSION, manifest.version);
});


// ---- colour modes (AC-1.15) --------------------------------------------------------------------------------

const LEVELS = ["urgent_low", "low", "in_range", "high", "urgent_high"];

test("site mode follows Nightscout's sgvToColoredRange classes and stylesheet for each theme", () => {
  const cfg = { colour_mode: "site" };
  const expected = {
    colors: ["#ff0000", "#ffff00", "#4cff00", "#ffff00", "#ff0000"],
    colorblindfriendly: ["#ff0000", "#ffff00", "#bdbdbd", "#ffff00", "#ff0000"],
    default: ["#bdbdbd", "#bdbdbd", "#bdbdbd", "#bdbdbd", "#bdbdbd"],
  };
  for (const [theme, colours] of Object.entries(expected)) {
    LEVELS.forEach((level, i) => {
      for (const night of [false, true]) {
        assert.deepEqual(logic.paint(level, cfg, theme, night), { bg: "#000000", fg: colours[i] }, `${theme} ${level}`);
      }
    });
    assert.deepEqual(logic.paint("stale", cfg, theme, false), { bg: "#000000", fg: "#bdbdbd" });
  }
});

test("three mode uses Nightscout's three classes whatever the site theme", () => {
  const cfg = { colour_mode: "three" };
  for (const theme of ["default", "colorblindfriendly", "colors"]) {
    assert.deepEqual(LEVELS.map((level) => logic.paint(level, cfg, theme, false).fg),
      ["#ff0000", "#ffff00", "#4cff00", "#ffff00", "#ff0000"]);
  }
});

test("five mode keeps the card palette, including night mode", () => {
  for (const level of [...LEVELS, "stale"]) {
    for (const night of [false, true]) {
      assert.deepEqual(logic.paint(level, { colour_mode: "five" }, "default", night), logic.colors(level, night));
    }
  }
});

test("modes never change which Nightscout class a reading is in", () => {
  // five's levels split Nightscout's urgent/warning by direction; they must agree with three's classes.
  const three = LEVELS.map((level) => logic.paint(level, { colour_mode: "three" }, "colors", false).fg);
  const classOf = { "#ff0000": "urgent", "#ffff00": "warning", "#4cff00": "inrange" };
  assert.deepEqual(three.map((fg) => classOf[fg]), ["urgent", "warning", "inrange", "warning", "urgent"]);
});

test("custom modes apply the configured colours and accept the editor's RGB lists", () => {
  const three = setupCard({ colour_mode: "custom_three", colour_urgent: "#a00", colour_warning: [200, 150, 0],
    colour_inrange: "#00AA44" })._config;
  assert.deepEqual(LEVELS.map((level) => logic.paint(level, three, "default", false).fg),
    ["#aa0000", "#c89600", "#00aa44", "#c89600", "#aa0000"]);
  assert.equal(logic.paint("stale", three, "default", false).fg, "#bdbdbd");

  const five = setupCard({ colour_mode: "custom_five", colour_urgent_low: "#000080", colour_low: "#4040ff",
    colour_in_range: "#ffffff", colour_high: "#ffa500", colour_urgent_high: "#800000" })._config;
  assert.deepEqual(logic.paint("in_range", five, "default", false), { bg: "#ffffff", fg: "#1f1f1f" });
  assert.deepEqual(logic.paint("urgent_low", five, "default", false), { bg: "#000080", fg: "#ffffff" });
  assert.deepEqual(logic.paint("high", five, "default", true), { bg: "#000000", fg: "#ffa500" });
});

test("a missing or invalid custom colour, or an unknown mode, is a visible configuration error", () => {
  assert.throws(() => setupCard({ colour_mode: "custom_three", colour_urgent: "#f00", colour_warning: "#ff0" }),
    /colour_inrange is missing/);
  assert.throws(() => setupCard({ colour_mode: "custom_five", colour_urgent_low: "blue", colour_low: "#00f",
    colour_in_range: "#0f0", colour_high: "#ff0", colour_urgent_high: "#f00" }), /colour_urgent_low must be a colour/);
  assert.throws(() => setupCard({ colour_mode: "rainbow" }), /colour_mode must be one of/);
});

test("the default colour mode is site", () => {
  const card = new CardClass();
  card.setConfig({});
  assert.equal(card._config.colour_mode, "site");
});

test("the editor offers every colour mode and the custom colour pickers", () => {
  const form = CardClass.getConfigForm();
  const mode = form.schema.find((field) => field.name === "colour_mode");
  assert.deepEqual(mode.selector.select.options.map((option) => option.value),
    ["site", "three", "five", "custom_three", "custom_five"]);
  const pickers = form.schema.find((field) => field.type === "expandable").schema.map((field) => field.name);
  assert.deepEqual(pickers, ["colour_urgent", "colour_warning", "colour_inrange", "colour_urgent_low", "colour_low",
    "colour_in_range", "colour_high", "colour_urgent_high"]);
  for (const name of ["colour_mode", ...pickers]) assert.ok(form.computeLabel({ name }), `label for ${name}`);
});
