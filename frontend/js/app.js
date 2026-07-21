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
  // The firm's STANDARD tax-organizer checklist — the single generic list every
  // new client starts from. This is the "organizer template" layer of the
  // roadmap: seed the new-client expected-docs picker from these forms, then let
  // the preparer PRUNE per client (remove chips that don't apply, add extras)
  // BEFORE creating. Defined once here; the create form renders its chips from a
  // copy of this array (never a second hardcoded list) so pruning stays honest.
  var ORGANIZER_TEMPLATE = [
    "W-2", "1099-NEC", "1099-INT", "1099-MISC", "K-1",
    "1098", "1099-DIV", "charitable receipt", "brokerage statement"
  ];
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
  // Quiet pen glyph for the per-card "edit client" affordance.
  var PEN_SVG = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
  function checkSvgAnimated() {
    return '<svg width="18" height="18" viewBox="0 0 24 24"><path class="ink-draw" d="M4 13 L10 18.5 L21 4.5" fill="none" stroke="#2f5fd0" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  /* ---------- app state ---------- */
  var state = { view: "capture", dropped: [], polling: null, nerdsTimer: null, selectedDoc: null, clients: [], justConfirmed: {}, knownReviewIds: {}, expandedRuns: {}, runTraceCache: {} };
  // New-client create form: open flag, the in-progress name, and the pruned
  // expected-docs list (seeded from ORGANIZER_TEMPLATE when the form opens).
  var newClientState = { open: false, name: "", docs: [], error: "" };
  // Edit-client form (CRUD-AUDIT gaps #2/#4): rename + edit expected_docs, and
  // delete a doc-free client. Mirrors newClientState; docCount is the number of
  // documents referencing this client at open time, which decides whether the
  // delete control (0 docs) or the muted "reassign first" note (>0) is shown.
  var editClientState = { open: false, id: null, name: "", docs: [], docCount: 0, error: "" };
  // CSV client-list import (T69). When non-null the new-client card shows a
  // preview-then-commit panel instead of the single-client form. Shape:
  // { toCreate:[{name,expected_docs}], dupes, found, blanks, headerSkipped, fileName }.
  var csvImportState = null;

  var $ = function (id) { return document.getElementById(id); };
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.remove("show"); }, 2200);
  }

  /* ================= NAVIGATION ================= */
  function show(view) {
    state.view = view;
    // Hash routing: refresh keeps the current view (replaceState — no history spam).
    if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
    refreshNavBadge();
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
      renderNerds(true);   // animate count-up + bars only on ENTRY
      // refresh the live telemetry every 5s while this view is on screen
      state.nerdsTimer = setInterval(function () {
        if (state.view === "nerds") renderNerds(false);   // silent refresh — no re-animation
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

  // Sidebar Review badge — the "N docs are waiting on you" signal. Refreshed on
  // every nav and piggybacked on render paths so confirms/deletes update it.
  function refreshNavBadge() {
    api.getDocuments().then(function (docs) {
      var n = (docs || []).filter(needsReview).length;
      var b = $("nav-review-badge"); if (!b) return;
      b.hidden = n === 0;
      b.textContent = String(n);
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
      var isPdf = f.type === "application/pdf" || /\.pdf$/i.test(f.name || "");
      var okExt = /\.(png|jpe?g|webp|gif|bmp)$/i.test(f.name || "");
      if (!okType && !okExt && !isPdf) { rejected++; continue; }  // images + PDFs only
      if (okType) { try { f._url = URL.createObjectURL(f); } catch (e) {} }  // PDFs render server-side
      state.dropped.push(f);
    }
    if (rejected) toast(rejected + (rejected === 1 ? " file" : " files") + " skipped — KeepBook reads images (PNG/JPG) and PDFs");
    renderCapture();
  }

  function startProcessing() {
    var files = state.dropped.slice();
    if (!files.length) return;
    // Snapshot the done-count first so pre-seeded / earlier docs don't inflate
    // this batch's progress bar (IMPROVEMENTS #3).
    api.getQueue().then(
      function (q0) { submitBatch(files, (q0 && q0.done) || 0, null); },
      function () { submitBatch(files, 0, null); }
    );
  }

  // Submit one batch. `password` applies to any password-protected PDFs in the
  // batch; it is passed straight to the intake request and never stored. On a
  // password_required / password_incorrect 400 the SAME files are re-submitted
  // with the password the user types (promptForPdfPassword).
  function submitBatch(files, baseDone, password) {
    // Enter the processing shell only on the first attempt; a password retry
    // keeps it (and the already-cleared queue) in place.
    if (!state.processing) {
      state.processing = { total: files.length, baseDone: baseDone };
      state.dropped = [];
      renderProcessing({ pending: files.length, processing: null, done: baseDone });
    }
    $("process-btn").disabled = true;
    $("process-btn").textContent = "Processing…";
    api.intake(files, password).then(function (r) {
      // Remember this batch's doc ids so the done-state can tell whether any
      // came back assigned to a client (drives the honest done copy, #4).
      if (state.processing) state.processing.batchIds = (r && r.queued) || [];
      pollQueue(true);
    }).catch(function (err) {
      var detail = (err && err.detail) || "";
      if (detail.indexOf("password_required") === 0 || detail.indexOf("password_incorrect") === 0) {
        // A PDF in this batch is encrypted. Prompt, then retry the same files.
        promptForPdfPassword(files, baseDone, detail.indexOf("password_incorrect") === 0);
        return;
      }
      toast("Couldn’t reach the model — is the model server running?");
      state.processing = null;
      $("process-btn").disabled = false;
      $("process-btn").textContent = "Process files";
      renderCapture();
    });
  }

  // Inline password prompt for an encrypted PDF. The password lives only in the
  // input's memory: it is read into a local variable for the retry and the input
  // is cleared immediately, so nothing is persisted anywhere. type="password"
  // keeps it off-screen; the retry sends the SAME files with it.
  function promptForPdfPassword(files, baseDone, wrong) {
    var msg = wrong
      ? "That password didn’t work. Try again."
      : "This PDF is password-protected. Enter its password to unlock it on this Mac.";
    $("queue-block").innerHTML =
      '<div class="section-label">Password needed</div>' +
      '<div class="pdf-pw">' +
        '<div class="pdf-pw-msg' + (wrong ? " error" : "") + '">' + esc(msg) + '</div>' +
        '<form class="pdf-pw-row" id="pdf-pw-form" autocomplete="off">' +
          '<input type="password" id="pdf-pw-input" class="field-input pdf-pw-input" placeholder="PDF password" autocomplete="off" aria-label="PDF password">' +
          '<button type="submit" class="btn btn-sm" id="pdf-pw-submit">Unlock</button>' +
          '<button type="button" class="btn-ghost btn-sm" id="pdf-pw-cancel">Cancel</button>' +
        '</form>' +
        '<div class="pdf-pw-note">Your password never leaves this Mac and is never saved.</div>' +
      '</div>';
    $("process-btn").disabled = true;
    $("process-btn").textContent = "Waiting for password…";
    var input = $("pdf-pw-input");
    if (input) input.focus();
    $("pdf-pw-form").onsubmit = function (e) {
      e.preventDefault();
      var pw = input ? input.value : "";
      if (input) input.value = "";  // clear from the DOM the moment we read it
      if (!pw) { if (input) input.focus(); return; }
      submitBatch(files, baseDone, pw);  // password kept only in this closure
    };
    $("pdf-pw-cancel").onclick = function () {
      state.processing = null;
      state.dropped = files;  // restore the queue so the user can retry or remove
      $("process-btn").disabled = false;
      $("process-btn").textContent = "Process files";
      renderCapture();
    };
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
          var batchIds = (state.processing && state.processing.batchIds) || [];
          state.processing = null;
          $("process-btn").disabled = true;
          $("process-btn").textContent = "Process files";
          if (state.view === "capture") {
            // The "sorted into per-client bins" line is only honest when a doc
            // actually landed in a client bin. Nothing is filed to a client
            // until confirm, so a batch that came back all-unassigned reads
            // "ready for review" instead. Default to bins on any lookup failure.
            var paintDone = function (assignedAny) {
              $("queue-block").innerHTML =
                '<div class="section-label">Done · ' + n + ' document' + (n === 1 ? "" : "s") + ' ready</div>' +
                '<div class="rl-empty">' + (assignedAny ? "Sorted into per-client bins." : "Ready for review.") +
                ' <a href="#" id="to-review">Review them →</a></div>';
              var lnk = $("to-review"); if (lnk) lnk.onclick = function (e) { e.preventDefault(); show("review"); };
              renderCaptureOp();
            };
            if (batchIds.length) {
              api.getDocuments().then(function (docs) {
                paintDone(docs.some(function (d) { return batchIds.indexOf(d.id) >= 0 && d.client_id; }));
              }).catch(function () { paintDone(true); });
            } else {
              paintDone(true);
            }
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
          '<div class="rl-client">' + esc(client) + '</div>' + badge + dupBadge(d) + sourceChip(d) + '</button>';
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

  // "possible duplicate" pill for a Review row whose backend flagged it a near/
  // exact duplicate of an existing doc (doc.duplicate_of). Rendered from the new
  // field only — absent/null => no pill. Human confirms via the detail compare.
  function dupBadge(doc) {
    return doc && doc.duplicate_of
      ? '<span class="rl-badge dup" title="Possible duplicate of ' + esc(doc.duplicate_of) +
        '">Possible duplicate</span>'
      : '';
  }

  // Provenance chip for a doc the watched-inbox thread ingested on its own
  // (backend stamps doc.source === "folder"; normal uploads leave it absent). A
  // tiny, unobtrusive "from inbox folder" tag so the reviewer can see a doc arrived
  // without anyone uploading it. Absent/other source => no chip.
  function sourceChip(doc) {
    return doc && doc.source === "folder"
      ? '<span class="rl-badge source" title="Auto-ingested from the watched inbox folder">from inbox folder</span>'
      : '';
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
        ? '<div class="doc-image">' +
            '<div class="doc-image-scroll"><img src="' + imgUrl + '" alt="source document"></div>' +
            '<div class="zoom-controls">' +
              '<button type="button" class="zbtn zout" title="Zoom out" aria-label="Zoom out">−</button>' +
              '<span class="zbtn zpct" aria-live="polite">Fit</span>' +
              '<button type="button" class="zbtn zin" title="Zoom in" aria-label="Zoom in">+</button>' +
              '<button type="button" class="zbtn zfit" title="Fit to pane">Fit</button>' +
            '</div>' +
          '</div>'
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
      right += '<h2>' + esc(heading) + pageLabel(doc) + sourceChip(doc) + '</h2>';
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

      // Possible-duplicate compare: this doc's image next to the doc it matched,
      // both labeled, with the two human verdicts — Keep (not a dup) or Discard
      // this copy. Rendered only when the backend flagged doc.duplicate_of. The
      // model proposes; the human confirms — nothing is ever auto-dropped.
      if (doc.duplicate_of) {
        var dupUrl = api.imageUrl({ id: doc.duplicate_of });
        right += '<div class="dup-compare">' +
          '<div class="dup-head">Possible duplicate</div>' +
          '<div class="dup-sub">This copy looks like <span class="tnum">' + esc(doc.duplicate_of) +
            '</span>, already in KeepBook. Compare them, then keep this copy or discard it.</div>' +
          '<div class="dup-pair">' +
            '<figure class="dup-fig"><figcaption>This copy · <span class="tnum">' + esc(doc.id) + '</span></figcaption>' +
              (imgUrl ? '<img src="' + esc(imgUrl) + '" alt="this copy">' : '<div class="dup-noimg">No preview</div>') +
            '</figure>' +
            '<figure class="dup-fig"><figcaption>Already filed · <span class="tnum">' + esc(doc.duplicate_of) + '</span></figcaption>' +
              (dupUrl ? '<img src="' + esc(dupUrl) + '" alt="already-filed document">' : '<div class="dup-noimg">No preview</div>') +
            '</figure>' +
          '</div>' +
          '<div class="dup-actions">' +
            '<button class="btn btn-sm" id="dup-keep-btn" type="button">Keep — not a duplicate</button>' +
            '<button class="link-discard" id="dup-discard-btn" type="button">Discard this copy</button>' +
          '</div>' +
        '</div>';
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
            // Highest-stakes field class — always masked on screen. Corrected:
            // both sides masked (correctionHtml). Confirmed-clean: masked static.
            // Unconfirmed: masked with a pen-icon "edit" → cleartext input.
            if (confirmed && f.corrected) {
              right += correctionHtml(k, f, fresh);
            } else if (confirmed) {
              right += '<div class="field-val tnum">' + esc(maskTin(f.value)) + '</div>';
            } else {
              right += tinEditHtml(k, f);
            }
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
        // Confirmed message + a quiet, non-destructive Re-open control stacked
        // beneath it (foot-left is a column). Re-open reads calmer than Discard:
        // it hovers to the ink-blue accent, never the red pen.
        right += '<div class="foot-left">' +
          '<div style="display:flex;align-items:center;gap:8px;color:var(--ink-blue);font-weight:600;font-size:14px">' +
          CHECK_SVG + ' Confirmed — checklist updated</div>' +
          '<button class="link-reopen" id="reopen-btn">Re-open for correction</button></div>' +
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
      setupZoom(el);

      var cb = $("confirm-btn"); if (cb) cb.onclick = function () { doConfirm(doc); };
      var td = $("to-dash"); if (td) td.onclick = function () { show("dashboard"); };
      var rb = $("reopen-btn"); if (rb) rb.onclick = function () { doUnconfirm(doc); };
      var db = $("discard-btn"); if (db) db.onclick = function () { doDelete(doc); };
      // Duplicate-compare verdicts: keep this copy (clear the flag) or discard it
      // (reuse the existing delete path — no second delete).
      var dk = $("dup-keep-btn"); if (dk) dk.onclick = function () { doResolveDuplicate(doc); };
      var dd = $("dup-discard-btn"); if (dd) dd.onclick = function () { doDelete(doc); };
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
      // TIN/SSN edit toggle: pen link reveals the cleartext input (focus+select),
      // "done" or Esc collapses back to masked, re-masking to the current value.
      el.querySelectorAll("[data-tin-edit]").forEach(function (btn) {
        btn.onclick = function () {
          var wrap = btn.closest(".tin-field"); if (!wrap) return;
          wrap.querySelector(".tin-masked").hidden = true;
          var box = wrap.querySelector(".tin-edit"); box.hidden = false;
          var inp = box.querySelector("input"); if (inp) { inp.focus(); inp.select(); }
        };
      });
      el.querySelectorAll("[data-tin-done]").forEach(function (btn) {
        btn.onclick = function () {
          var wrap = btn.closest(".tin-field"); if (!wrap) return;
          var inp = wrap.querySelector(".tin-edit input");
          var disp = wrap.querySelector(".tin-masked .field-val");
          if (disp && inp) disp.textContent = maskTin(inp.value);
          wrap.querySelector(".tin-edit").hidden = true;
          wrap.querySelector(".tin-masked").hidden = false;
        };
      });
      el.querySelectorAll(".tin-edit input").forEach(function (inp) {
        inp.addEventListener("keydown", function (e) {
          if (e.key === "Escape") {
            var done = inp.closest(".tin-field").querySelector("[data-tin-done]");
            if (done) done.click();
          }
        });
      });
      // lazy-load the model trace the first time the disclosure is opened
      var det = el.querySelector(".trace-disclosure");
      if (det) det.addEventListener("toggle", function () {
        if (det.open && !det.dataset.loaded) { det.dataset.loaded = "1"; renderTrace(doc.id, det.querySelector(".trace-body")); }
      });
    });
  }

  // Zoom for the Review source image. Zoom state (zi) is a fresh closure local per
  // renderDetail call — selecting a different document rebuilds the pane and resets
  // to Fit, so no zoom bleeds between docs. zi = -1 → Fit (image fills pane width);
  // otherwise zi indexes ZOOM_STEPS, a percentage of the image's natural size.
  function setupZoom(root) {
    var scroll = root.querySelector(".doc-image-scroll");
    var img = scroll && scroll.querySelector("img");
    if (!scroll || !img) return null;
    var ZOOM_STEPS = [100, 150, 200, 300, 400];
    var MAX = ZOOM_STEPS.length - 1;
    var zi = -1;
    var pctEl = root.querySelector(".zpct");
    var inBtn = root.querySelector(".zin");
    var outBtn = root.querySelector(".zout");
    var fitBtn = root.querySelector(".zfit");

    function apply() {
      if (zi < 0) {
        img.style.width = "";          // fall back to CSS width:100% — fit-to-pane
        img.style.maxWidth = "";
        scroll.classList.remove("zoomed");
        if (pctEl) pctEl.textContent = "Fit";
      } else {
        var pct = ZOOM_STEPS[zi];
        var natural = img.naturalWidth || img.clientWidth || 0;
        img.style.maxWidth = "none";
        img.style.width = natural ? Math.round(natural * pct / 100) + "px" : pct + "%";
        scroll.classList.add("zoomed");
        if (pctEl) pctEl.textContent = pct + "%";
      }
      if (inBtn) inBtn.disabled = zi >= MAX;
      if (outBtn) outBtn.disabled = zi < 0;
    }
    function setZoom(i) {
      zi = i < -1 ? -1 : (i > MAX ? MAX : i);
      apply();
    }

    if (inBtn) inBtn.onclick = function () { setZoom(zi + 1); };
    if (outBtn) outBtn.onclick = function () { setZoom(zi - 1); };
    if (fitBtn) fitBtn.onclick = function () { setZoom(-1); };
    // double-click toggles Fit <-> 200%
    img.addEventListener("dblclick", function () { setZoom(zi < 0 ? 2 : -1); });
    // cmd/ctrl + wheel zooms (plain wheel keeps native scroll untouched)
    scroll.addEventListener("wheel", function (e) {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      setZoom(e.deltaY < 0 ? zi + 1 : zi - 1);
    }, { passive: false });
    // drag-to-pan when zoomed (native scroll also works)
    var pan = null;
    scroll.addEventListener("pointerdown", function (e) {
      if (!scroll.classList.contains("zoomed") || e.button !== 0) return;
      pan = { x: e.clientX, y: e.clientY, l: scroll.scrollLeft, t: scroll.scrollTop };
      scroll.classList.add("grabbing");
      try { scroll.setPointerCapture(e.pointerId); } catch (_) {}
    });
    scroll.addEventListener("pointermove", function (e) {
      if (!pan) return;
      scroll.scrollLeft = pan.l - (e.clientX - pan.x);
      scroll.scrollTop = pan.t - (e.clientY - pan.y);
    });
    function endPan() { pan = null; scroll.classList.remove("grabbing"); }
    scroll.addEventListener("pointerup", endPan);
    scroll.addEventListener("pointercancel", endPan);

    apply();
    return { setZoom: setZoom, apply: apply, steps: ZOOM_STEPS };
  }

  function renderTrace(id, bodyEl) {
    if (!bodyEl) return Promise.resolve();
    return api.getTrace(id).then(function (t) {
      var head = '<div class="trace-head">' +
        esc((t.model_runtime || "") + " · " + (t.model_name || "")) +
        (t.latency_s != null ? " · " + t.latency_s + "s" : "") +
        (t.retried ? " · retried" : "") + '</div>';
      var calls = (t.calls || []).map(function (c) {
        var prompt = String(c.prompt == null ? "" : c.prompt);
        var promptHead = prompt.slice(0, 240) + (prompt.length > 240 ? "…" : "");
        var isErr = c.error != null;
        var resp = isErr ? ("ERROR: " + c.error) : String(c.response == null ? "" : c.response);
        // Honest label: the pipeline STAGE that issued this call (classify /
        // extract / region:<field> / ensemble:<model>). Falls back to "call N"
        // for older traces captured before stage labels existed.
        var label = c.stage ? c.stage : ("call " + String(c.seq || "?"));
        var retryTag = c.retry ? '<span class="tc-retry">strict-JSON retry</span>' : "";
        return '<div class="trace-call"><div class="tc-seq">' + esc(label) + retryTag + '</div>' +
          '<div class="tc-label">prompt (input)</div><pre class="tc-pre">' + esc(promptHead) + '</pre>' +
          '<div class="tc-label">raw model output</div><pre class="tc-pre' + (isErr ? " tc-err" : "") + '">' + esc(resp) + '</pre></div>';
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
    var tin = isTin(k);
    // TIN keys: mask BOTH the struck original and the ink-blue corrected value.
    // The provenance ("a digit was fixed") still shows, but two SSNs never sit on
    // screen at once — the whole point of the privacy story.
    var oldV = tin ? maskTin(f.original_value) : (isMoney(k) ? money(f.original_value) : f.original_value);
    var newV = tin ? maskTin(f.value) : (isMoney(k) ? money(f.value) : f.value);
    return '<div class="correction' + (fresh ? " fresh" : "") + '"><span class="old tnum">' + esc(oldV) + '</span>' +
      '<span class="new tnum">' + esc(newV) + '</span>' +
      '<span class="pen-note">corrected</span></div>';
  }

  // TIN/SSN/EIN editor for UNCONFIRMED docs. Masked by default with a quiet pen
  // "edit" link; activating it reveals a CLEARTEXT input pre-filled with the true
  // value. Cleartext while actively editing is deliberate — the reviewer is reading
  // the number straight off the source image to fix a wrong digit and has to see
  // the digits to do it; the masked display is what sits on screen the rest of the
  // time. The input carries data-field like every other field, so doConfirm collects
  // it with zero special-casing (a value left untouched submits unchanged → not a
  // correction). The low-confidence flag renders in BOTH the masked and edit states.
  function tinEditHtml(k, f) {
    var lc = f.low_confidence ? " lowconf" : "";
    var note = f.low_confidence ? '<div class="lowconf-note">low confidence — check the photo</div>' : "";
    var pen = '<svg class="pen-ico" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">' +
      '<path d="M4 20h4L18.5 9.5a2 2 0 0 0-2.83-2.83L5 17v3z" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    return '<div class="tin-field" data-tin="' + esc(k) + '">' +
      '<div class="tin-masked">' +
        '<span class="field-val tnum">' + esc(maskTin(f.value)) + '</span>' +
        '<button type="button" class="tin-edit-link" data-tin-edit="' + esc(k) + '">' + pen + 'edit</button>' +
        note +
      '</div>' +
      '<div class="tin-edit" hidden>' +
        '<input class="field-input tnum' + lc + '" data-field="' + esc(k) + '" value="' + esc(f.value) + '">' +
        '<button type="button" class="tin-done-link" data-tin-done="' + esc(k) + '">done</button>' +
        note +
      '</div>' +
    '</div>';
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
        // Refresh the "N awaiting review" op-notes here so they don't read one
        // high until an unrelated re-render (the confirm just moved a doc out of
        // the review queue). Same fresh docs we already fetched.
        var opEl = $("review-op"); if (opEl) opEl.textContent = opSummary(docs);
        var capEl = $("capture-op"); if (capEl) capEl.textContent = opSummary(docs);
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
            '<div class="rl-client">' + esc(clientName(d.client_id) || "Unassigned") + '</div>' + badge + dupBadge(d) + sourceChip(d) + '</button>';
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

  // Keep a flagged copy: clear duplicate_of server-side, then re-render Review so
  // the "possible duplicate" badge + compare pane drop away. The doc keeps its
  // place in the review queue (status is unchanged) and can still be confirmed.
  function doResolveDuplicate(doc) {
    api.resolveDuplicate(doc.id).then(function (updated) {
      toast("Kept — not a duplicate");
      state.selectedDoc = updated.id;
      renderReview();          // re-fetches docs, re-renders list + detail
      refreshNavBadge();
    }).catch(function (e) { toast("Couldn’t resolve: " + e.message); });
  }

  // Re-open a confirmed doc for correction (inverse of confirm). The doc returns
  // to the review queue as "needs review" and the detail re-renders editable with
  // its prior values + corrections intact; the checklist item un-checks
  // count-aware server-side.
  function doUnconfirm(doc) {
    api.unconfirm(doc.id).then(function (updated) {
      toast("Re-opened for correction");
      // Clear the one-shot confirm animation flag so the reopened doc renders in
      // its editable state, not the settled strike-through.
      delete state.justConfirmed[doc.client_id + "|" + doc.doc_type];
      state.selectedDoc = updated.id;  // keep it selected; now "extracted" => needs review
      // Refresh cached clients so the Dashboard checklist reflects the un-check,
      // then re-render Review (which re-renders the detail editable) + the badge.
      api.getClients().then(function (cs) { state.clients = cs; renderReview(); },
                           function () { renderReview(); });
      refreshNavBadge();
    }).catch(function (e) { toast("Re-open failed: " + e.message); });
  }

  /* ================= CHECKLIST DASHBOARD ================= */
  function renderDashboard() {
    Promise.all([api.getClients(), api.getDocuments(), api.getStats()]).then(function (res) {
      var clients = res[0], docs = res[1], stats = res[2];
      state.clients = clients;
      state.dashboardDocs = docs;   // used by the edit form to count a client's docs
      renderStats(stats, fmtIntakeTime(lastIntakeIso(docs)));

      var grid = $("client-grid");
      grid.innerHTML = clients.map(function (c) { return clientCardHtml(c, docs); }).join("");

      grid.querySelectorAll("[data-req-client]").forEach(function (b) {
        b.onclick = function () {
          var c = clients.filter(function (x) { return x.id === b.dataset.reqClient; })[0];
          if (!c) return;
          window.location.href = chaseMailtoHref(c);
          toast("Draft opened in your mail app");
        };
      });
      // Quiet pen on each card opens the edit form for that client.
      grid.querySelectorAll("[data-edit-client]").forEach(function (b) {
        b.onclick = function () { openEditClientForm(b.dataset.editClient); };
      });
      // "Draft reminder" — only rendered on cards with a MISSING item.
      grid.querySelectorAll("[data-nudge-client]").forEach(function (b) {
        b.onclick = function () { openNudgeModal(b.dataset.nudgeClient, b.dataset.nudgeName); };
      });
      // The create + edit forms render as full-row cards in the grid; the create
      // form's trigger is the static "Add Client" FAB (bound once in boot()).
      appendEditClientCard();
      appendNewClientCard();
      // one-shot ink animations consumed on render
      state.justConfirmed = {};
    });
  }

  /* ---------- New-client (create) affordance ---------- */
  // A ghost card pinned after the client cards. Collapsed it is a quiet
  // "+ New client" prompt; clicking expands it into an in-page form (plain DOM,
  // no modal library). paintNewClientCard() rebuilds the whole card; paintChips()
  // rebuilds ONLY the chip list so editing a chip never steals focus from the
  // name field.
  function appendNewClientCard() {
    var grid = $("client-grid");
    if (!grid) return;
    var card = document.createElement("div");
    card.id = "new-client-card";
    grid.appendChild(card);
    paintNewClientCard();
  }

  function openNewClientForm() {
    // Seed the picker from the firm's standard organizer template (a COPY, so
    // pruning this client never mutates the shared template).
    if (editClientState.open) closeEditClientForm();  // never stack two full-row forms
    csvImportState = null;
    newClientState = { open: true, name: "", docs: ORGANIZER_TEMPLATE.slice(), error: "" };
    paintNewClientCard();
    var card = $("new-client-card");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    var nameEl = $("nc-name"); if (nameEl) nameEl.focus();
  }

  function closeNewClientForm() {
    csvImportState = null;
    newClientState = { open: false, name: "", docs: [], error: "" };
    paintNewClientCard();
  }

  function ncSetError(msg) {
    newClientState.error = msg;
    var e = $("nc-error");
    if (e) e.textContent = msg || "";
  }

  function ncAddDoc() {
    var el = $("nc-add-input");
    if (!el) return;
    var v = (el.value || "").trim();
    if (!v) return;
    if (newClientState.docs.indexOf(v) < 0) newClientState.docs.push(v);  // no dup chips
    el.value = "";
    paintChips();
    el.focus();
  }

  function paintChips() {
    var wrap = $("nc-chips");
    if (!wrap) return;
    if (!newClientState.docs.length) {
      wrap.innerHTML = '<span class="nc-chips-empty">No forms expected yet — add one below, or create with none.</span>';
      return;
    }
    wrap.innerHTML = newClientState.docs.map(function (t, i) {
      return '<span class="nc-chip">' + esc(t) +
        '<button class="nc-chip-x" data-i="' + i + '" title="Remove ' + esc(t) + '" aria-label="Remove ' + esc(t) + '">×</button></span>';
    }).join("");
    wrap.querySelectorAll("[data-i]").forEach(function (b) {
      b.onclick = function () { newClientState.docs.splice(+b.dataset.i, 1); paintChips(); };
    });
  }

  function paintNewClientCard() {
    var card = $("new-client-card");
    if (!card) return;
    if (!newClientState.open) {
      // Closed: nothing in the grid — the FAB is the affordance now.
      card.hidden = true;
      card.className = "";
      card.innerHTML = "";
      return;
    }
    // CSV import in progress → the card is a preview-then-commit panel, not the
    // single-client form. Nothing is created until Import is clicked.
    if (csvImportState) { paintCsvPreview(card); return; }
    card.hidden = false;
    card.className = "card client-card new-client-form";
    card.innerHTML = '<div class="card-pad">' +
      '<div class="nc-title">New client</div>' +
      '<label class="nc-label" for="nc-name">Client name</label>' +
      '<input class="select nc-name" id="nc-name" type="text" autocomplete="off" ' +
        'placeholder="e.g. Okafor, Ruth" value="' + esc(newClientState.name) + '">' +
      '<label class="nc-label">Expected documents ' +
        '<span class="nc-hint">— the firm’s standard organizer; remove what this client doesn’t owe</span></label>' +
      '<div class="nc-chips" id="nc-chips"></div>' +
      '<div class="nc-add"><input class="select nc-add-input" id="nc-add-input" type="text" ' +
        'autocomplete="off" placeholder="Add another form…">' +
        '<button class="btn-ghost btn-sm" id="nc-add-btn" type="button">Add</button></div>' +
      '<div class="nc-error" id="nc-error">' + esc(newClientState.error) + '</div>' +
      '<div class="nc-import-row">or <button class="nc-import-link" id="nc-import-link" type="button">import a client list (CSV)</button>' +
        '<input type="file" id="nc-csv-input" accept=".csv,text/csv" hidden></div>' +
      '<div class="nc-foot"><button class="btn-ghost btn-sm" id="nc-cancel" type="button">Cancel</button>' +
        '<button class="btn btn-sm" id="nc-create" type="button">Create client</button></div>' +
      '</div>';
    paintChips();
    var nameEl = $("nc-name");
    if (nameEl) {
      nameEl.oninput = function () { newClientState.name = nameEl.value; if (newClientState.error) ncSetError(""); };
      nameEl.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); submitNewClient(); } };
      nameEl.focus();
    }
    var addInput = $("nc-add-input");
    if (addInput) addInput.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); ncAddDoc(); } };
    var addBtn = $("nc-add-btn"); if (addBtn) addBtn.onclick = ncAddDoc;
    var cancel = $("nc-cancel"); if (cancel) cancel.onclick = closeNewClientForm;
    var create = $("nc-create"); if (create) create.onclick = submitNewClient;
    var importLink = $("nc-import-link");
    var csvInput = $("nc-csv-input");
    if (importLink && csvInput) importLink.onclick = function () { csvInput.click(); };
    if (csvInput) csvInput.onchange = function () {
      var f = csvInput.files && csvInput.files[0];
      csvInput.value = "";          // allow re-picking the same file later
      if (f) onCsvFileChosen(f);
    };
  }

  /* ---------- CSV client-list import (T69) ---------- */
  // Normalization shared by the preview count and the commit loop, so the
  // "M duplicates will be skipped" line can never disagree with what Import does.
  function normName(s) { return String(s == null ? "" : s).trim().toLowerCase(); }

  // Turn a parse result into a frozen commit plan: dedupe against existing client
  // names (case-insensitive) AND within-file repeats, computed ONCE here so the
  // preview and the sequential create loop operate on the identical list.
  function computeCsvPlan(parsed) {
    var seen = {};
    (state.clients || []).forEach(function (c) { seen[normName(c.name)] = true; });
    var toCreate = [];
    var dupes = 0;
    parsed.clients.forEach(function (c) {
      var key = normName(c.name);
      if (seen[key]) { dupes++; return; }
      seen[key] = true;
      toCreate.push(c);
    });
    return {
      toCreate: toCreate, dupes: dupes, found: parsed.clients.length,
      blanks: parsed.skipped_blank, headerSkipped: parsed.header_skipped, fileName: ""
    };
  }

  function onCsvFileChosen(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result == null ? "" : reader.result);
      var parsed = window.KeepBookCsv.parseClientCsv(text, ORGANIZER_TEMPLATE.slice());
      if (!parsed.clients.length) {
        toast("No client rows found in that CSV");
        return;   // stay on the form; nothing to preview
      }
      csvImportState = computeCsvPlan(parsed);
      csvImportState.fileName = file.name || "";
      paintNewClientCard();
    };
    reader.onerror = function () { toast("Couldn't read that file"); };
    reader.readAsText(file);
  }

  function cancelCsvImport() {
    csvImportState = null;
    paintNewClientCard();   // back to the single-client form
  }

  // Preview panel: honest counts + the first few names that WILL be created.
  // Import is disabled when everything is a duplicate (nothing new to add).
  function paintCsvPreview(card) {
    var p = csvImportState;
    card.hidden = false;
    card.className = "card client-card new-client-form";
    var mk = p.toCreate.length;
    var shown = p.toCreate.slice(0, 6).map(function (c) { return esc(c.name); });
    var more = mk - shown.length;
    var namesLine = mk
      ? shown.join(", ") + (more > 0 ? " +" + more + " more" : "")
      : "Every row is already a client — nothing new to import.";
    var summary = p.found + " client" + (p.found === 1 ? "" : "s") + " found · " +
      p.dupes + " duplicate" + (p.dupes === 1 ? "" : "s") + " will be skipped · " +
      p.blanks + " blank row" + (p.blanks === 1 ? "" : "s") + " ignored";
    card.innerHTML = '<div class="card-pad">' +
      '<div class="nc-title">Import client list</div>' +
      (p.fileName ? '<div class="nc-csv-file">' + esc(p.fileName) + '</div>' : '') +
      '<div class="nc-csv-summary">' + esc(summary) + '</div>' +
      '<div class="nc-csv-names">' + namesLine + '</div>' +
      '<div class="nc-error" id="csv-error"></div>' +
      '<div class="nc-foot"><button class="btn-ghost btn-sm" id="csv-cancel" type="button">Cancel</button>' +
        '<button class="btn btn-sm" id="csv-import-btn" type="button"' + (mk ? "" : " disabled") + '>' +
        'Import ' + mk + ' client' + (mk === 1 ? "" : "s") + '</button></div>' +
      '</div>';
    var cc = $("csv-cancel"); if (cc) cc.onclick = cancelCsvImport;
    var ci = $("csv-import-btn"); if (ci) ci.onclick = runCsvImport;
  }

  // Commit: sequential, awaited api.createClient calls (the backend mints ids —
  // no parallel spam). Partial-failure honesty: on the first failure, stop, report
  // how many landed, and re-render so the user sees exactly what exists.
  function runCsvImport() {
    var plan = csvImportState;
    if (!plan || !plan.toCreate.length) return;
    var btn = $("csv-import-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Importing…"; }
    var total = plan.toCreate.length;
    var created = 0;
    var chain = Promise.resolve();
    plan.toCreate.forEach(function (c) {
      chain = chain.then(function () {
        return api.createClient({ name: c.name, expected_docs: (c.expected_docs || []).slice() })
          .then(function () { created++; });
      });
    });
    chain.then(function () {
      csvImportState = null;
      closeNewClientForm();
      toast("Imported " + created + " client" + (created === 1 ? "" : "s"));
      renderDashboard();
    }).catch(function (e) {
      csvImportState = null;
      closeNewClientForm();
      var msg = e && e.message ? e.message : "error";
      toast("Imported " + created + " of " + total + " — " + msg);
      renderDashboard();
    });
  }

  function submitNewClient() {
    var name = (newClientState.name || "").trim();
    if (!name) { ncSetError("Client name is required."); var n = $("nc-name"); if (n) n.focus(); return; }
    var payload = { name: name, expected_docs: newClientState.docs.slice() };
    var btn = $("nc-create");
    if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
    api.createClient(payload).then(function (client) {
      toast("Client added — " + client.name);
      closeNewClientForm();
      // Re-render the dashboard so the new client appears immediately with a
      // 0/N checklist (received_docs is empty on a brand-new client, so every
      // expected form shows MISSING).
      renderDashboard();
    }).catch(function (e) {
      ncSetError("Couldn't create client: " + e.message);
      if (btn) { btn.disabled = false; btn.textContent = "Create client"; }
    });
  }

  /* ---------- Edit-client (rename / expected_docs / delete) affordance ---------- */
  // A full-row card (like the create form) that opens below the grid when a
  // card's pen is clicked. paintEditClientCard() rebuilds the whole card;
  // paintEditChips() rebuilds ONLY the chip list so editing a chip never steals
  // focus from the name field.
  function appendEditClientCard() {
    var grid = $("client-grid");
    if (!grid) return;
    var card = document.createElement("div");
    card.id = "edit-client-card";
    grid.appendChild(card);
    paintEditClientCard();
  }

  function openEditClientForm(clientId) {
    var c = (state.clients || []).filter(function (x) { return x.id === clientId; })[0];
    if (!c) return;
    // Close the create form so two full-row forms never stack.
    if (newClientState.open) closeNewClientForm();
    var docCount = (state.dashboardDocs || []).filter(function (d) {
      return d.client_id === clientId;
    }).length;
    editClientState = {
      open: true, id: clientId, name: c.name,
      docs: (c.expected_docs || []).slice(),   // COPY — editing never mutates state.clients
      docCount: docCount, error: ""
    };
    paintEditClientCard();
    var card = $("edit-client-card");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    var nameEl = $("ec-name"); if (nameEl) nameEl.focus();
  }

  function closeEditClientForm() {
    editClientState = { open: false, id: null, name: "", docs: [], docCount: 0, error: "" };
    paintEditClientCard();
  }

  function ecSetError(msg) {
    editClientState.error = msg;
    var e = $("ec-error");
    if (e) e.textContent = msg || "";
  }

  function ecAddDoc() {
    var el = $("ec-add-input");
    if (!el) return;
    var v = (el.value || "").trim();
    if (!v) return;
    if (editClientState.docs.indexOf(v) < 0) editClientState.docs.push(v);  // no dup chips
    el.value = "";
    paintEditChips();
    el.focus();
  }

  function paintEditChips() {
    var wrap = $("ec-chips");
    if (!wrap) return;
    if (!editClientState.docs.length) {
      wrap.innerHTML = '<span class="nc-chips-empty">No forms expected — add one below, or save with none.</span>';
      return;
    }
    wrap.innerHTML = editClientState.docs.map(function (t, i) {
      return '<span class="nc-chip">' + esc(t) +
        '<button class="nc-chip-x" data-i="' + i + '" title="Remove ' + esc(t) + '" aria-label="Remove ' + esc(t) + '">×</button></span>';
    }).join("");
    wrap.querySelectorAll("[data-i]").forEach(function (b) {
      b.onclick = function () { editClientState.docs.splice(+b.dataset.i, 1); paintEditChips(); };
    });
  }

  function paintEditClientCard() {
    var card = $("edit-client-card");
    if (!card) return;
    if (!editClientState.open) {
      card.hidden = true; card.className = ""; card.innerHTML = "";
      return;
    }
    var n = editClientState.docCount;
    // Delete is offered ONLY when the client has zero documents (the backend
    // guards the same way with a 409); otherwise a muted note points at the fix.
    var deleteControl = n > 0
      ? '<span class="ec-locked-note">has ' + n + ' document' + (n === 1 ? "" : "s") +
          ' — reassign or discard them first</span>'
      : '<button class="ec-danger" id="ec-delete" type="button">Delete client</button>';
    card.hidden = false;
    card.className = "card client-card new-client-form";
    card.innerHTML = '<div class="card-pad">' +
      '<div class="nc-title">Edit client</div>' +
      '<label class="nc-label" for="ec-name">Client name</label>' +
      '<input class="select nc-name" id="ec-name" type="text" autocomplete="off" ' +
        'placeholder="e.g. Okafor, Ruth" value="' + esc(editClientState.name) + '">' +
      '<label class="nc-label">Expected documents ' +
        '<span class="nc-hint">— the forms this client owes; remove any that no longer apply</span></label>' +
      '<div class="nc-chips" id="ec-chips"></div>' +
      '<div class="nc-add"><input class="select nc-add-input" id="ec-add-input" type="text" ' +
        'autocomplete="off" placeholder="Add another form…">' +
        '<button class="btn-ghost btn-sm" id="ec-add-btn" type="button">Add</button></div>' +
      '<div class="nc-error" id="ec-error">' + esc(editClientState.error) + '</div>' +
      '<div class="ec-foot"><div class="ec-foot-left">' + deleteControl + '</div>' +
        '<div class="ec-foot-right"><button class="btn-ghost btn-sm" id="ec-cancel" type="button">Cancel</button>' +
        '<button class="btn btn-sm" id="ec-save" type="button">Save changes</button></div></div>' +
      '</div>';
    paintEditChips();
    var nameEl = $("ec-name");
    if (nameEl) {
      nameEl.oninput = function () { editClientState.name = nameEl.value; if (editClientState.error) ecSetError(""); };
      nameEl.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); submitEditClient(); } };
      nameEl.focus();
    }
    var addInput = $("ec-add-input");
    if (addInput) addInput.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); ecAddDoc(); } };
    var addBtn = $("ec-add-btn"); if (addBtn) addBtn.onclick = ecAddDoc;
    var cancel = $("ec-cancel"); if (cancel) cancel.onclick = closeEditClientForm;
    var save = $("ec-save"); if (save) save.onclick = submitEditClient;
    var del = $("ec-delete"); if (del) del.onclick = deleteEditClient;
  }

  function submitEditClient() {
    var id = editClientState.id;
    var name = (editClientState.name || "").trim();
    if (!name) { ecSetError("Client name is required."); var nm = $("ec-name"); if (nm) nm.focus(); return; }
    var payload = { name: name, expected_docs: editClientState.docs.slice() };
    var btn = $("ec-save");
    if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    api.updateClient(id, payload).then(function (client) {
      toast("Client updated — " + client.name);
      closeEditClientForm();
      renderDashboard();
    }).catch(function (e) {
      ecSetError("Couldn't save: " + e.message);
      if (btn) { btn.disabled = false; btn.textContent = "Save changes"; }
    });
  }

  function deleteEditClient() {
    var id = editClientState.id;
    var name = editClientState.name || id;
    if (!window.confirm('Delete client "' + name + '"? This cannot be undone.')) return;
    var btn = $("ec-delete");
    if (btn) { btn.disabled = true; btn.textContent = "Deleting…"; }
    api.deleteClient(id).then(function () {
      toast("Client deleted — " + name);
      closeEditClientForm();
      renderDashboard();
    }).catch(function (e) {
      // The backend guards deletion (409) if a document slipped in since the
      // form opened; surface it instead of silently failing.
      ecSetError("Couldn't delete: " + e.message);
      if (btn) { btn.disabled = false; btn.textContent = "Delete client"; }
    });
  }

  /* ---------- Draft reminder (nudge) modal ---------- */
  // Visible-autonomy affordance (ROADMAP Phase 2, Tier A #4): the model DRAFTS
  // a per-client "still waiting on" note from GET /clients/{id}/nudge; the
  // human reviews it in an editable textarea and copies it — there is no send
  // capability anywhere, by design. Only shown on cards with a MISSING item
  // (clientCardHtml omits the affordance entirely once a client is complete).
  var nudgeState = { open: false, clientId: null, clientName: "", loading: false, error: "", result: null };

  function ensureNudgeOverlay() {
    var el = $("nudge-overlay");
    if (el) return el;
    el = document.createElement("div");
    el.id = "nudge-overlay";
    el.className = "nudge-overlay";
    document.body.appendChild(el);
    el.addEventListener("click", function (e) { if (e.target === el) closeNudgeModal(); });
    return el;
  }

  // Close = REMOVE the overlay node from the DOM, never hide it. The first
  // ship relied on el.hidden, but the `hidden` attribute only maps to the UA
  // stylesheet's [hidden]{display:none}, and our own author rule
  // .nudge-overlay{display:flex} overrides UA declarations — so the "hidden"
  // overlay stayed a full-viewport z-index-60 click shield and froze the app
  // (found in the coordinator's real-browser pass). Removal is synchronous
  // (no close fade, so no transitionend to miss) and ensureNudgeOverlay
  // recreates a fresh node on the next open.
  function removeNudgeOverlay() {
    var el = $("nudge-overlay");
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function nudgeEscHandler(e) { if (e.key === "Escape") closeNudgeModal(); }

  function openNudgeModal(clientId, clientName) {
    nudgeState = { open: true, clientId: clientId, clientName: clientName || "", loading: true, error: "", result: null };
    paintNudgeModal();
    document.addEventListener("keydown", nudgeEscHandler);
    api.getNudge(clientId).then(function (r) {
      if (!nudgeState.open || nudgeState.clientId !== clientId) return;  // closed/superseded meanwhile
      nudgeState.loading = false;
      nudgeState.result = r;
      paintNudgeModal();
    }).catch(function (e) {
      if (!nudgeState.open || nudgeState.clientId !== clientId) return;
      nudgeState.loading = false;
      nudgeState.error = (e && e.message) || "error";
      paintNudgeModal();
    });
  }

  function closeNudgeModal() {
    nudgeState = { open: false, clientId: null, clientName: "", loading: false, error: "", result: null };
    document.removeEventListener("keydown", nudgeEscHandler);
    removeNudgeOverlay();
  }

  function paintNudgeModal() {
    // Closed -> make sure no overlay node exists at all (self-healing even if
    // a stray late repaint lands after close), and never create one.
    if (!nudgeState.open) { removeNudgeOverlay(); return; }
    var overlay = ensureNudgeOverlay();

    var body;
    if (nudgeState.loading) {
      body = '<div class="rl-empty">Drafting…</div>';
    } else if (nudgeState.error) {
      body = '<div class="rl-empty">Couldn’t draft a reminder (' + esc(nudgeState.error) + ').</div>';
    } else {
      var r = nudgeState.result || {};
      if (!r.draft) {
        body = '<div class="rl-empty">' + esc(nudgeState.clientName) + ' has everything — nothing to nudge.</div>';
      } else {
        var note = r.generated_by === "model"
          ? '<div class="nudge-note">drafted by the model — review before sending</div>'
          : '<div class="nudge-note nudge-note-template">drafted from a template — review before sending</div>';
        body = '<textarea class="nudge-textarea" id="nudge-textarea" spellcheck="false">' + esc(r.draft) + '</textarea>' +
          note +
          '<div class="nudge-foot"><button class="btn-ghost btn-sm" id="nudge-cancel" type="button">Close</button>' +
          '<button class="btn btn-sm" id="nudge-copy" type="button">Copy</button></div>';
      }
    }

    overlay.innerHTML = '<div class="nudge-modal" role="dialog" aria-modal="true" aria-label="Draft reminder">' +
      '<div class="nudge-head"><div class="nudge-title">Draft reminder</div>' +
      '<button class="nudge-x" id="nudge-x" type="button" aria-label="Close">×</button></div>' +
      '<div class="nudge-sub">' + esc(nudgeState.clientName) + '</div>' +
      '<div class="nudge-body">' + body + '</div></div>';

    var x = $("nudge-x"); if (x) x.onclick = closeNudgeModal;
    var cancel = $("nudge-cancel"); if (cancel) cancel.onclick = closeNudgeModal;
    var copyBtn = $("nudge-copy");
    if (copyBtn) copyBtn.onclick = function () { copyNudgeDraft(copyBtn); };
  }

  function execCommandCopy(ta, done) {
    try {
      ta.focus(); ta.select();
      done(document.execCommand("copy"));
    } catch (e) { done(false); }
  }

  function copyNudgeDraft(btn) {
    var ta = $("nudge-textarea");
    if (!ta) return;
    function done(ok) {
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(function () { if ($("nudge-copy")) $("nudge-copy").textContent = "Copy"; }, 1600);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(ta.value).then(function () { done(true); }, function () { execCommandCopy(ta, done); });
    } else {
      execCommandCopy(ta, done);
    }
  }

  // Build a real mailto: draft chasing a client's still-missing documents.
  // Frontend-only (works offline, same in mock mode). Every interpolated value
  // is URL-encoded; body newlines are CRLF (%0D%0A after encoding). No claims
  // are fabricated — the body lists exactly the expected-minus-received items.
  function chaseMailtoHref(c) {
    var received = {};
    (c.received_docs || []).forEach(function (t) { received[t] = true; });
    var missing = (c.expected_docs || []).filter(function (t) { return !received[t]; });
    var subject = "Missing tax documents — " + c.name;
    var lines = [
      "Hi " + c.name + ",",
      "",
      "As we prepare your 2025 return, we're still missing the following documents:",
      ""
    ];
    missing.forEach(function (t) { lines.push("- " + t); });
    lines.push("");
    lines.push("Could you send these over when you have a moment? Reply to this email or upload them at your convenience.");
    lines.push("");
    lines.push("Thank you.");
    var body = lines.join("\r\n");
    return "mailto:?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
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
        return '<div class="check-row done' + (animate ? " row-settle" : "") + '">' +
          '<span class="check-box done">' + (animate ? checkSvgAnimated() : CHECK_SVG) + '</span>' +
          '<div class="grow"><div class="c-label">' + esc(t) + (animate ? '<span class="new-dot" title="just confirmed"></span>' : '') + '</div><div class="c-sub">' + esc(sub) + '</div></div></div>';
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
        '<button class="request-link" data-req-client="' + esc(c.id) + '" style="margin-left:12px">Request</button></div>';
    }).join("");

    // Confirmed doc types that match no expected checklist item — otherwise
    // they'd be filed and exported but invisible on the card. Listed quietly
    // under the checklist with their received date, only when nonempty.
    var expectedSet = {};
    c.expected_docs.forEach(function (t) { expectedSet[t] = true; });
    var alsoReceived = (c.received_docs || []).filter(function (t) { return !expectedSet[t]; });
    var alsoHtml = "";
    if (alsoReceived.length) {
      alsoHtml = '<div class="also-received"><span class="ar-label">Also received:</span> ' +
        alsoReceived.map(function (t) {
          var m = meta[t] || {};
          return '<span class="ar-item">' + esc(t) +
            (m.date ? ' <span class="ar-date">(' + esc(m.date) + ')</span>' : '') + '</span>';
        }).join(", ") + '</div>';
    }

    var badge = complete
      ? '<span class="client-badge-complete">all in ✓</span>'
      : '';
    var frac = '<span class="progress-frac tnum' + (complete ? " done" : "") + '" title="Documents received and confirmed, out of the ' + total + ' this client is expected to send.">' + haveCount + '/' + total + '</span>';

    // "Draft reminder" — visible-autonomy affordance, only while something is
    // actually missing. margin-right:auto pushes it to the left of the foot
    // row while the CSV link (unchanged) stays flush right via the
    // container's justify-content:flex-end (docs/API.md "Nudge draft").
    var draftReminderBtn = complete ? "" : (
      '<button class="draft-reminder-link" style="margin-right:auto" ' +
      'data-nudge-client="' + esc(c.id) + '" data-nudge-name="' + esc(c.name) + '">Draft reminder</button>'
    );

    return '<div class="card client-card"><div class="card-pad">' +
      '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px">' +
      '<div class="client-name">' + esc(c.name) + '</div>' +
      '<div class="client-head-right">' +
        '<button class="client-edit-btn" data-edit-client="' + esc(c.id) + '" ' +
        'title="Edit client" aria-label="Edit ' + esc(c.name) + '">' + PEN_SVG + '</button>' +
        frac + badge + '</div></div>' +
      '<div class="client-meta">2025 tax intake · <span class="count tnum">' + haveCount + ' of ' + total + ' received</span></div>' +
      rows +
      alsoHtml +
      '<div class="client-foot">' + draftReminderBtn + '<a class="export-csv-link" href="' + esc(api.exportCsvUrl(c.id)) +
      '" download="' + esc(c.id) + '.csv" title="Confirmed documents as CSV — imports anywhere">Export CSV ↓</a></div>' +
      '</div></div>';
  }

  function renderStats(s, lastIntake) {
    var rate = (s.correction_rate * 100).toFixed(1) + "%";
    $("stats-line").innerHTML =
      '<div class="stats-bar">' +
      '<div class="stat" title="Every field value the model has extracted across all processed documents."><span class="num tnum">' + s.fields_extracted + '</span><span class="lbl">fields extracted</span></div>' +
      '<div class="stat" title="Fields where a reviewer changed the model\'s value before confirming."><span class="num tnum">' + s.fields_corrected + '</span><span class="lbl">fields corrected</span></div>' +
      '<div class="stat" title="Corrected fields divided by extracted fields. The live accuracy meter, measured on your real documents."><span class="num rate tnum">' + rate + '</span><span class="lbl">correction rate</span></div>' +
      (lastIntake ? '<span class="stats-note" title="When the most recent document arrived.">last intake ' + esc(lastIntake) + '</span>' : '') +
      '</div>';
  }

  /* ================= STATS FOR NERDS ================= */
  function renderNerds(animate) {
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
        tile(tt.docs_processed, "docs processed", "",
          "Documents that finished the pipeline in the last 24 hours — classified and extracted, or honestly marked unrecognized.") +
        tile(pct0(tt.first_try_type_acc), "classified right first try", "blue",
          "Share of documents whose first automatic classification matched the type a human ultimately confirmed. Manual reclassifications count against it.") +
        tile(pct(tt.correction_rate), "correction rate (red pen)", "red",
          "Corrected fields ÷ extracted fields, last 24 hours. The live accuracy meter — measured on your real documents, not a benchmark.") +
        '<div class="tile" title="Seconds from intake to extraction, per document. Median is the typical case; p95 is the slow tail."><div class="tile-num tnum">' + tt.median_latency_s + 's</div>' +
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
        var grow = animate ? " bar-grow" : "";
        var delay = animate ? ";animation-delay:" + (i * 18) + "ms" : "";
        return '<div class="bar' + (recent ? " recent" : "") + grow + '" style="height:' + h + 'px' + delay + '" title="' +
          esc(localHour(i)) + ' — ' + b.docs + ' doc' + (b.docs === 1 ? "" : "s") +
          (b.corrections ? ", " + b.corrections + " correction" + (b.corrections === 1 ? "" : "s") : "") + '"></div>';
      }).join("");
      html += '<div class="nerd-block">' +
        '<div class="nb-head"><span class="section-label" style="margin:0" title="Documents processed per hour over the last 24. Hover a bar for its hour\'s count.">Docs per hour</span>' +
        '<span class="nb-hint">now ←</span></div>' +
        '<div class="bar-strip">' + bars + '</div>' +
        '<div class="bar-axis"><span>24h ago</span><span>12h</span><span>now</span></div></div>';

      // extraction + corrections-by-category blocks
      var lowPct = tt.fields_extracted ? pct(tt.fields_low_confidence / tt.fields_extracted) : "0%";
      var cats = tt.corrections_by_category;
      html += '<div class="nerd-cols">' +
        '<div class="nerd-block"><div class="section-label" style="margin-top:0">Extraction</div>' +
        nbRow("Fields extracted", '<span>' + tt.fields_extracted + '</span>',
          "Every field value the model returned across processed documents, last 24 hours.") +
        nbRow("Flagged low-confidence", '<span class="hl-chip">' + tt.fields_low_confidence + ' · ' + lowPct + '</span>',
          "Fields a deterministic check flagged for closer review — empty values, format oddities, handwritten documents, cross-field mismatches. Never a made-up probability.") +
        nbRow("Corrected by a reviewer", '<span style="color:var(--red)">' + tt.fields_corrected + ' · ' + pct(tt.correction_rate) + '</span>',
          "Fields where the human changed the model's value at confirm time. The red pen.") +
        '</div>' +
        '<div class="nerd-block"><div class="section-label" style="margin-top:0" title="Where the red pen lands: reviewer corrections grouped by what kind of field was wrong.">Corrections by field</div>' +
        nbRow("Dollar amounts", '<span>' + cats.money + '</span>',
          "Corrections to money fields — wages, withholding, interest, mortgage interest.") +
        nbRow("TIN / SSN digits", '<span>' + cats.tin_ssn + '</span>',
          "Corrections to SSN, TIN, or EIN digits — the highest-stakes field class.") +
        nbRow("Payer / employer names", '<span>' + cats.names + '</span>',
          "Corrections to names of people, employers, payers, lenders, or partnerships.") +
        nbRow("Document type (reclassified)", '<span>' + cats.doc_type + '</span>',
          "Documents a reviewer moved to a different type than the model's classification.") +
        '</div></div>';

      html += '<div class="nerd-foot">' +
        '<span class="hand-note" style="font-size:17px">the red-pen rate is the number to watch</span></div>';

      // Recent runs — the cross-run trace surface. One row per processed doc;
      // click to expand the exact per-call model I/O (reuses renderTrace).
      html += '<div class="nerd-block" id="runs-block">' +
        '<div class="nb-head"><span class="section-label" style="margin:0">Recent runs</span>' +
        '<span class="nb-hint">click a row to see what the model saw &amp; said</span></div>' +
        '<div id="runs-rows"><div class="rl-empty">Loading…</div></div></div>';

      // Silent 5s refresh rebuilds this subtree; the runs rows briefly become
      // "Loading…", the page shortens, and the browser clamps scroll to top —
      // a reader mid-scroll got yanked. Fix: LOCK the body's height through the
      // swap so the page never shortens (scroll can't clamp), then release once
      // the async runs rows have landed and restored the natural height.
      var nb = $("nerds-body");
      var sy = animate ? null : window.scrollY;
      if (sy !== null) nb.style.minHeight = nb.offsetHeight + "px";
      nb.innerHTML = html;
      if (sy !== null) window.scrollTo(0, sy);
      if (animate) countUpTiles(nb);
      renderRuns(sy);
    }).catch(function (e) {
      $("nerds-body").innerHTML = '<div class="rl-empty">Timeline unavailable (' + esc(e.message) + '). Needs backend /stats/timeline or mock mode.</div>';
    });
  }

  // rAF count-up for headline tiles — parses the numeric lead of each tile,
  // preserving its suffix (%, s) and decimal places. Entry-only (guarded by the
  // caller) and self-guards prefers-reduced-motion so it never fights the CSS switch.
  function countUpTiles(root) {
    if (!root) return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    root.querySelectorAll(".tile-num").forEach(function (el) {
      var m = /^(\d+(?:\.\d+)?)(.*)$/.exec(el.textContent.trim());
      if (!m) return;
      var target = parseFloat(m[1]);
      if (!isFinite(target) || target <= 0) return;
      var suffix = m[2] || "";
      var decimals = (m[1].split(".")[1] || "").length;
      var dur = 500, start = null;
      el.textContent = (0).toFixed(decimals) + suffix;
      requestAnimationFrame(function frame(ts) {
        if (start === null) start = ts;
        var p = Math.min(1, (ts - start) / dur);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = (target * eased).toFixed(decimals) + suffix;
        if (p < 1) requestAnimationFrame(frame);
        else el.textContent = target.toFixed(decimals) + suffix;
      });
    });
  }

  // Recent-runs table: newest processed docs, each expandable to its full trace.
  function renderRuns(restoreScrollY) {
    var host = $("runs-rows");
    if (!host || !api.getRuns) { if (host) host.innerHTML = ''; return; }
    // restoreScrollY: on the silent 5s refresh, once the rows have landed the
    // natural height is back — release the height lock and re-pin the scroll.
    function repin() {
      if (restoreScrollY == null) return;
      var nb = $("nerds-body");
      if (nb) nb.style.minHeight = "";
      if (state.view === "nerds") window.scrollTo(0, restoreScrollY);
    }
    api.getRuns(20).then(function (r) {
      var runs = (r && r.runs) || [];
      if (!runs.length) { host.innerHTML = '<div class="rl-empty">No runs yet — process a document to see it here.</div>'; return; }
      host.innerHTML = runs.map(function (run) {
        var typeLabel = run.doc_type === "UNRECOGNIZED" ? "Unrecognized" : (run.doc_type || "—");
        var stages = (run.stages || []).length
          ? (run.stages || []).join(" · ")
          : "no trace recorded";
        var model = esc((run.model_runtime || "") + " · " + (run.model_name || ""));
        var lat = run.latency_s != null ? esc(run.latency_s + "s") : "—";
        var retryTag = run.retried ? '<span class="run-retry">retried</span>' : "";
        return '<div class="run-row" data-id="' + esc(String(run.doc_id || "")) +
          '" data-raw="' + (run.raw_available ? "1" : "0") + '">' +
          '<button class="run-head" type="button" aria-expanded="false">' +
          '<span class="run-caret">▸</span>' +
          '<span class="run-type">' + esc(typeLabel) + ' <span class="run-id tnum">' + esc(String(run.doc_id || "")) + '</span></span>' +
          '<span class="run-stages">' + esc(stages) + retryTag + '</span>' +
          '<span class="run-model tnum">' + model + '</span>' +
          '<span class="run-lat tnum">' + lat + '</span>' +
          '</button>' +
          '<div class="run-trace" hidden><div class="trace-body">Loading…</div></div>' +
          '</div>';
      }).join("");

      host.querySelectorAll(".run-row").forEach(function (row) {
        var head = row.querySelector(".run-head");
        var panel = row.querySelector(".run-trace");
        var body = row.querySelector(".trace-body");
        var id = row.dataset.id;
        function loadTrace() {
          if (row.dataset.raw === "0") {
            // No raws on disk for this run (seeded/older doc) — say so plainly
            // rather than fetch a trace the backend would 404 on.
            body.innerHTML = '<div class="rl-empty">No model trace was recorded for this document.</div>';
          } else if (state.runTraceCache[id]) {
            // Traces are immutable once a doc is processed; serve the cached
            // render so the 5s telemetry refresh never re-flickers an open row.
            body.innerHTML = state.runTraceCache[id];
          } else {
            renderTrace(id, body).then(function () {   // reuse the one trace renderer
              state.runTraceCache[id] = body.innerHTML;
            });
          }
        }
        function setOpen(open) {
          row.classList.toggle("open", open);
          panel.hidden = !open;
          head.setAttribute("aria-expanded", open ? "true" : "false");
          row.querySelector(".run-caret").textContent = open ? "▾" : "▸";
          if (open) {
            state.expandedRuns[id] = true;
            if (!row.dataset.loaded) { row.dataset.loaded = "1"; loadTrace(); }
          } else {
            delete state.expandedRuns[id];
          }
        }
        head.addEventListener("click", function () { setOpen(!row.classList.contains("open")); });
        if (state.expandedRuns[id]) setOpen(true);   // survive the 5s live refresh
      });
      repin();
    }).catch(function (e) {
      host.innerHTML = '<div class="rl-empty">Runs unavailable (' + esc(e.message) + '). Needs backend /runs or mock mode.</div>';
      repin();
    });
  }

  // tip: plain-language definition of the measure, rendered as a native title
  // tooltip (zero-risk, works everywhere; the dotted label underline is the cue).
  function tile(num, label, tone, tip) {
    var cls = tone === "blue" ? " style=\"color:var(--ink-blue)\"" : tone === "red" ? " style=\"color:var(--red)\"" : "";
    return '<div class="tile"' + (tip ? ' title="' + esc(tip) + '"' : '') + '><div class="tile-num tnum"' + cls + '>' + num + '</div>' +
      '<div class="tile-lbl">' + esc(label) + '</div></div>';
  }
  function nbRow(label, valueHtml, tip) {
    return '<div class="nb-row"' + (tip ? ' title="' + esc(tip) + '"' : '') + '><span class="nb-lbl">' + esc(label) + '</span>' +
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
    // Add Client FAB is static markup — bind once here, not per dashboard render.
    var fab = $("fab-new-client");
    if (fab) fab.onclick = openNewClientForm;
    // Logo/wordmark click = home (home IS the dashboard).
    var brand = document.querySelector(".brand");
    if (brand) { brand.style.cursor = "pointer"; brand.onclick = function () { show("dashboard"); }; }

    // preload clients so review/dashboard have names
    api.getClients().then(function (cs) { state.clients = cs; });

    // starting view: hash route first (refresh-stable), then legacy ?view=,
    // then the dashboard — the screen the user lives in.
    var h = (location.hash || "").replace("#", "");
    var v = h || new URLSearchParams(location.search).get("view");
    show(v && $("view-" + v) ? v : "dashboard");
    window.addEventListener("hashchange", function () {
      var hv = (location.hash || "").replace("#", "");
      if (hv && $("view-" + hv) && hv !== state.view) show(hv);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
