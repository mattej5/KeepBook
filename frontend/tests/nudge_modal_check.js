/*
 * DOM-free regression check for the nudge modal's close semantics.
 *
 * THE regression this guards: closing the modal must REMOVE the overlay node
 * from the DOM. The first ship set el.hidden instead; the author rule
 * .nudge-overlay{display:flex} overrides the UA [hidden]{display:none}, so the
 * "closed" overlay stayed a full-viewport z-index-60 click shield and froze
 * the whole app (real-browser find). Detachment is a strictly stronger,
 * CSS-independent invariant, so it is checkable without a browser.
 *
 * Run:            node frontend/tests/nudge_modal_check.js
 * Against a file: node frontend/tests/nudge_modal_check.js <path-to-app.js>
 * (pointing it at the pre-fix app.js makes it FAIL — that is the teeth check)
 *
 * What only a real browser can verify: computed display/z-index, the fade-in,
 * actual click-through behavior, clipboard, focus. This harness verifies the
 * open/close/reopen/late-promise state machine and node attachment only.
 */
"use strict";

var fs = require("fs");
var path = require("path");

var appPath = process.argv[2] || path.join(__dirname, "..", "js", "app.js");
var src = fs.readFileSync(appPath, "utf8");

var START = "/* ---------- Draft reminder (nudge) modal ---------- */";
var END = "// Build a real mailto:";
var a = src.indexOf(START);
var b = src.indexOf(END);
if (a < 0 || b < 0 || b <= a) {
  console.error("FAIL: could not locate the nudge modal section markers in " + appPath);
  process.exit(1);
}
var section = src.slice(a, b);

/* ------------------------- minimal fake DOM ------------------------------ */
function makeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    id: "",
    className: "",
    hidden: false,
    innerHTML: "",
    parentNode: null,
    children: [],
    appendChild: function (c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = this;
      this.children.push(c);
      return c;
    },
    removeChild: function (c) {
      var i = this.children.indexOf(c);
      if (i < 0) throw new Error("removeChild: not a child");
      this.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    addEventListener: function () {},
    removeEventListener: function () {},
  };
}

var body = makeEl("body");

function findById(root, id) {
  if (root.id === id) return root;
  for (var i = 0; i < root.children.length; i++) {
    var hit = findById(root.children[i], id);
    if (hit) return hit;
  }
  return null;
}

var keydownListeners = 0;
var doc = {
  body: body,
  createElement: makeEl,
  // Like the real one: only finds ATTACHED nodes — detached overlays are gone.
  getElementById: function (id) { return findById(body, id); },
  addEventListener: function (type) { if (type === "keydown") keydownListeners++; },
  removeEventListener: function (type) { if (type === "keydown") keydownListeners--; },
  execCommand: function () { return true; },
};

function $(id) { return doc.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
  });
}

var pendingNudge = null; // {resolve, reject} — the harness controls settlement
var api = {
  getNudge: function () {
    return new Promise(function (resolve, reject) {
      pendingNudge = { resolve: resolve, reject: reject };
    });
  },
};

/* ------------------- evaluate the section, capture handles --------------- */
var factory = new Function(
  "document", "api", "$", "esc", "navigator",
  section +
    "\nreturn { open: openNudgeModal, close: closeNudgeModal, paint: paintNudgeModal," +
    " state: function () { return nudgeState; } };"
);
var modal = factory(doc, api, $, esc, {});

/* ------------------------------ assertions ------------------------------- */
var failures = 0;
function check(name, cond) {
  if (cond) { console.log("ok   - " + name); }
  else { failures++; console.error("FAIL - " + name); }
}
function overlayAttached() { return doc.getElementById("nudge-overlay") !== null; }

function run() {
  // 1. open -> overlay attached, esc handler bound
  modal.open("client_a", "Client A");
  check("open attaches the overlay", overlayAttached());
  check("open registers the Escape handler", keydownListeners === 1);

  var settle = pendingNudge; pendingNudge = null;
  settle.resolve({ client_id: "client_a", missing: ["W-2"], draft: "Hi Client A,\n- W-2", generated_by: "model" });

  return Promise.resolve().then(function () {
    check("draft paint keeps the overlay attached", overlayAttached());

    // 2. close -> THE regression: node must be GONE from the DOM, not hidden
    modal.close();
    check("close DETACHES the overlay from the DOM (the click-shield regression)", !overlayAttached());
    check("close removes the Escape handler", keydownListeners === 0);
    check("close resets state.open", modal.state().open === false);

    // 3. reopen works with a fresh node
    modal.open("client_b", "Client B");
    check("reopen attaches a fresh overlay", overlayAttached());
    var settle2 = pendingNudge; pendingNudge = null;

    // 4. close while the fetch is still in flight, then let it settle late
    modal.close();
    check("close-while-loading detaches the overlay", !overlayAttached());
    settle2.resolve({ client_id: "client_b", missing: [], draft: null });
    return Promise.resolve();
  }).then(function () {
    check("late-settling fetch does not resurrect the overlay", !overlayAttached());

    // 5. failed fetch paints an error state (still open, closable), then close
    modal.open("client_c", "Client C");
    var settle3 = pendingNudge; pendingNudge = null;
    settle3.reject(new Error("boom"));
    return Promise.resolve().then(function () {}); // let the .catch run
  }).then(function () {
    check("failed fetch keeps the overlay open (error state shown)", overlayAttached());
    modal.close();
    check("close after a failed fetch detaches the overlay", !overlayAttached());

    // 6. a stray paint while closed must not create a node
    modal.paint();
    check("painting while closed never creates an overlay", !overlayAttached());

    console.log(failures ? "\n" + failures + " FAILURE(S)" : "\nALL CHECKS PASSED");
    process.exit(failures ? 1 : 0);
  });
}

run().catch(function (e) {
  console.error("FAIL - harness error: " + (e && e.stack || e));
  process.exit(1);
});
