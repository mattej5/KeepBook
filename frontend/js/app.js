/*
 * KeepBook UI — three views over one api.js.
 * Load-bearing bits: the correction render (strike + ink), the checklist
 * state derivation (confirmed == checked), and the ink-in animation.
 */
(function () {
  "use strict";
  var api = window.KeepBookAPI;

  /* ---------- field metadata ---------- */
  var FIELD_LABELS = {
    employer: "Employer", ein: "EIN", employee_name: "Employee", ssn: "SSN",
    box1_wages: "Wages (Box 1)", box2_fed_withheld: "Fed. tax withheld (Box 2)",
    payer: "Payer", recipient: "Recipient", recipient_tin: "Recipient TIN",
    box1_interest: "Interest income (Box 1)", box4_fed_withheld: "Fed. tax withheld (Box 4)",
    box1_nonemployee_comp: "Nonemployee comp. (Box 1)",
    lender: "Lender", borrower: "Borrower", borrower_tin: "Borrower TIN",
    box1_mortgage_interest: "Mortgage interest (Box 1)",
    // Real backend schema keys (backend/pipeline.py FIELD_SCHEMA) — without these,
    // K-1 / 1099-INT / 1098 render raw snake_case in Review.
    recipient_name: "Recipient", box1_interest_income: "Interest income (Box 1)",
    box3_other_income: "Other income (Box 3)",
    partnership_name: "Partnership", partner_name: "Partner",
    partnership_ein: "Partnership EIN", ordinary_income: "Ordinary income",
    borrower_name: "Borrower"
  };
  // Classify-only doc types (T65): classified + assigned + confirmed, but no
  // fields are extracted (extract: false). Kept in sync with backend
  // pipeline.CLASSIFY_ONLY_TYPES. They still satisfy a checklist item once
  // confirmed (matching is by doc_type string).
  var CLASSIFY_ONLY_TYPES = [
    "1099-DIV", "1099-B", "1099-R", "1099-G",
    "1098-T", "1098-E", "1095-A",
    "property tax statement", "charitable receipt", "brokerage statement",
    "W-9", "engagement letter"
  ];
  var CLASSIFY_ONLY = {};
  CLASSIFY_ONLY_TYPES.forEach(function (t) { CLASSIFY_ONLY[t] = 1; });
  var DOC_TYPES = ["W-2", "1099-NEC", "1099-INT", "1099-MISC", "K-1", "1098"].concat(CLASSIFY_ONLY_TYPES);
  var TIN_FIELDS = { ssn: 1, recipient_tin: 1, borrower_tin: 1, partnership_ein: 1 };
  function labelFor(k) { return FIELD_LABELS[k] || k; }
  function isClassifyOnly(t) { return !!CLASSIFY_ONLY[t]; }
  function isMoney(k) { return /^box|wages|interest|withheld|comp|income|mortgage/.test(k); }
  // Mask any SSN/TIN/EIN — the privacy story. Covers ssn, *_tin, *_ein.
  function isTin(k) { return !!TIN_FIELDS[k] || k === "ssn" || /(?:tin|ein)$/.test(k); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function maskTin(v) {
    var d = String(v || "").replace(/\D/g, "");
    if (d.length < 4) return v || "—";
    return "•••-••-" + d.slice(-4);
  }
  function money(v) { return v === "" || v == null ? "—" : "$" + v; }

  var CHECK_SVG = '<svg width="18" height="18" viewBox="0 0 24 24"><path d="M4 13 L10 18.5 L21 4.5" fill="none" stroke="#2f5fd0" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  function checkSvgAnimated() {
    return '<svg width="18" height="18" viewBox="0 0 24 24"><path class="ink-draw" d="M4 13 L10 18.5 L21 4.5" fill="none" stroke="#2f5fd0" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  /* ---------- app state ---------- */
  var state = { view: "capture", dropped: [], polling: null, nerdsTimer: null, selectedDoc: null, clients: [], justConfirmed: {}, knownReviewIds: {} };

  var $ = function (id) { return document.getElementById(id); };
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  /* ================= NAVIGATION ================= */
  function show(view) {
    state.view = view;
    if (state.nerdsTimer) { clearInterval(state.nerdsTimer); state.nerdsTimer = null; }
    document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
    $("view-" + view).classList.add("active");
    document.querySelectorAll("#nav button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.view === view);
    });
    if (view === "capture") renderCapture();
    if (view === "review") renderReview();
    if (view === "dashboard") renderDashboard();
    if (view === "nerds") {
      renderNerds();
      // refresh the live telemetry every 5s while this view is on screen
      state.nerdsTimer = setInterval(function () {
        if (state.view === "nerds") renderNerds();
        else { clearInterval(state.nerdsTimer); state.nerdsTimer = null; }
      }, 5000);
    }
  }

  function fmtReceived(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d)) return null;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  // A doc that hasn't been confirmed yet — the Review queue's contents.
  function needsReview(d) {
    return d.status === "extracted" || d.status === "unrecognized" || d.status === "error";
  }
  // Friendly local wall-clock time, e.g. "8:42 AM".
  function fmtIntakeTime(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d)) return null;
    return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  // Most recent received_at across the docs (the last intake).
  function lastIntakeIso(docs) {
    var best = null, bestT = -Infinity;
    (docs || []).forEach(function (d) {
      if (!d.received_at) return;
      var t = new Date(d.received_at).getTime();
      if (!isNaN(t) && t > bestT) { bestT = t; best = d.received_at; }
    });
    return best;
  }
  // Operational one-liner shown where the old privacy copy sat: how many docs
  // are waiting on the reviewer, and when the last batch came in. Live data.
  function opSummary(docs) {
    var awaiting = (docs || []).filter(needsReview).length;
    var parts = [awaiting + " awaiting review"];
    var t = fmtIntakeTime(lastIntakeIso(docs));
    if (t) parts.push("last intake " + t);
    return parts.join(" · ");
  }
  function renderCaptureOp() {
    var el = $("capture-op"); if (!el) return;
    api.getDocuments().then(function (docs) {
      var e = $("capture-op"); if (e) e.textContent = opSummary(docs);
    }).catch(function () {});
  }

  /* ================= CAPTURE / SUBMIT ================= */
  function renderCapture() {
    renderCaptureOp();
    var block = $("queue-block");
    if (!state.dropped.length && !state.processing) {
      block.innerHTML = '<div class="section-label">Queued · 0 files</div>' +
        '<div class="rl-empty">No files queued yet. Drop a client\'s documents above.</div>';
      $("process-btn").disabled = true;
      $("process-btn").textContent = "Process files";
      return;
    }
    if (state.processing) return; // processing view is managed by the poll loop
    var rows = state.dropped.map(function (f, i) {
      var thumb = f._url
        ? '<span class="qthumb"><img src="' + f._url + '" alt=""></span>'
        : '<span class="qthumb"><span class="ext">' + esc((f.name.split(".").pop() || "").toUpperCase().slice(0, 3)) + '</span></span>';
      return '<div class="qrow">' + thumb +
        '<div class="meta"><div class="fname">' + esc(f.name) + '</div>' +
        '<div class="fsub tnum">' + fmtSize(f.size) + '</div></div>' +
        '<button class="btn-ghost btn-sm" data-rm="' + i + '">remove</button></div>';
    }).join("");
    block.innerHTML = '<div class="section-label">Queued · ' + state.dropped.length + ' file' +
      (state.dropped.length === 1 ? "" : "s") + '</div>' + rows;
    block.querySelectorAll("[data-rm]").forEach(function (b) {
      b.onclick = function () { state.dropped.splice(+b.dataset.rm, 1); renderCapture(); };
    });
    $("process-btn").disabled = false;
    $("process-btn").textContent = "Process " + state.dropped.length + " file" + (state.dropped.length === 1 ? "" : "s");
  }

  function fmtSize(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1048576) return Math.round(n / 1024) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function addFiles(list) {
    var rejected = 0;
    for (var i = 0; i < list.length; i++) {
      var f = list[i];
      var okType = f.type && f.type.indexOf("image") === 0;
      var okExt = /\.(png|jpe?g|webp|gif|bmp)$/i.test(f.name || "");
      if (!okType && !okExt) { rejected++; continue; }  // KeepBook reads images, not PDFs
      if (okType) { try { f._url = URL.createObjectURL(f); } catch (e) {} }
      state.dropped.push(f);
    }
    if (rejected) toast(rejected + (rejected === 1 ? " file" : " files") + " skipped — KeepBook reads images, not PDFs");
    renderCapture();
  }

  function startProcessing() {
    var files = state.dropped.slice();
    if (!files.length) return;
    // Snapshot the done-count first so pre-seeded / earlier docs don't inflate
    // this batch's progress bar (IMPROVEMENTS #3).
    api.getQueue().then(
      function (q0) { beginBatch(files, (q0 && q0.done) || 0); },
      function () { beginBatch(files, 0); }
    );
  }

  function beginBatch(files, baseDone) {
    state.processing = { total: files.length, baseDone: baseDone };
    state.dropped = [];
    renderProcessing({ pending: files.length, processing: null, done: baseDone });
    $("process-btn").disabled = true;
    $("process-btn").textContent = "Processing…";
    api.intake(files).then(function () { pollQueue(true); }).catch(function () {
      toast("Couldn’t reach the model — is the model server running?");
      state.processing = null;
      $("process-btn").disabled = false;
      $("process-btn").textContent = "Process files";
      renderCapture();
    });
  }

  function renderProcessing(q) {
    var base = state.processing ? (state.processing.baseDone || 0) : 0;
    var total = state.processing ? state.processing.total : (q.pending + q.done);
    var doneCount = Math.max(0, q.done - base);
    var pct = total ? Math.min(100, Math.round((doneCount / total) * 100)) : 0;
    var rows = "";
    for (var i = 0; i < total; i++) {
      var label, cls;
      if (i < doneCount) { label = "extracted"; cls = "done"; }
      else if (i === doneCount && q.pending > 0) { label = "reading…"; cls = "processing"; }
      else { label = "queued"; cls = "done"; }
      rows += '<div class="qrow"><span class="qthumb"><span class="ext">DOC</span></span>' +
        '<div class="meta"><div class="fname">Document ' + (i + 1) + '</div>' +
        '<div class="fsub">Gemma 4 · on-device</div></div>' +
        '<span class="qstatus ' + cls + '">' + label + '</span></div>';
    }
    $("queue-block").innerHTML =
      '<div class="section-label">Processing on this Mac · ' + doneCount + ' of ' + total + '</div>' +
      '<div class="progress-wrap"><div class="progress-track"><div class="progress-fill" style="width:' + pct + '%"></div></div></div>' +
      '<div style="margin-top:12px">' + rows + '</div>';
  }

  function pollQueue(immediate) {
    if (state.polling) clearInterval(state.polling);
    function step() {
      api.getQueue().then(function (q) {
        if (state.view === "capture") renderProcessing(q);
        // Done only when nothing is pending AND nothing is in flight — the
        // in-flight doc keeps status "pending" until the worker finishes, so
        // this no longer declares completion a doc early (IMPROVEMENTS #3).
        if (q.pending === 0 && !q.processing) {
          clearInterval(state.polling); state.polling = null;
          var n = state.processing ? state.processing.total : q.done;
          state.processing = null;
          $("process-btn").disabled = true;
          $("process-btn").textContent = "Process files";
          if (state.view === "capture") {
            $("queue-block").innerHTML =
              '<div class="section-label">Done · ' + n + ' document' + (n === 1 ? "" : "s") + ' ready</div>' +
              '<div class="rl-empty">Sorted into per-client bins. <a href="#" id="to-review">Review them →</a></div>';
            var lnk = $("to-review"); if (lnk) lnk.onclick = function (e) { e.preventDefault(); show("review"); };
            renderCaptureOp();
          }
          toast(n + " document" + (n === 1 ? "" : "s") + " ready in Review");
        }
      }).catch(function () {
        clearInterval(state.polling); state.polling = null;
        state.processing = null;
        toast("Lost the model connection — is the server running?");
        $("process-btn").disabled = false;
        $("process-btn").textContent = "Process files";
      });
    }
    if (immediate) step();
    state.polling = setInterval(step, 2000);
  }

  /* ================= BIN REVIEW & CORRECTION ================= */
  function renderReview() {
    api.getDocuments().then(function (docs) {
      var needs = docs.filter(needsReview);
      var opEl = $("review-op"); if (opEl) opEl.textContent = opSummary(docs);
      var listEl = $("review-list");
      if (!needs.length) {
        listEl.innerHTML = '<div class="rl-empty">Nothing to review. All caught up.</div>';
        $("review-detail").innerHTML = '<div class="rl-empty">Confirmed documents flow to the Dashboard checklist.</div>';
        return;
      }
      // keep selection valid
      if (!state.selectedDoc || !needs.some(function (d) { return d.id === state.selectedDoc; })) {
        state.selectedDoc = needs[0].id;
      }
      // Cards settle in only on ARRIVAL: a doc whose id we haven't shown before
      // gets .card-land once. Selection re-renders reuse the same ids, so nothing
      // re-animates — same one-shot discipline as state.justConfirmed.
      var landIdx = 0;
      listEl.innerHTML = needs.map(function (d) {
        var badge = d.status === "error"
          ? '<span class="rl-badge error">Error</span>'
          : d.status === "unrecognized"
            ? '<span class="rl-badge unrec">Unrecognized</span>'
            : '<span class="rl-badge needs">Needs review</span>';
        var client = clientName(d.client_id) || "Unassigned";
        var typ = d.status === "error" ? "Couldn’t read" : d.status === "unrecognized" ? "Unknown document" : d.doc_type;
        var isNew = !state.knownReviewIds[d.id];
        var landAttr = isNew ? ' style="animation-delay:' + Math.min(landIdx++, 6) * 40 + 'ms"' : '';
        return '<button class="rl-item' + (isNew ? " card-land" : "") + (d.id === state.selectedDoc ? " active" : "") + '" data-id="' + d.id + '"' + landAttr + '>' +
          '<div class="rl-type">' + esc(typ) + pageLabel(d) + '</div>' +
          '<div class="rl-client">' + esc(client) + '</div>' + badge + '</button>';
      }).join("");
      needs.forEach(function (d) { state.knownReviewIds[d.id] = 1; });
      listEl.querySelectorAll("[data-id]").forEach(function (b) {
        b.onclick = function () { state.selectedDoc = b.dataset.id; renderReview(); };
      });
      renderDetail(state.selectedDoc);
    });
  }

  function clientName(id) {
    var c = state.clients.filter(function (x) { return x.id === id; })[0];
    return c ? c.name : null;
  }

  // "p. N" chip shown wherever a doc is listed (Review list + detail heading).
  function pageLabel(doc) {
    var pn = doc && doc.page_number;
    return pn ? ' <span class="page-label">p. ' + esc(String(pn)) + '</span>' : '';
  }

  // Effective picks read straight off the identity controls. pickedClient does
  // NOT fall back to doc.client_id: the model's guess is not consent, so an
  // unconfirmed doc with a suggestion still resolves to "" until the reviewer
  // affirms it (checkbox) or names someone else (dropdown).
  function pickedType(doc) { var t = $("pick-type"); return t ? t.value : doc.doc_type; }
  function pickedClient() {
    var drop = $("pick-client");
    if (drop && drop.value) return drop.value;
    var affirm = $("pick-affirm");
    if (affirm && affirm.checked) return affirm.dataset.client;
    return "";
  }

  // Confirm stays blocked, with the reason shown, until identity is affirmed.
  function refreshConfirmState(doc) {
    var cb = $("confirm-btn");
    if (!cb) return;
    var typ = pickedType(doc);
    var reason = "";
    if (!typ || typ === "UNRECOGNIZED") reason = "Choose a document type first";
    else if (!pickedClient()) reason = "Confirm the client identity first";
    cb.disabled = !!reason;
    var note = $("confirm-block-note");
    if (note) note.textContent = reason;
  }

  function renderDetail(id) {
    api.getDocument(id).then(function (doc) {
      var el = $("review-detail");
      var imgUrl = api.imageUrl(doc);
      var imgHtml = imgUrl
        ? '<div class="doc-image"><img src="' + imgUrl + '" alt="source document"></div>'
        : '<div class="doc-image"><div class="noimg">No preview available</div></div>';

      var confirmed = doc.status === "confirmed";
      // The strike-draw only plays on the confirm MOMENT — same one-shot key the
      // dashboard ink-in uses (set in doConfirm, consumed by renderDashboard).
      // Later re-renders of an already-confirmed doc render the settled strike.
      var fresh = confirmed && !!state.justConfirmed[doc.client_id + "|" + doc.doc_type];
      var errored = doc.status === "error";
      var unrec = doc.status === "unrecognized";
      var classifyOnly = isClassifyOnly(doc.doc_type);
      var right = "";

      var heading = errored ? "Model unavailable" : unrec ? "Unrecognized document" : doc.doc_type;
      right += '<h2>' + esc(heading) + pageLabel(doc) + '</h2>';
      right += '<div class="doc-sub">' + (confirmed
        ? 'Confirmed · in ' + esc(clientName(doc.client_id) || "—") + '\'s file'
        : errored
          ? 'This document could not be read.'
          : unrec
            ? 'Model read this file'
            : classifyOnly
              ? 'Classified as ' + esc(doc.doc_type) + '. No fields are extracted for this type — assign a client and confirm to file it.'
              : 'Model read this ' + doc.doc_type + '. Check it against the image, correct anything wrong, then confirm.') + '</div>';

      if (errored && !confirmed) {
        right += '<div class="error-banner">Couldn’t reach the model — is the model server running? Retry.</div>';
      } else if (unrec && !confirmed) {
        right += '<div class="unrec-banner">The model would not force this into a tax-form type. Classify it by hand if it belongs to a client, or leave it here.</div>';
      }

      // client + doc_type pickers (needed to confirm; per docs/API.md /confirm)
      if (!confirmed) {
        right += '<div class="doc-type-picker">';
        if (unrec || errored) {
          right += '<label>Document type</label><select class="select" id="pick-type">' +
            '<option value="">Choose type…</option>' +
            DOC_TYPES.map(function (t) { return '<option value="' + t + '">' + t + '</option>'; }).join("") +
            '</select>';
        }
        // Identity is an affirmative act, never a silent default. Misassigning a
        // document to the wrong client is a confidentiality incident for a tax
        // firm, so the control starts UNCONFIRMED even when the model pre-assigned
        // a client — the reviewer must restate "this belongs to X" before Confirm
        // enables. When there is a suggestion, that restatement is a checkbox;
        // either way the dropdown is never pre-selected.
        var suggested = state.clients.filter(function (c) { return c.id === doc.client_id; })[0];
        right += '<label>Client identity</label>';
        if (suggested) {
          right += '<label class="identity-affirm"><input type="checkbox" id="pick-affirm" data-client="' +
            esc(suggested.id) + '"> This document belongs to <strong>' + esc(suggested.name) + '</strong></label>' +
            '<div class="identity-alt">Wrong client? Choose the correct one below.</div>';
        }
        right += '<select class="select" id="pick-client">' +
          '<option value="">' + (suggested ? "Or file under a different client…" : "Choose client…") + '</option>' +
          state.clients.map(function (c) {
            // NEVER pre-selected — a defaulted selection would make identity a
            // no-op instead of an affirmative act.
            return '<option value="' + c.id + '">' + esc(c.name) + '</option>';
          }).join("") +
          '</select>';
        // Page number (optional): continuation pages with no extractable name get
        // filed by hand under a client + page number.
        right += '<label>Page number (optional)</label>' +
          '<input class="select page-input" id="pick-page" type="number" min="1" step="1" placeholder="e.g. 2"' +
          (doc.page_number ? ' value="' + esc(String(doc.page_number)) + '"' : '') + '>';
        right += '</div>';
      }

      // fields
      var keys = Object.keys(doc.fields || {});
      if (keys.length) {
        right += '<div class="field-list">';
        keys.forEach(function (k) {
          var f = doc.fields[k];
          right += '<div class="field-row"><div class="field-label">' + esc(labelFor(k)) + '</div><div>';
          if (isTin(k)) {
            right += '<div class="field-val tnum">' + esc(maskTin(f.value)) + '</div>';
          } else if (confirmed && f.corrected) {
            right += correctionHtml(k, f, fresh);
          } else if (confirmed) {
            right += '<div class="field-val tnum">' + esc(isMoney(k) ? money(f.value) : f.value || "—") + '</div>';
          } else {
            var lc = f.low_confidence ? " lowconf" : "";
            right += '<input class="field-input' + lc + '" data-field="' + k + '" value="' + esc(f.value) + '">';
            if (f.low_confidence) right += '<div class="lowconf-note">low confidence — check the photo</div>';
          }
          right += '</div></div>';
        });
        right += '</div>';
      } else if (classifyOnly && !confirmed) {
        right += '<div class="rl-empty">Classify-only document — no fields to review. Assign a client and confirm to check it off their list.</div>';
      } else if (!unrec && !errored) {
        right += '<div class="rl-empty">No fields extracted.</div>';
      }

      // model trace disclosure — one click answers "what did the model see and
      // say?" (served from raws/<id>.json via GET /documents/<id>/trace)
      right += '<details class="trace-disclosure"><summary>View model trace</summary>' +
        '<div class="trace-body">Loading…</div></details>';

      // footer
      right += '<div class="detail-foot">';
      if (confirmed) {
        right += '<div style="display:flex;align-items:center;gap:8px;color:var(--ink-blue);font-weight:600;font-size:14px">' +
          CHECK_SVG + ' Confirmed — checklist updated</div>' +
          '<button class="btn-ghost btn-sm" id="to-dash">See it on the Dashboard →</button>';
      } else {
        right += '<div class="foot-left"><div class="privacy-line">Confirming files this into the client\'s checklist.</div>' +
          '<div class="confirm-block-note" id="confirm-block-note"></div></div>' +
          '<button class="btn btn-sm" id="confirm-btn" disabled>Confirm ' + (unrec || errored ? "document" : doc.doc_type) + '</button>';
      }
      right += '</div>';

      // Quiet discard affordance — remove an erroneous ingest. Confirmation
      // dialog names the doc type + client before anything is deleted.
      right += '<div class="detail-discard"><button class="link-discard" id="discard-btn">Discard this document</button></div>';

      el.className = "detail";
      el.innerHTML = imgHtml + '<div class="fields">' + right + '</div>';

      var cb = $("confirm-btn"); if (cb) cb.onclick = function () { doConfirm(doc); };
      var td = $("to-dash"); if (td) td.onclick = function () { show("dashboard"); };
      var db = $("discard-btn"); if (db) db.onclick = function () { doDelete(doc); };
      // Identity is an affirmative act: keep Confirm blocked (with a reason) until
      // the reviewer has restated the client. Re-evaluate on every identity edit.
      if (!confirmed) {
        var reeval = function () { refreshConfirmState(doc); };
        ["pick-affirm", "pick-client", "pick-type"].forEach(function (idn) {
          var elx = $(idn); if (elx) elx.addEventListener("change", reeval);
        });
        // A checked affirmation and an explicit dropdown pick are mutually
        // exclusive statements — picking a different client clears the checkbox.
        var drop = $("pick-client");
        if (drop) drop.addEventListener("change", function () {
          var af = $("pick-affirm"); if (af && drop.value) af.checked = false;
        });
        refreshConfirmState(doc);
      }
      // lazy-load the model trace the first time the disclosure is opened
      var det = el.querySelector(".trace-disclosure");
      if (det) det.addEventListener("toggle", function () {
        if (det.open && !det.dataset.loaded) { det.dataset.loaded = "1"; renderTrace(doc.id, det.querySelector(".trace-body")); }
      });
    });
  }

  function renderTrace(id, bodyEl) {
    if (!bodyEl) return;
    api.getTrace(id).then(function (t) {
      var head = '<div class="trace-head">' +
        esc((t.model_runtime || "") + " · " + (t.model_name || "")) +
        (t.latency_s != null ? " · " + t.latency_s + "s" : "") +
        (t.retried ? " · retried" : "") + '</div>';
      var calls = (t.calls || []).map(function (c) {
        var prompt = String(c.prompt == null ? "" : c.prompt);
        var promptHead = prompt.slice(0, 240) + (prompt.length > 240 ? "…" : "");
        var isErr = c.error != null;
        var resp = isErr ? ("ERROR: " + c.error) : String(c.response == null ? "" : c.response);
        return '<div class="trace-call"><div class="tc-seq">call ' + esc(String(c.seq || "?")) + '</div>' +
          '<div class="tc-label">prompt</div><pre class="tc-pre">' + esc(promptHead) + '</pre>' +
          '<div class="tc-label">response</div><pre class="tc-pre' + (isErr ? " tc-err" : "") + '">' + esc(resp) + '</pre></div>';
      }).join("");
      bodyEl.innerHTML = head + (calls || '<div class="rl-empty">No model calls recorded.</div>');
    }).catch(function (e) {
      var msg = /404/.test(e.message)
        ? "No model trace was recorded for this document."
        : "Trace unavailable (" + e.message + ").";
      bodyEl.innerHTML = '<div class="rl-empty">' + esc(msg) + '</div>';
    });
  }

  function correctionHtml(k, f, fresh) {
    var oldV = isMoney(k) ? money(f.original_value) : f.original_value;
    var newV = isMoney(k) ? money(f.value) : f.value;
    return '<div class="correction' + (fresh ? " fresh" : "") + '"><span class="old tnum">' + esc(oldV) + '</span>' +
      '<span class="new tnum">' + esc(newV) + '</span>' +
      '<span class="pen-note">corrected</span></div>';
  }

  function doConfirm(doc) {
    var docType = pickedType(doc);
    var clientId = pickedClient();
    // Backstop the disabled button: identity is the human gate, so a missing
    // affirmation blocks the write rather than silently filing to the guess.
    if (!docType || docType === "UNRECOGNIZED") { toast("Choose a document type first"); return; }
    if (!clientId) { toast("Confirm the client identity first"); return; }

    var fields = {};
    document.querySelectorAll("[data-field]").forEach(function (inp) {
      fields[inp.dataset.field] = inp.value.trim();
    });

    var payload = { client_id: clientId, doc_type: docType, fields: fields };
    var pageEl = $("pick-page");
    if (pageEl && pageEl.value.trim() !== "") {
      var pn = parseInt(pageEl.value, 10);
      if (!isNaN(pn) && pn >= 1) payload.page_number = pn;
    }

    api.confirm(doc.id, payload).then(function (updated) {
      // remember for the dashboard ink-in animation
      state.justConfirmed[clientId + "|" + updated.doc_type] = true;
      var anyCorrected = Object.keys(updated.fields).some(function (kk) { return updated.fields[kk].corrected; });
      toast(anyCorrected ? "Correction saved — value struck, checklist updated" : "Confirmed — checklist updated");
      state.selectedDoc = updated.id;         // keep detail showing the confirmed doc (with corrections)
      renderDetail(updated.id);
      // refresh the list so the confirmed doc drops out of "needs review"
      api.getDocuments().then(function (docs) {
        var needs = docs.filter(function (d) { return d.status === "extracted" || d.status === "unrecognized" || d.status === "error"; });
        var listEl = $("review-list");
        listEl.innerHTML = needs.length ? needs.map(function (d) {
          var badge = d.status === "error"
            ? '<span class="rl-badge error">Error</span>'
            : d.status === "unrecognized"
              ? '<span class="rl-badge unrec">Unrecognized</span>'
              : '<span class="rl-badge needs">Needs review</span>';
          return '<button class="rl-item" data-id="' + d.id + '"><div class="rl-type">' +
            esc(d.status === "error" ? "Couldn’t read" : d.status === "unrecognized" ? "Unknown document" : d.doc_type) + pageLabel(d) + '</div>' +
            '<div class="rl-client">' + esc(clientName(d.client_id) || "Unassigned") + '</div>' + badge + '</button>';
        }).join("") : '<div class="rl-empty">Nothing left to review. All caught up.</div>';
        listEl.querySelectorAll("[data-id]").forEach(function (b) {
          b.onclick = function () { state.selectedDoc = b.dataset.id; renderReview(); };
        });
      });
    }).catch(function (e) { toast("Confirm failed: " + e.message); });
  }

  function doDelete(doc) {
    var typeLabel = doc.status === "unrecognized" ? "this unrecognized document"
      : doc.status === "error" ? "this unreadable document"
      : (doc.doc_type ? "this " + doc.doc_type : "this document");
    var who = clientName(doc.client_id) || "no client yet";
    if (!window.confirm(
      "Discard " + typeLabel + " (" + who + ")?\n\n" +
      "It will be removed and its checklist item unchecked. This can't be undone."
    )) return;
    api.deleteDocument(doc.id).then(function () {
      toast("Document discarded");
      state.selectedDoc = null;
      // Refresh the cached client checklist so the Dashboard reflects the removal.
      api.getClients().then(function (cs) { state.clients = cs; renderReview(); },
                           function () { renderReview(); });
    }).catch(function (e) { toast("Discard failed: " + e.message); });
  }

  /* ================= CHECKLIST DASHBOARD ================= */
  function renderDashboard() {
    Promise.all([api.getClients(), api.getDocuments(), api.getStats()]).then(function (res) {
      var clients = res[0], docs = res[1], stats = res[2];
      state.clients = clients;
      renderStats(stats, fmtIntakeTime(lastIntakeIso(docs)));

      var grid = $("client-grid");
      grid.innerHTML = clients.map(function (c) { return clientCardHtml(c, docs); }).join("");

      grid.querySelectorAll("[data-req]").forEach(function (b) {
        b.onclick = function () { toast("Chase email drafted for " + b.dataset.req); };
      });
      // one-shot ink animations consumed on render
      state.justConfirmed = {};
    });
  }

  function clientCardHtml(c, docs) {
    var received = {};
    (c.received_docs || []).forEach(function (t) { received[t] = true; });

    // docs of this client currently in review (extracted, not yet confirmed)
    var inReview = {};
    docs.forEach(function (d) {
      if (d.client_id === c.id && d.status === "extracted") inReview[d.doc_type] = d;
    });
    // metadata for received docs (date / correction count)
    var meta = {};
    docs.forEach(function (d) {
      if (d.client_id === c.id && d.status === "confirmed") {
        var corr = Object.keys(d.fields || {}).filter(function (k) { return d.fields[k].corrected; }).length;
        meta[d.doc_type] = { date: fmtReceived(d.received_at), corr: corr };
      }
    });

    var total = c.expected_docs.length;
    var haveCount = c.expected_docs.filter(function (t) { return received[t]; }).length;
    var complete = haveCount === total;

    var rows = c.expected_docs.map(function (t) {
      if (received[t]) {
        var animate = state.justConfirmed[c.id + "|" + t];
        var m = meta[t] || {};
        var sub = "Received" + (m.date ? " " + m.date : "") + (m.corr ? " · " + m.corr + " correction" + (m.corr > 1 ? "s" : "") : "");
        return '<div class="check-row' + (animate ? " row-settle" : "") + '">' +
          '<span class="check-box done">' + (animate ? checkSvgAnimated() : CHECK_SVG) + '</span>' +
          '<div class="grow"><div class="c-label">' + esc(t) + '</div><div class="c-sub">' + esc(sub) + '</div></div></div>';
      }
      if (inReview[t]) {
        return '<div class="check-row pending"><span class="check-box"></span>' +
          '<div class="grow"><div class="c-label">' + esc(t) + '</div>' +
          '<div class="c-sub">In review — waiting on your confirm</div></div>' +
          '<span class="pending-flag">in review</span></div>';
      }
      return '<div class="check-row missing"><span class="check-box"></span>' +
        '<div class="grow"><div class="c-label">' + esc(t) + '</div>' +
        '<div class="c-sub">Not received yet</div></div>' +
        '<span class="missing-flag">MISSING</span>' +
        '<button class="request-link" data-req="' + esc(c.name) + '" style="margin-left:12px">Request</button></div>';
    }).join("");

    var badge = complete
      ? '<span class="client-badge-complete">all in ✓</span>'
      : '';
    var frac = '<span class="progress-frac tnum' + (complete ? " done" : "") + '">' + haveCount + '/' + total + '</span>';

    return '<div class="card client-card"><div class="card-pad">' +
      '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px">' +
      '<div class="client-name">' + esc(c.name) + '</div>' +
      '<div class="client-head-right">' + frac + badge + '</div></div>' +
      '<div class="client-meta">2025 tax intake · <span class="count tnum">' + haveCount + ' of ' + total + ' received</span></div>' +
      rows +
      '<div class="client-foot"><a class="export-csv-link" href="' + esc(api.exportCsvUrl(c.id)) +
      '" download="' + esc(c.id) + '.csv" title="Confirmed documents as CSV — imports anywhere">Export CSV ↓</a></div>' +
      '</div></div>';
  }

  function renderStats(s, lastIntake) {
    var rate = (s.correction_rate * 100).toFixed(1) + "%";
    $("stats-line").innerHTML =
      '<div class="stats-bar">' +
      '<div class="stat"><span class="num tnum">' + s.fields_extracted + '</span><span class="lbl">fields extracted</span></div>' +
      '<div class="stat"><span class="num tnum">' + s.fields_corrected + '</span><span class="lbl">fields corrected</span></div>' +
      '<div class="stat"><span class="num rate tnum">' + rate + '</span><span class="lbl">correction rate</span></div>' +
      (lastIntake ? '<span class="stats-note">last intake ' + esc(lastIntake) + '</span>' : '') +
      '</div>';
  }

  /* ================= STATS FOR NERDS ================= */
  function renderNerds() {
    api.getTimeline(24).then(function (t) {
      var tt = t.totals;
      var pct = function (x) { return (x * 100).toFixed(1) + "%"; };
      var pct0 = function (x) { return Math.round(x * 100) + "%"; };

      var html = "";
      html += '<div class="screen-head">' +
        '<div><div style="display:flex;align-items:center;gap:10px">' +
        '<h1>Stats for nerds</h1><span class="live-dot-wrap"><span class="live-dot"></span>live</span></div>' +
        '<div class="sub">The dashboard shows the last 24 hours. Nothing leaves this Mac.</div></div>' +
        '<div class="nerd-runtime tnum">Gemma 4 e4b · on-device</div></div>';

      // headline tiles (T33: docs processed, first-try classification %, correction rate, latency)
      html += '<div class="tiles">' +
        tile(tt.docs_processed, "docs processed", "") +
        tile(pct0(tt.first_try_type_acc), "classified right first try", "blue") +
        tile(pct(tt.correction_rate), "correction rate (red pen)", "red") +
        '<div class="tile"><div class="tile-num tnum">' + tt.median_latency_s + 's</div>' +
        '<div class="tile-lbl">median · p95 ' + tt.p95_latency_s + 's</div></div>' +
        '</div>';

      // docs-per-hour bar strip. Labels are the viewer's LOCAL wall-clock hour
      // (the /stats/timeline buckets carry UTC hours; the strip is hourly and
      // ends at the current hour, so derive local labels from position).
      var max = Math.max.apply(null, t.buckets.map(function (b) { return b.docs; }).concat([1]));
      var nowHr = new Date();
      var nB = t.buckets.length;
      function localHour(i) {
        var d = new Date(nowHr.getTime()); d.setMinutes(0, 0, 0);
        d.setHours(d.getHours() - (nB - 1 - i));
        return ("0" + d.getHours()).slice(-2) + ":00";
      }
      var bars = t.buckets.map(function (b, i) {
        var h = Math.max(4, Math.round(b.docs / max * 64));
        var recent = i >= t.buckets.length - 3;
        return '<div class="bar' + (recent ? " recent" : "") + '" style="height:' + h + 'px" title="' +
          esc(localHour(i)) + ' — ' + b.docs + ' doc' + (b.docs === 1 ? "" : "s") +
          (b.corrections ? ", " + b.corrections + " correction" + (b.corrections === 1 ? "" : "s") : "") + '"></div>';
      }).join("");
      html += '<div class="nerd-block">' +
        '<div class="nb-head"><span class="section-label" style="margin:0">Docs per hour</span>' +
        '<span class="nb-hint">now ←</span></div>' +
        '<div class="bar-strip">' + bars + '</div>' +
        '<div class="bar-axis"><span>24h ago</span><span>12h</span><span>now</span></div></div>';

      // extraction + corrections-by-category blocks
      var lowPct = tt.fields_extracted ? pct(tt.fields_low_confidence / tt.fields_extracted) : "0%";
      var cats = tt.corrections_by_category;
      html += '<div class="nerd-cols">' +
        '<div class="nerd-block"><div class="section-label" style="margin-top:0">Extraction</div>' +
        nbRow("Fields extracted", '<span>' + tt.fields_extracted + '</span>') +
        nbRow("Flagged low-confidence", '<span class="hl-chip">' + tt.fields_low_confidence + ' · ' + lowPct + '</span>') +
        nbRow("Corrected by a reviewer", '<span style="color:var(--red)">' + tt.fields_corrected + ' · ' + pct(tt.correction_rate) + '</span>') +
        '</div>' +
        '<div class="nerd-block"><div class="section-label" style="margin-top:0">Corrections by field</div>' +
        nbRow("Dollar amounts", '<span>' + cats.money + '</span>') +
        nbRow("TIN / SSN digits", '<span>' + cats.tin_ssn + '</span>') +
        nbRow("Payer / employer names", '<span>' + cats.names + '</span>') +
        nbRow("Document type (reclassified)", '<span>' + cats.doc_type + '</span>') +
        '</div></div>';

      html += '<div class="nerd-foot">' +
        '<span class="hand-note" style="font-size:17px">the red-pen rate is the number to watch</span></div>';

      $("nerds-body").innerHTML = html;
    }).catch(function (e) {
      $("nerds-body").innerHTML = '<div class="rl-empty">Timeline unavailable (' + esc(e.message) + '). Needs backend /stats/timeline or mock mode.</div>';
    });
  }

  function tile(num, label, tone) {
    var cls = tone === "blue" ? " style=\"color:var(--ink-blue)\"" : tone === "red" ? " style=\"color:var(--red)\"" : "";
    return '<div class="tile"><div class="tile-num tnum"' + cls + '>' + num + '</div>' +
      '<div class="tile-lbl">' + esc(label) + '</div></div>';
  }
  function nbRow(label, valueHtml) {
    return '<div class="nb-row"><span class="nb-lbl">' + esc(label) + '</span>' +
      '<span class="nb-val tnum">' + valueHtml + '</span></div>';
  }

  /* ================= BOOT ================= */
  function boot() {
    // nav
    document.querySelectorAll("#nav button").forEach(function (b) {
      b.onclick = function () { show(b.dataset.view); };
    });
    // reset (mock only)
    if (api.MOCK && api.resetMock) {
      var rb = $("reset-btn"); rb.hidden = false;
      rb.onclick = function () { api.resetMock(); location.reload(); };
    }
    // drop zone
    var dz = $("dropzone"), fi = $("file-input");
    dz.onclick = function () { fi.click(); };
    fi.onchange = function () { if (fi.files.length) addFiles(fi.files); fi.value = ""; };
    ["dragenter", "dragover"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("dragover"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("dragover"); });
    });
    dz.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
    });
    $("process-btn").onclick = startProcessing;

    // preload clients so review/dashboard have names
    api.getClients().then(function (cs) { state.clients = cs; });

    // deep-link to a starting view
    var v = new URLSearchParams(location.search).get("view");
    show(v && $("view-" + v) ? v : "capture");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
