/*
 * KeepBook API layer — single source of network access.
 * One code path; mock vs. real is decided once, here.
 *
 * Contract: docs/API.md is the pinned source of truth for shapes + endpoints.
 * Mock mode (?mock=1) serves the same shapes from frontend/mock/*.json so the
 * whole UI runs with the backend absent and Wi-Fi off.
 *
 * Base URL (real mode): window.KEEPBOOK_API  ||  ?api=<url>  ||  same-origin.
 */
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  var MOCK = params.get("mock") === "1";
  var RESET = params.get("reset") === "1";

  function realBase() {
    if (window.KEEPBOOK_API) return String(window.KEEPBOOK_API).replace(/\/$/, "");
    var q = params.get("api");
    if (q) return q.replace(/\/$/, "");
    return ""; // same-origin
  }

  /* ------------------------------------------------------------------ *
   * REAL adapter — thin wrapper over fetch against docs/API.md.
   * ------------------------------------------------------------------ */
  var real = (function () {
    var base = realBase();
    function j(path, opts) {
      return fetch(base + path, opts).then(function (r) {
        if (!r.ok) throw new Error(path + " -> " + r.status);
        return r.json();
      });
    }
    return {
      getClients: function () { return j("/clients"); },
      getDocuments: function () { return j("/documents"); },
      getDocument: function (id) { return j("/documents/" + id); },
      getStats: function () { return j("/stats"); },
      getQueue: function () { return j("/queue"); },
      imageUrl: function (doc) { return base + "/documents/" + doc.id + "/image"; },
      intake: function (files) {
        var fd = new FormData();
        for (var i = 0; i < files.length; i++) fd.append("file", files[i]);
        return j("/intake", { method: "POST", body: fd });
      },
      confirm: function (id, payload) {
        return j("/documents/" + id + "/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
      }
    };
  })();

  /* ------------------------------------------------------------------ *
   * MOCK adapter — in-memory state seeded from fixtures, persisted to
   * localStorage so corrections survive a reload during the demo.
   * ------------------------------------------------------------------ */
  var mock = (function () {
    var STORE_KEY = "keepbook_mock_v3";
    var state = null;      // {clients, documents, uploads}
    var readyPromise = null;

    function loadFixtures() {
      return Promise.all([
        fetch("mock/clients.json").then(function (r) { return r.json(); }),
        fetch("mock/documents.json").then(function (r) { return r.json(); })
      ]).then(function (res) {
        return { clients: res[0], documents: res[1], uploads: [] };
      });
    }

    function persist() {
      try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) {}
    }

    function ready() {
      if (readyPromise) return readyPromise;
      if (RESET) { try { localStorage.removeItem(STORE_KEY); } catch (e) {} }
      var cached = null;
      try { cached = JSON.parse(localStorage.getItem(STORE_KEY)); } catch (e) {}
      if (cached && cached.documents && cached.clients) {
        state = cached;
        readyPromise = Promise.resolve(state);
      } else {
        readyPromise = loadFixtures().then(function (s) { state = s; persist(); return state; });
      }
      return readyPromise;
    }

    // Lazily materialize any dropped uploads whose processing time has elapsed.
    function tick() {
      var now = Date.now();
      var changed = false;
      state.uploads.forEach(function (u) {
        if (!u.done && now >= u.readyAt) {
          u.done = true;
          changed = true;
          state.documents.push({
            id: u.id,
            client_id: null,
            status: u.doc_type === "UNRECOGNIZED" ? "unrecognized" : "extracted",
            doc_type: u.doc_type,
            image_path: u.image_path || null,
            fields: u.fields || {}
          });
        }
      });
      if (changed) persist();
    }

    // Guess a plausible doc_type + starter fields from a dropped filename so
    // freshly-dropped files land in Review as real, editable extractions.
    function templateFor(name) {
      var n = (name || "").toLowerCase();
      if (n.indexOf("1099") >= 0 && n.indexOf("int") >= 0) {
        return { doc_type: "1099-INT", fields: {
          payer: { value: "", corrected: false },
          recipient: { value: "", corrected: false },
          recipient_tin: { value: "", corrected: false },
          box1_interest: { value: "", corrected: false },
          box4_fed_withheld: { value: "", corrected: false }
        }};
      }
      if (n.indexOf("1098") >= 0) {
        return { doc_type: "1098", fields: {
          lender: { value: "", corrected: false },
          borrower: { value: "", corrected: false },
          borrower_tin: { value: "", corrected: false },
          box1_mortgage_interest: { value: "", corrected: false }
        }};
      }
      if (n.indexOf("w2") >= 0 || n.indexOf("w-2") >= 0) {
        return { doc_type: "W-2", fields: {
          employer: { value: "", corrected: false },
          ein: { value: "", corrected: false },
          employee_name: { value: "", corrected: false },
          ssn: { value: "", corrected: false },
          box1_wages: { value: "", corrected: false },
          box2_fed_withheld: { value: "", corrected: false }
        }};
      }
      return { doc_type: "UNRECOGNIZED", fields: {} };
    }

    return {
      isMock: true,
      resetMock: function () { try { localStorage.removeItem(STORE_KEY); } catch (e) {} },

      getClients: function () { return ready().then(function () { return clone(state.clients); }); },
      getDocuments: function () { return ready().then(function () { tick(); return clone(state.documents); }); },
      getDocument: function (id) {
        return ready().then(function () {
          tick();
          var d = state.documents.filter(function (x) { return x.id === id; })[0];
          return d ? clone(d) : Promise.reject(new Error("no doc " + id));
        });
      },
      imageUrl: function (doc) { return doc && doc.image_path ? doc.image_path : ""; },

      getStats: function () {
        return ready().then(function () {
          tick();
          var extracted = 0, corrected = 0;
          state.documents.forEach(function (d) {
            if (d.status === "unrecognized") return;
            Object.keys(d.fields || {}).forEach(function (k) {
              extracted++;
              if (d.fields[k].corrected) corrected++;
            });
          });
          return {
            fields_extracted: extracted,
            fields_corrected: corrected,
            correction_rate: extracted ? +(corrected / extracted).toFixed(4) : 0
          };
        });
      },

      getQueue: function () {
        return ready().then(function () {
          tick();
          var pending = 0, processing = null;
          state.uploads.forEach(function (u) {
            if (!u.done) { pending++; if (!processing) processing = u.id; }
          });
          var done = state.uploads.filter(function (u) { return u.done; }).length;
          return { pending: pending, processing: processing, done: done };
        });
      },

      intake: function (files) {
        return ready().then(function () {
          var queued = [];
          var now = Date.now();
          var base = state.documents.length + state.uploads.length;
          for (var i = 0; i < files.length; i++) {
            var id = "doc_" + String(base + i + 1).padStart(3, "0");
            var t = templateFor(files[i].name);
            var upload = {
              id: id,
              name: files[i].name,
              doc_type: t.doc_type,
              fields: t.fields,
              image_path: null,
              readyAt: now + (i + 1) * 2200,  // ~2.2s/doc, sequential
              done: false
            };
            // Preview the dropped image if it is one, so Review can show it.
            if (files[i].type && files[i].type.indexOf("image") === 0) {
              try { upload.image_path = URL.createObjectURL(files[i]); } catch (e) {}
            }
            state.uploads.push(upload);
            queued.push(id);
          }
          persist();
          return { queued: queued };
        });
      },

      confirm: function (id, payload) {
        return ready().then(function () {
          var d = state.documents.filter(function (x) { return x.id === id; })[0];
          if (!d) return Promise.reject(new Error("no doc " + id));
          if (payload.doc_type) d.doc_type = payload.doc_type;
          if (payload.client_id) d.client_id = payload.client_id;
          // Apply field edits: anything differing from extraction = corrected.
          var incoming = payload.fields || {};
          Object.keys(incoming).forEach(function (k) {
            var newVal = String(incoming[k]);
            var cur = d.fields[k] || { value: "", corrected: false };
            var baseline = cur.corrected ? cur.original_value : cur.value;
            if (newVal !== String(cur.value)) {
              d.fields[k] = { value: newVal, corrected: true, original_value: baseline };
            }
          });
          d.status = "confirmed";
          // Update client checklist: confirmed doc_type joins received_docs.
          var c = state.clients.filter(function (x) { return x.id === d.client_id; })[0];
          if (c && c.received_docs.indexOf(d.doc_type) < 0) c.received_docs.push(d.doc_type);
          persist();
          return clone(d);
        });
      }
    };
  })();

  function clone(x) { return JSON.parse(JSON.stringify(x)); }

  var api = MOCK ? mock : real;
  api.MOCK = MOCK;
  window.KeepBookAPI = api;
})();
