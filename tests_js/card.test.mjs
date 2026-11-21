// Tests for the glucose card's logic and rendered output. Run: node --test tests_js
import { test } from "node:test";
import assert from "node:assert/strict";

// Just enough DOM for the real card's rendering methods to run outside a browser.
function element() {
  return {
    style: { setProperty() {} },
    children: [],
    setAttribute() {},
    replaceChildren(...children) { this.children = children; },
  };
}

globalThis.HTMLElement = class {
  attachShadow() {
    const elements = new Map();
    return {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, element());
        return elements.get(selector);
      },
    };
  }
};
const registered = new Map();
globalThis.customElements = {
  get: (name) => registered.get(name),
  define: (name, constructor) => registered.set(name, constructor),
};
globalThis.window = globalThis;
globalThis.document = { createElement: element };

const { logic } = await import("../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
const T = { urgent_low: 55, low: 80, high: 180, urgent_high: 260 };

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

test("old, missing or non-numeric readings are stale", () => {
  assert.equal(logic.classify(120, 16, 15, T), "stale");
  assert.equal(logic.classify(NaN, 1, 15, T), "stale");
  assert.equal(logic.classify(120, NaN, 15, T), "stale");
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

const GLUCOSE = "sensor.test_glucose";
const timeAt = (clock) => `2026-01-08T${clock}Z`;
const prediction = (clock, mgdl) => ({ time: timeAt(clock), mgdl });
const recorded = (clock, s) => ({ lu: Date.parse(timeAt(clock)) / 1000, s });
const commands = (path) => path.match(/[ML]/g) || [];

function renderFixture(t, config = {}) {
  t.mock.method(Date, "now", () => Date.parse(timeAt("12:00:00")));
  const Card = registered.get("nightscout-glucose-card");
  const card = new Card();
  card.setConfig({ entity: GLUCOSE, hours_to_show: 1, night_mode: false, ...config });
  card._build();

  return async function render({ glucose = "152", last = timeAt("11:58:00"), forecast, history = [] } = {}) {
    card.hass = {
      states: {
        [GLUCOSE]: { state: glucose, attributes: { unit_of_measurement: "mg/dL" } },
        "sensor.test_last_reading": { state: last },
        "sensor.test_forecast_in_30_minutes": forecast,
      },
      callWS: async () => ({ [GLUCOSE]: history }),
    };
    await new Promise(setImmediate);
    const paths = [...card._el.chart.innerHTML.matchAll(/<path d="([^"]*)"/g)];
    return {
      reading: card._el.value.textContent,
      footer: card._el.footer.children.map((node) => node.textContent),
      historyPath: paths[0][1],
      forecastPath: paths[1][1],
    };
  };
}

test("an unknown 30-minute scalar leaves usable future forecast points visible", async (t) => {
  const render = renderFixture(t);
  const result = await render({
    last: timeAt("12:00:02"),
    forecast: { state: "unknown", attributes: {
      source_time: timeAt("11:55:00"),
      forecast_time: null,
      forecast: [
        prediction("12:10:00", 160),
        prediction("12:00:01", 145),
        prediction("11:58:00", 141),
        prediction("12:05:00", 155),
      ],
    } },
  });
  assert.deepEqual(result.footer, []);
  assert.deepEqual(commands(result.forecastPath), ["M", "L", "L"]);
  const xs = [...result.forecastPath.matchAll(/[ML]([\d.]+),/g)].map(([, x]) => Number(x));
  assert.ok(xs[0] < xs[1] && xs[1] < xs[2], "future curve must run forward from the measured point");
});

test("old v2 AR2 is hidden while current v3 glucose stays visible", async (t) => {
  const render = renderFixture(t);
  const result = await render({ forecast: { state: "169", attributes: {
    source_time: timeAt("11:41:00"),
    forecast_time: timeAt("12:11:02"),
    forecast: [prediction("12:11:02", 169)],
  } } });
  assert.equal(result.reading, "152");
  assert.deepEqual(result.footer, []);
  assert.equal(result.forecastPath, "");
});

test("footer expiry and explicit null times are distinct from absent legacy metadata", async (t) => {
  const render = renderFixture(t, { stale_minutes: 45 });
  const points = [prediction("11:59:00", 149), prediction("12:07:00", 169)];
  const legacy = await render({ forecast: { state: "169", attributes: { forecast: points } } });
  assert.deepEqual(legacy.footer, ["30 min ≈ 169"]);
  assert.deepEqual(commands(legacy.forecastPath), ["M", "L"]);

  for (const forecastTime of [timeAt("11:59:00"), null]) {
    const expired = await render({ forecast: { state: "169", attributes: {
      source_time: timeAt("11:29:00"), forecast_time: forecastTime, forecast: points,
    } } });
    assert.deepEqual(expired.footer, []);
    assert.deepEqual(commands(expired.forecastPath), ["M", "L"]);
  }

  const missingAnchor = await render({ forecast: { state: "169", attributes: {
    source_time: null, forecast_time: timeAt("12:07:00"), forecast: points,
  } } });
  assert.deepEqual(missingAnchor.footer, []);
  assert.equal(missingAnchor.forecastPath, "");
});

test("history keeps measured segments through dropout and does not bridge them on reconnect", async (t) => {
  const render = renderFixture(t, { stale_minutes: 8 });
  const history = [
    recorded("11:20:00", "151"), recorded("11:21:00", "152"), recorded("11:22:00", "unknown"),
    recorded("11:23:00", "154"), recorded("11:24:00", "156"), recorded("11:25:00", "unavailable"),
    recorded("11:26:00", "158"), recorded("11:27:00", "160"),
    recorded("11:36:00", "165"), recorded("11:37:00", "167"), recorded("11:38:00", "unavailable"),
  ];
  const forecast = { state: "169", attributes: {
    source_time: timeAt("11:58:00"),
    forecast_time: timeAt("12:28:00"),
    forecast: [prediction("12:28:00", 169)],
  } };
  const dropout = await render({ glucose: "unavailable", last: "unavailable", forecast, history });
  assert.equal(dropout.reading, "---");
  assert.deepEqual(dropout.footer, ["⚠ No recent reading"]);
  assert.equal(dropout.forecastPath, "");
  assert.deepEqual(commands(dropout.historyPath), ["M", "L", "M", "L", "M", "L", "M", "L"]);

  const recovered = await render({ forecast, history });
  assert.equal(recovered.reading, "152");
  assert.deepEqual(recovered.footer, ["30 min ≈ 169"]);
  assert.ok(recovered.historyPath.startsWith(dropout.historyPath));
  const current = recovered.historyPath.slice(dropout.historyPath.length).match(/^M([\d.]+,[\d.]+)L\1$/);
  assert.ok(current, "the one recovered reading is a separate visible point");
  assert.ok(recovered.forecastPath.startsWith(`M${current[1]}L`));
});

test("card version matches manifest.json, so browsers reload it after an update", async () => {
  const { readFile } = await import("node:fs/promises");
  const { CARD_VERSION } = await import("../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
  const manifest = JSON.parse(await readFile(new URL("../custom_components/nightscout_extended/manifest.json", import.meta.url)));
  assert.equal(CARD_VERSION, manifest.version);
});
