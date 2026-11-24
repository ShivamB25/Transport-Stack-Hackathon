const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function response(body, ok = true) {
  return { ok, status: ok ? 200 : 400, json: async () => body };
}

function journey(label) {
  return {
    status: "ok",
    mode: "live",
    feed_age_seconds: 5,
    feed_summary: { fresh_vehicle_count: 100 },
    selected_rung: "4",
    depart: "2026-11-24T08:30:00+05:30",
    resolved_from: { stop_name: label, stop_id: "from", routing_lat: 28.6, routing_lng: 77.2 },
    resolved_to: { stop_name: "Destination", stop_id: "to", routing_lat: 28.5, routing_lng: 77.25 },
    options: [{
      legs: [],
      total_duration_s: 1800,
      total_window_p10_s: 1700,
      total_window_p90_s: 1900,
      quieter_hour_typical_s: 1500
    }]
  };
}

function abstention() {
  return {
    status: "insufficient_data",
    mode: "live",
    feed_age_seconds: 61,
    feed_summary: { paused: true },
    options: [],
    abstention: {
      code: "stale_feed",
      reason: "GPS feed is stale",
      feed_age_seconds: 61,
      threshold_seconds: 45
    }
  };
}

function demo() {
  const stage = {
    stage_id: "initial",
    title: "Initial",
    status_badge: "RECOMMENDED",
    headline: "Transfer",
    explanation: "Timetable",
    replay_time_ist: "17:11:45",
    observed_delay_s: 165,
    remaining_buffer_s: 1292,
    recommended_option: "transfer",
    advice_flipped: false,
    outcome_revealed: false,
    bus_lat: 28.49,
    bus_lng: 77.30,
    bus_stop_name: "Origin",
    transfer_option: {
      label: "Transfer", recommended: true, estimated_duration_s: 3600,
      window_p10_s: 3500, window_p90_s: 3700, scheduled_duration_s: 3500, miss_probability: 0.1
    },
    direct_option: {
      label: "Direct", recommended: false, estimated_duration_s: 3700,
      window_p10_s: 3600, window_p90_s: 3800, scheduled_duration_s: 3700, miss_probability: 0
    }
  };
  return {
    status: "ok",
    fixture_day: "2026-10-22",
    run_id: "run",
    vehicle_id: "vehicle",
    origin: { name: "Origin", lat: 28.49, lng: 77.30 },
    destination: { name: "Destination", lat: 28.54, lng: 77.20 },
    transfer_stop: {
      stop_name: "Transfer", lat: 28.51, lng: 77.22,
      connecting_route: "D-7702UP", connecting_observed_depart: "2026-10-22T18:04:36+05:30"
    },
    route_name: "717AUP",
    geometries: { direct_legs: [], transfer_legs: [] },
    stages: [stage]
  };
}

function loadUi(fetchImpl) {
  const elements = new Map();
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const listeners = {};
    const classes = new Set(["abstention-panel", "replay-demo-panel", "results-summary", "error-panel"].includes(id) ? ["hidden"] : []);
    const el = {
      id,
      value: id === "origin-input" ? "Origin" : id === "destination-input" ? "Destination" : id === "depart-input" ? "2026-11-24T08:30:00+05:30" : id === "mode-select" ? "live" : "",
      textContent: "",
      innerHTML: "",
      disabled: false,
      dataset: {},
      style: {},
      children: [],
      listeners,
      classList: {
        add: (...names) => names.forEach((name) => classes.add(name)),
        remove: (...names) => names.forEach((name) => classes.delete(name)),
        contains: (name) => classes.has(name)
      },
      addEventListener(type, handler) { (listeners[type] ||= []).push(handler); },
      appendChild(child) { this.children.push(child); },
      setAttribute() {}
    };
    elements.set(id, el);
    return el;
  }
  const layer = {
    addTo() { return this; },
    clearLayers() {},
    getLayers() { return []; },
    getBounds() { return { isValid: () => false }; }
  };
  const leafletObject = {
    addTo() { return this; },
    bindTooltip() { return this; }
  };
  const sandbox = {
    console,
    URLSearchParams,
    Date,
    Math,
    Number,
    String,
    Promise,
    setInterval,
    clearInterval,
    document: {
      getElementById: element,
      createElement: (tag) => element(`${tag}-${Math.random()}`)
    },
    fetch: fetchImpl,
    L: {
      map: () => ({ setView: () => ({}) }),
      tileLayer: () => ({ addTo: () => ({}) }),
      featureGroup: () => layer,
      polyline: () => Object.create(leafletObject),
      circleMarker: () => Object.create(leafletObject),
      marker: () => Object.create(leafletObject),
      divIcon: (options) => options
    }
  };
  const html = fs.readFileSync(path.join(__dirname, "../../src/delhi_transit/api/static/index.html"), "utf8");
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  vm.runInNewContext(scripts.at(-1)[1], sandbox);
  return { elements };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

test("an older journey response cannot overwrite a newer abstention", async () => {
  const requests = [];
  const { elements } = loadUi(async (url) => {
    if (url.startsWith("/journeys")) {
      const request = deferred();
      requests.push(request);
      return request.promise;
    }
    return response({ is_fresh: true, current_feed_age_seconds: 4, state: {} });
  });
  const submit = elements.get("journey-form").listeners.submit[0];
  submit({ preventDefault() {} });
  submit({ preventDefault() {} });
  assert.equal(requests.length, 2);

  requests[1].resolve(response(abstention()));
  await flush();
  assert.equal(elements.get("abstention-panel").classList.contains("hidden"), false);
  assert.equal(elements.get("options-list").children.length, 0);

  requests[0].resolve(response(journey("Older journey")));
  await flush();
  assert.equal(elements.get("abstention-panel").classList.contains("hidden"), false);
  assert.equal(elements.get("options-list").children.length, 0);
  assert.equal(elements.get("results-summary").classList.contains("hidden"), true);
  assert.match(elements.get("feed-age-text").textContent, /Stale GPS/);
});

test("a late demo response cannot override a newer journey", async () => {
  const requests = [];
  const { elements } = loadUi(async (url) => {
    if (url.startsWith("/journeys") || url.startsWith("/replay/demo")) {
      const request = deferred();
      request.url = url;
      requests.push(request);
      return request.promise;
    }
    return response({ is_fresh: true, current_feed_age_seconds: 4, state: {} });
  });
  elements.get("replay-demo-btn").listeners.click[0]();
  elements.get("journey-form").listeners.submit[0]({ preventDefault() {} });
  assert.equal(requests.length, 2);
  assert.equal(elements.get("replay-demo-btn").disabled, false);
  assert.equal(elements.get("replay-demo-btn").textContent, "Replay demo");
  assert.equal(elements.get("plan-btn").disabled, true);

  requests[1].resolve(response(journey("Newer journey")));
  await flush();
  assert.equal(elements.get("options-list").children.length, 1);

  requests[0].resolve(response(demo()));
  await flush();
  assert.equal(elements.get("replay-demo-panel").classList.contains("hidden"), true);
  assert.equal(elements.get("options-list").children.length, 1);
  assert.match(elements.get("results-summary").innerHTML, /Newer journey/);
  assert.equal(elements.get("plan-btn").disabled, false);
});

test("a newer demo releases the superseded journey's loading button", async () => {
  const requests = [];
  const { elements } = loadUi(async (url) => {
    if (url.startsWith("/journeys") || url.startsWith("/replay/demo")) {
      const request = deferred();
      requests.push(request);
      return request.promise;
    }
    return response({ is_fresh: true, current_feed_age_seconds: 4, state: {} });
  });
  elements.get("journey-form").listeners.submit[0]({ preventDefault() {} });
  elements.get("replay-demo-btn").listeners.click[0]();
  assert.equal(elements.get("plan-btn").disabled, false);
  assert.equal(elements.get("plan-btn").textContent, "Plan journey");
  assert.equal(elements.get("replay-demo-btn").disabled, true);

  requests[0].resolve(response(journey("Older journey")));
  await flush();
  assert.equal(elements.get("replay-demo-btn").disabled, true);
  assert.equal(elements.get("options-list").children.length, 0);

  requests[1].resolve(response(demo()));
  await flush();
  assert.equal(elements.get("replay-demo-btn").disabled, false);
  assert.equal(elements.get("replay-demo-panel").classList.contains("hidden"), false);
});
