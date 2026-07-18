/*
 * KeepBook CSV import — tolerant client-list parser.
 *
 * Pure, DOM-free, zero-dependency so it is directly require-able in node for
 * unit tests (app.js cannot be — it runs boot() and touches window at load).
 * Exposed as window.KeepBookCsv in the browser and module.exports in node.
 *
 * Contract (see docs/TASKS.md T69): a tolerant CSV -> [{name, expected_docs[]}].
 *   - column 1 = client name (required, non-empty after trim)
 *   - column 2 = expected docs, optional, semicolon- OR pipe-separated
 *   - quoted fields with embedded commas / doubled quotes ("") / newlines are honored
 *   - a leading header row is skipped when its first cell is name/client/client name
 *   - blank lines are skipped and counted
 * Returns { clients, skipped_blank, header_skipped }.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.KeepBookCsv = api;
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  // Character-level CSV tokenizer. Handles quoted fields (embedded commas,
  // newlines, and doubled-quote escapes), and \r\n / \r / \n row breaks.
  // A trailing newline does NOT emit a phantom empty row (so it never inflates
  // the "blank rows ignored" count); a genuine blank line in the middle does.
  function tokenize(text) {
    var rows = [];
    var row = [];
    var field = "";
    var inQuotes = false;
    var i = 0;
    var n = text.length;
    while (i < n) {
      var c = text.charAt(i);
      if (inQuotes) {
        if (c === '"') {
          if (text.charAt(i + 1) === '"') { field += '"'; i += 2; continue; }
          inQuotes = false; i++; continue;
        }
        field += c; i++; continue;
      }
      if (c === '"') { inQuotes = true; i++; continue; }
      if (c === ",") { row.push(field); field = ""; i++; continue; }
      if (c === "\r") {
        row.push(field); field = ""; rows.push(row); row = [];
        if (text.charAt(i + 1) === "\n") i += 2; else i++;
        continue;
      }
      if (c === "\n") {
        row.push(field); field = ""; rows.push(row); row = []; i++;
        continue;
      }
      field += c; i++;
    }
    // Flush the final field/row only if anything is pending — a file ending in a
    // newline leaves field="" and row=[], which we deliberately drop.
    if (field !== "" || row.length > 0) { row.push(field); rows.push(row); }
    return rows;
  }

  function cell(row, i) {
    var v = row.length > i ? row[i] : "";
    return v == null ? "" : String(v);
  }

  function isHeaderName(name) {
    var h = name.toLowerCase();
    return h === "name" || h === "client" || h === "client name";
  }

  function parseClientCsv(text, defaultDocs) {
    var base = Array.isArray(defaultDocs) ? defaultDocs : [];
    var rows = tokenize(String(text == null ? "" : text));
    var clients = [];
    var skipped_blank = 0;
    var header_skipped = false;
    var seenData = false;
    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      var allBlank = row.every(function (x) { return String(x == null ? "" : x).trim() === ""; });
      if (allBlank) { skipped_blank++; continue; }
      var name = cell(row, 0).trim();
      // Header detection: only the FIRST non-blank row, only once.
      if (!seenData && !header_skipped && isHeaderName(name)) {
        header_skipped = true;
        continue;
      }
      seenData = true;
      if (!name) continue; // name is required; a row with docs but no name is dropped
      var docs = cell(row, 1).split(/[;|]/).map(function (s) { return s.trim(); })
        .filter(function (s) { return s !== ""; });
      // A bare client list (name only) should still produce a useful checklist:
      // seed expected_docs from the firm's organizer template (a COPY, so the
      // caller's template array is never shared/mutated).
      if (!docs.length) docs = base.slice();
      clients.push({ name: name, expected_docs: docs });
    }
    return { clients: clients, skipped_blank: skipped_blank, header_skipped: header_skipped };
  }

  return { parseClientCsv: parseClientCsv };
});
