// Unit tests for the glucose card's pure logic and rendered behavior. Run: node --test tests_js
import { test } from "node:test";
import assert from "node:assert/strict";

let CardClass;
// Just enough DOM for the module to load and render outside a browser.
globalThis.HTMLElement = class {};
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

function renderCard({
  glucose, delta, forecast, showChart = false, last = "2026-01-01T11:30:00Z", now = "2026-01-01T12:05:00Z",
}) {
  const card = new CardClass();
  card.setConfig({
    show_chart: showChart,
    show_footer: true,
    show_clock: false,
    night_mode: false,
    stale_minutes: 15,
  });
  const names = ["card", "wrap", "chart", "clock", "name", "age", "value", "arrow",
    "delta", "bar", "fill", "footer", "missing"];
  const el = Object.fromEntries(names.map((name) => [name, element()]));
  card._root = {};
  card._el = el;
  card._ids = {
    glucose: "sensor.glucose",
    delta: delta ? "sensor.delta" : undefined,
    forecast: forecast ? "sensor.forecast" : undefined,
    last: "sensor.last",
  };
  card._hass = {
    states: {
      "sensor.glucose": glucose,
      "sensor.last": { state: last, attributes: {} },
      ...(delta ? { "sensor.delta": delta } : {}),
      ...(forecast ? { "sensor.forecast": forecast } : {}),
    },
  };

  const realNow = Date.now;
  Date.now = () => new Date(now).getTime();
  try {
    card._render();
  } finally {
    Date.now = realNow;
  }
  return el;
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

test("current reading uses Nightscout display limits on canonical mg/dL in both units", () => {
  // Nightscout 15.0.6, lib/client/index.js#updateCurrentSGV: LOW for [39, 40), HIGH above 400.
  const cases = [
    { native: 38.999, mgdl: "39", mmol: "2.2", expected: ["39", "2.2"] },
    { native: 39, mgdl: "39", mmol: "2.2", expected: ["LOW", "LOW"] },
    { native: 39.999, mgdl: "40", mmol: "2.2", expected: ["LOW", "LOW"] },
    { native: 40, mgdl: "40", mmol: "2.2", expected: ["40", "2.2"] },
    { native: 400, mgdl: "400", mmol: "22.2", expected: ["400", "22.2"] },
    { native: 400.001, mgdl: "400", mmol: "22.2", expected: ["HIGH", "HIGH"] },
    { native: 401, mgdl: "401", mmol: "22.3", expected: ["HIGH", "HIGH"] },
  ];
  const actual = cases.map(({ native, mgdl, mmol }) => [
    renderCard({ glucose: glucose(mgdl, "mg/dL", native) }).value.textContent,
    renderCard({ glucose: glucose(mmol, "mmol/L", native) }).value.textContent,
  ]);
  assert.deepEqual(actual, cases.map(({ expected }) => expected));
});

test("current reading uses the same limits for older entities without canonical mg/dL", () => {
  const cases = [
    [39, "LOW", "LOW"],
    [40, "40", "2.2"],
    [400, "400", "22.2"],
    [401, "HIGH", "HIGH"],
  ];
  const last = "2026-01-01T12:00:00Z";
  const actual = cases.map(([mgdl]) => [
    renderCard({ glucose: sensor(mgdl, "mg/dL"), last }).value.textContent,
    renderCard({ glucose: sensor(mgdl / 18.016, "mmol/L"), last }).value.textContent,
  ]);
  assert.deepEqual(actual, cases.map(([, mgdl, mmol]) => [mgdl, mmol]));
});

test("display limits leave the chart's numeric reading and original timestamp intact", (t) => {
  const drawChart = t.mock.method(CardClass.prototype, "_drawChart");
  const sourceTime = "2026-01-01T12:01:00Z";
  const low = renderCard({ glucose: glucose("39", "mg/dL", 39, sourceTime), showChart: true });
  const high = renderCard({ glucose: glucose("22.3", "mmol/L", 401, sourceTime), showChart: true });
  assert.deepEqual(drawChart.mock.calls.map(({ arguments: [, , current] }) => current), [
    [Date.parse(sourceTime), 39],
    [Date.parse(sourceTime), 401],
  ]);
  assert.match(low.chart.innerHTML, /<path d="M[\d.]+,400\.0"/);
  assert.match(high.chart.innerHTML, /<path d="M[\d.]+,0\.0"/);
  assert.deepEqual([low.value.textContent, high.value.textContent], ["LOW", "HIGH"]);
  assert.match(low.wrap.attributes["aria-label"], /^Glucose LOW,/);
  assert.match(high.wrap.attributes["aria-label"], /^Glucose HIGH,/);
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
  assert.equal(logic.formatDelta(6, false), "+6");
  assert.equal(logic.formatDelta(-18, false), "−18");
  assert.equal(logic.formatDelta(0.4, false), "±0");
  assert.equal(logic.formatDelta(0.33, true), "+0.3");
  assert.equal(logic.formatDelta(-1.05, true), "−1.0");
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
  assert.deepEqual(logic.thresholds({ low: 70, high: "200", urgent_high: -1 }),
    { urgent_low: 55, low: 70, high: 200, urgent_high: 260 });
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
