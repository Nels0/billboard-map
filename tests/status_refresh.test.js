"use strict";
/* Tests for the live status refresh loop in site/index.html.
 *
 * The page is one file with an inline <script> and no build step, so rather
 * than restructure it into modules the suite slices the refresh functions out
 * of the HTML and runs them against stubs. That keeps the shipped page exactly
 * as it is; the cost is the two markers below, which must keep matching.
 *
 * Run with: make test   (or: node --test tests/)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const PAGE = path.join(__dirname, "..", "site", "index.html");
const START = "  function fetchStates() {";
const END = "  function post(body) {";

function source() {
  const html = fs.readFileSync(PAGE, "utf8");
  const a = html.indexOf(START);
  const b = html.indexOf(END);
  assert.ok(a >= 0 && b > a,
    "could not find the refresh block in site/index.html — update START/END here if it moved");
  return html.slice(a, b);
}

const OK = (state) => ({
  ok: true,
  states: { a1: { state, updated: "2026-08-30T00:00:00Z", by: "Nelson", note: "" } }
});

// A fresh page per test: stubbed DOM, scripted fetch, and timers we fire by hand.
function page() {
  const env = {
    sync: { cfg: { endpoint: "https://example/exec", token: "t" }, live: false,
            error: "", at: 0, fetching: false, fails: 0, timer: null, changed: [] },
    POLL_MS: 120000, STALE_MS: 60000, RETRY_BASE_MS: 5000,
    RETRY_MAX_MS: 60000, FETCH_TIMEOUT_MS: 12000,
    pending: {},
    byId: { a1: { properties: { id: "a1", state: "", state_updated: "",
                                state_by: "", state_note: "" } } },
    document: { hidden: false },
    window: { AbortController }
  };

  const log = [];
  env.render = () => log.push("render");
  env.refreshOne = (id) => log.push("refreshOne:" + id);
  env.toast = (m) => log.push("toast:" + m);
  env.updateStatusLine = () => log.push("statusline");

  // Timers never fire on their own; fire(ms) runs the one waiting on that delay.
  const timers = [];
  env.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  env.clearTimeout = () => {};

  // Queue of scripted responses: a JSON body, "fail", or "hang".
  const queue = [];
  let calls = 0;
  env.fetch = (url, opts) => {
    calls++;
    const step = queue.shift();
    assert.ok(step !== undefined, "unexpected fetch: nothing scripted");
    if (step === "hang") {
      return new Promise((_, reject) => opts.signal.addEventListener("abort", () => {
        const e = new Error("aborted"); e.name = "AbortError"; reject(e);
      }));
    }
    if (step === "fail") return Promise.reject(new Error("network"));
    return Promise.resolve({ ok: true, json: () => Promise.resolve(step) });
  };

  const names = Object.keys(env);
  const factory = new Function(...names, source() +
    "\nreturn { fetchStates, refreshStates, maybeRefresh, scheduleRefresh, retryDelay };");
  const api = factory(...names.map((n) => env[n]));

  return Object.assign(api, {
    sync: env.sync, pending: env.pending, byId: env.byId, document: env.document,
    log, timers,
    delays: () => timers.filter((t) => t.ms !== env.FETCH_TIMEOUT_MS).map((t) => t.ms),
    fire: (ms) => {
      const t = timers.find((x) => x.ms === ms);
      assert.ok(t, "no timer scheduled at " + ms + "ms");
      t.fn();
    },
    calls: () => calls,
    respond: (...steps) => queue.push(...steps)
  });
}

test("first load paints the map and goes live", async () => {
  const p = page();
  p.respond(OK("up"));
  assert.equal(await p.refreshStates("initial"), true);
  assert.ok(p.log.includes("render"), "the first load does a full render");
  assert.equal(p.byId.a1.properties.state, "up");
  assert.ok(p.sync.live && p.sync.at > 0);
  assert.deepEqual(p.delays(), [120000], "poll is armed after a good read");
});

test("a poll that changes nothing repaints nothing", async () => {
  const p = page();
  p.respond(OK("up"), OK("up"));
  await p.refreshStates("initial");
  p.log.length = 0;
  await p.refreshStates("timer");
  assert.ok(!p.log.some((l) => l === "render" || l.startsWith("refreshOne")),
    "an unchanged poll must not disturb an open card");
});

test("a poll that changes one record repaints only that one", async () => {
  const p = page();
  p.respond(OK("up"), OK("down"));
  await p.refreshStates("initial");
  p.log.length = 0;
  await p.refreshStates("timer");
  assert.ok(p.log.includes("refreshOne:a1"));
  assert.ok(!p.log.includes("render"), "a full render would close the open card");
});

test("a poll does not clobber a write still in flight", async () => {
  const p = page();
  p.respond(OK("up"));
  await p.refreshStates("initial");
  // Optimistic local value, as setState leaves it while the POST is running.
  p.pending.a1 = true;
  p.byId.a1.properties.state = "down";
  p.respond(OK("up"));
  await p.refreshStates("timer");
  assert.equal(p.byId.a1.properties.state, "down");
});

test("failures back off 5/10/20/40/60s and warn once", async () => {
  const p = page();
  p.respond(OK("up"));
  await p.refreshStates("initial");
  p.log.length = 0;
  p.timers.length = 0;
  for (let i = 0; i < 6; i++) { p.respond("fail"); await p.refreshStates("timer"); }
  assert.deepEqual(p.delays(), [5000, 10000, 20000, 40000, 60000, 60000]);
  assert.equal(p.log.filter((l) => l.startsWith("toast:Lost contact")).length, 1,
    "one warning per outage, not one per attempt");
});

test("recovery clears the failure count and says so", async () => {
  const p = page();
  p.respond(OK("up"), "fail", OK("down"));
  await p.refreshStates("initial");
  await p.refreshStates("timer");
  p.log.length = 0;
  await p.refreshStates("timer");
  assert.ok(p.log.some((l) => l.startsWith("toast:Back in contact")));
  assert.equal(p.sync.fails, 0);
  assert.ok(p.sync.live);
});

test("a wake-up refetches only once the reading is stale", async () => {
  const p = page();
  p.respond(OK("up"));
  await p.refreshStates("initial");
  const before = p.calls();
  p.maybeRefresh("visible");
  assert.equal(p.calls(), before, "a fresh reading is left alone");

  p.sync.at = Date.now() - 61000;   // just past STALE_MS
  p.respond(OK("up"));
  p.maybeRefresh("visible");
  assert.equal(p.calls(), before + 1);
});

test("only one request is ever in flight", async () => {
  const p = page();
  p.respond("hang");
  p.refreshStates("initial");
  p.maybeRefresh("interaction");
  p.refreshStates("manual");
  assert.equal(p.calls(), 1);
  p.fire(12000);                    // let the abort land so nothing dangles
});

test("a hidden page skips the request but keeps its timer", async () => {
  const p = page();
  p.respond(OK("up"));
  await p.refreshStates("initial");
  p.document.hidden = true;
  p.timers.length = 0;
  const before = p.calls();
  await p.refreshStates("timer");
  assert.equal(p.calls(), before, "no point spending a request on a hidden page");
  assert.deepEqual(p.delays(), [120000], "but it must stay armed for the wake-up");
});

// The bug this whole loop exists for: a request that never settles used to pin
// the in-flight flag and stop every later retry.
test("a hung request times out and schedules a retry", async () => {
  const p = page();
  p.respond("hang");
  const done = p.refreshStates("initial");
  p.fire(12000);
  assert.equal(await done, false);
  assert.equal(p.sync.error, "timed out");
  assert.equal(p.sync.fetching, false);
  assert.deepEqual(p.delays(), [5000], "and the loop carries on");
});
