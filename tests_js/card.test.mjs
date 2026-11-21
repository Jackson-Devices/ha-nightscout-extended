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

  async function render({
    entity = GLUCOSE, glucose = "152", last = timeAt("11:58:00"), forecast, history = [],
    revision, connected = true, callWS,
  } = {}) {
    const base = entity.replace(/_glucose$/, "");
    card.hass = {
      connected,
      states: {
        [entity]: { state: glucose, attributes: { unit_of_measurement: "mg/dL", sgv_history_revision: revision } },
        [`${base}_last_reading`]: { state: last },
        [`${base}_forecast_in_30_minutes`]: forecast,
      },
      callWS: callWS ?? (async (message) => {
        if (message.type === "call_service") throw { code: "not_found" };
        return { [entity]: history };
      }),
    };
    await new Promise(setImmediate);
    const paths = [...card._el.chart.innerHTML.matchAll(/<path d="([^"]*)"/g)];
    return {
      reading: card._el.value.textContent,
      footer: card._el.footer.children.map((node) => node.textContent),
      historyPath: paths[0][1],
      forecastPath: paths[1][1],
    };
  }
  render.setConfig = (config) => card.setConfig(config);
  return render;
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

const reading = (id, clock, sgv) => ({ id, date: Date.parse(timeAt(clock)), sgv });
const response = (readings, status = "complete", missing_ranges = []) => ({
  context: {}, response: { readings, unit: "mg/dL", status, missing_ranges, as_of: timeAt("11:59:45") },
});
const pathXs = (path) => [...path.matchAll(/[ML]([\d.]+),/g)].map(([, x]) => Number(x));

test("source history draws all dated readings and refetches once per new revision", async (t) => {
  const render = renderFixture(t);
  let readings = [
    reading("c", "11:50:00", 155), reading("b", "11:40:00", 152),
    reading("b2", "11:40:00", 153), reading("a", "11:30:00", 150),
  ];
  const calls = [];
  const callWS = async (message) => {
    calls.push(message);
    return response(readings);
  };
  const first = await render({ revision: "rev-1", last: timeAt("11:50:00"), callWS });
  assert.deepEqual(calls, [{
    type: "call_service", domain: "nightscout_extended", service: "get_glucose_history", return_response: true,
    service_data: { entity_id: GLUCOSE, start: timeAt("11:00:00.000"), end: timeAt("12:00:00.000") },
  }]);
  assert.deepEqual(pathXs(first.historyPath), [333.3, 444.4, 444.4, 555.6]);
  assert.deepEqual(commands(first.historyPath), ["M", "L", "L", "L"]);

  readings = [...readings, reading("late", "11:35:00", 151)];
  const late = await render({ revision: "rev-2", last: timeAt("11:50:00"), callWS });
  assert.deepEqual(pathXs(late.historyPath), [333.3, 388.9, 444.4, 444.4, 555.6]);

  readings = [...readings, reading("next", "11:59:00", 157)];
  const latest = await render({ revision: "rev-3", last: timeAt("11:59:00"), callWS });
  assert.equal(pathXs(latest.historyPath).at(-1), 655.6);
  // HA may publish the last-reading entity separately from its history revision.
  await render({ revision: "rev-3", last: timeAt("11:59:30"), callWS });
  assert.equal(calls.length, 3);
});

test("partial and offline source coverage stays explicit and a complete empty range clears the chart", async (t) => {
  const render = renderFixture(t);
  const calls = [];
  let result = response([reading("one", "11:30:00", 143), reading("two", "11:40:00", 145)], "partial", [
    { start: Date.parse(timeAt("11:32:00")), end: Date.parse(timeAt("11:38:00")) },
  ]);
  const callWS = async (message) => {
    calls.push(message);
    return result;
  };
  const partial = await render({ revision: "same", callWS });
  assert.deepEqual(partial.footer, ["History partial"]);
  assert.deepEqual(commands(partial.historyPath), ["M", "L", "M", "L"]);
  assert.deepEqual(pathXs(partial.historyPath), [333.3, 333.3, 444.4, 444.4]);

  const offline = await render({ revision: "same", connected: false, callWS });
  assert.deepEqual(offline.footer, ["History cached"]);
  assert.equal(offline.historyPath, partial.historyPath);
  assert.equal(calls.length, 1);

  result = response([]);
  const restored = await render({ revision: "same", callWS });
  assert.deepEqual(restored.footer, []);
  assert.equal(restored.historyPath, "");
  assert.equal(calls.length, 2);
  assert.ok(calls.every((message) => message.type === "call_service"));
});

test("permission and source errors do not read Recorder as a substitute", async (t) => {
  const render = renderFixture(t);
  const calls = [];
  const callWS = async (message) => {
    calls.push(message.type);
    if (calls.length === 1) return response([reading("allowed", "11:30:00", 143)]);
    throw { code: "home_assistant_error", message: "Unauthorized" };
  };
  const before = await render({ revision: "allowed", callWS });
  assert.deepEqual(pathXs(before.historyPath), [333.3, 333.3]);
  const denied = await render({ revision: "revoked", callWS });
  assert.deepEqual(calls, ["call_service", "call_service"]);
  assert.deepEqual(denied.footer, ["History unavailable"]);
  assert.equal(denied.historyPath, "");
});

test("a superseded entity and range cannot replace the displayed SGV series", async (t) => {
  const render = renderFixture(t);
  const other = "sensor.other_glucose";
  let finishFirst;
  const first = new Promise((resolve) => { finishFirst = resolve; });
  const calls = [];
  const callWS = async (message) => {
    calls.push(message.service_data);
    return message.service_data.entity_id === GLUCOSE
      ? first : response([reading("other", "10:30:00", 140)]);
  };
  await render({ revision: "first", callWS });
  render.setConfig({ entity: other, hours_to_show: 2 });
  const visible = await render({ entity: other, revision: "second", callWS });
  assert.deepEqual(calls, [
    { entity_id: GLUCOSE, start: timeAt("11:00:00.000"), end: timeAt("12:00:00.000") },
    { entity_id: other, start: timeAt("10:00:00.000"), end: timeAt("12:00:00.000") },
  ]);
  assert.deepEqual(pathXs(visible.historyPath), [200, 200]);
  finishFirst(response([reading("old entity", "11:55:00", 180)]));
  const settled = await render({ entity: other, revision: "second", callWS });
  assert.equal(settled.historyPath, visible.historyPath);
  assert.equal(calls.length, 2);
});

test("card version matches manifest.json, so browsers reload it after an update", async () => {
  const { readFile } = await import("node:fs/promises");
  const { CARD_VERSION } = await import("../custom_components/nightscout_extended/www/nightscout-glucose-card.js");
  const manifest = JSON.parse(await readFile(new URL("../custom_components/nightscout_extended/manifest.json", import.meta.url)));
  assert.equal(CARD_VERSION, manifest.version);
});
