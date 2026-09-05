"use strict";
/* Tests for the two-tier passphrase gate in site/index.html.
 *
 * Same trick as status_refresh.test.js: the page is one file with an inline
 * <script> and no build step, so the suite slices the functions it needs out
 * of the HTML and runs them against stubs rather than restructuring the page.
 * The markers below must keep matching; the test fails loudly if they do not.
 *
 * Run with: make test   (or: node --test tests/)
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const PAGE = path.join(__dirname, "..", "site", "index.html");

// Three slices, all taken from the shipped page: the real tier order and the
// `tier` it records, the unlock path that walks it, and the contact card that
// reads it back. Taking TIER_ORDER from the page rather than stubbing it is
// the point - a build that stopped offering the lite tier would still pass
// against a stub.
const SLICES = [
  ["  var TIER_ORDER = ", "  var byId = {};"],
  ["  async function attempt(pass, remember) {", '  form.addEventListener("submit"'],
  ["  function esc(s) {", "  function popup(f) {"]
];

function source() {
  const html = fs.readFileSync(PAGE, "utf8");
  return SLICES.map(function (pair) {
    const a = html.indexOf(pair[0]);
    const b = html.indexOf(pair[1]);
    assert.ok(a >= 0 && b > a,
      "could not find " + JSON.stringify(pair[0]) +
      " in site/index.html — update the markers here if it moved");
    return html.slice(a, b);
  }).join("\n");
}

// Envelopes are stubs: decrypt() succeeds only when the passphrase matches the
// one recorded on the envelope, which is what real AES-GCM does for us.
const env = (pass, payload) => ({ pass: pass, payload: payload });

function page(file) {
  const ctx = {
    data: null,
    envelope: Promise.resolve(file),
    gate: { style: {} },
    document: { getElementById: () => ({ classList: { add: () => {} } }) }
  };

  const log = [];
  ctx.decrypt = (e, pass) => {
    log.push("decrypt");
    return e.pass === pass
      ? Promise.resolve(e.payload)
      : Promise.reject(new Error("OperationError"));
  };
  ctx.store = (pass, on) => log.push("store:" + pass + ":" + on);
  ctx.start = () => log.push("start");

  const names = Object.keys(ctx);
  const factory = new Function(...names, source() +
    "\nreturn { attempt, contactBlock, tier: function () { return tier; }," +
    " data: function () { return data; } };");
  const api = factory(...names.map((n) => ctx[n]));
  return Object.assign(api, { log, decrypts: () => log.filter((l) => l === "decrypt").length });
}

const TWO_TIER = {
  v: 2,
  tiers: {
    full: env("openall", { who: "full" }),
    lite: env("deliveronly", { who: "lite" })
  }
};

test("the full passphrase opens the full tier", async () => {
  const p = page(TWO_TIER);
  await p.attempt("openall", true);
  assert.equal(p.tier(), "full");
  assert.deepEqual(p.data(), { who: "full" });
  assert.equal(p.decrypts(), 1, "the full tier is tried first, so once is enough");
  assert.ok(p.log.includes("store:openall:true") && p.log.includes("start"));
});

test("the lite passphrase falls through to the lite tier", async () => {
  const p = page(TWO_TIER);
  await p.attempt("deliveronly", false);
  assert.equal(p.tier(), "lite");
  assert.deepEqual(p.data(), { who: "lite" }, "the lite payload is a different one");
  assert.equal(p.decrypts(), 2, "full is attempted and fails before lite is tried");
});

test("a wrong passphrase opens nothing and leaves the page locked", async () => {
  const p = page(TWO_TIER);
  await assert.rejects(() => p.attempt("guess", true));
  assert.equal(p.data(), null);
  assert.ok(!p.log.some((l) => l === "start" || l.startsWith("store:")),
    "nothing is remembered and the app never starts");
});

test("a v1 file (one bare envelope) still opens", async () => {
  const p = page(env("openall", { who: "v1" }));
  await p.attempt("openall", true);
  assert.equal(p.tier(), "full");
  assert.deepEqual(p.data(), { who: "v1" });
});

test("a build with no lite tier rejects the lite passphrase", async () => {
  const p = page({ v: 2, tiers: { full: env("openall", { who: "full" }) } });
  await assert.rejects(() => p.attempt("deliveronly", true));
  assert.equal(p.decrypts(), 1, "an absent tier is skipped, not decrypted");
});

test("the empty-contact message says which case it is", async () => {
  const p = page(TWO_TIER);
  const blank = { name: "", phone: "", email: "" };

  await p.attempt("openall", true);
  assert.match(p.contactBlock(blank), /No contact details on this response/,
    "on the full tier a blank card really is a blank response");

  await p.attempt("deliveronly", true);
  assert.match(p.contactBlock(blank), /not included with this passphrase/,
    "on the lite tier the details exist; this reader just cannot see them");
});

test("contact details still render when the tier carries them", async () => {
  const p = page(TWO_TIER);
  await p.attempt("openall", true);
  const html = p.contactBlock({ name: "A Host", phone: "021 555 1234", email: "a@b.nz" });
  assert.match(html, /A Host/);
  assert.match(html, /tel:0215551234/, "tel: strips the spaces, the label keeps them");
  assert.match(html, /mailto:a@b\.nz/);
});
