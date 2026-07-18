# CRUD Gap Audit (T63)

A written audit of KeepBook's create/read/update/delete surface across the
FastAPI backend (`backend/main.py`) and the static frontend (`frontend/js`).
Written to justify the one gap we closed for the demo — document delete — and to
name the rest honestly as post-demo backlog rather than pretend they exist.

## CRUD surface — what exists vs. what's missing

Legend: ✅ implemented · ⚠️ partial / indirect only · ❌ missing

| Entity | Operation | Backend | Frontend | Notes |
| --- | --- | --- | --- | --- |
| **Documents** | Create | ✅ `POST /intake` | ✅ Capture drop-zone → `intake()` | Images only; queues a pending doc per file. |
| | Read | ✅ `GET /documents`, `/documents/{id}`, `/{id}/trace`, `/{id}/image` | ✅ Review list + detail, model-trace disclosure | |
| | Update | ⚠️ `POST /documents/{id}/confirm` only | ⚠️ Confirm flow only | The *only* write path: sets `doc_type`, `client_id`, field corrections, status→confirmed. No general field edit outside confirm. |
| | Delete | ✅ `DELETE /documents/{id}` **(shipped today)** | ✅ "Discard this document" in Review detail **(shipped today)** | Removes doc, count-aware checklist un-check, `deleted` event, best-effort image + `raws/{id}.json` cleanup. |
| **Clients** | Create | ✅ `POST /clients` | ❌ no create-client UI | Clients arrive via seeded state / API; the UI can't add one. |
| | Read | ✅ `GET /clients` | ✅ Dashboard checklist cards | |
| | Update | ❌ | ❌ | Can't rename a client or edit its `expected_docs`. |
| | Delete | ❌ | ❌ | Can't remove a duplicate/test client. |
| **Checklists** (`expected_docs` / `received_docs`) | Create | ⚠️ at client creation only | ❌ | `expected_docs` fixed when the client is created. |
| | Read | ✅ derived in `/clients` | ✅ Dashboard rows (received / in-review / missing) | |
| | Update | ⚠️ `received_docs` auto-maintained by confirm (append) + delete (count-aware remove) | ⚠️ reflected, not directly editable | No way to hand-edit the expected list or manually tick/untick an item. |
| | Delete | ⚠️ implicit via document delete | ⚠️ implicit | Un-check is a side effect of deleting the underlying doc, not a first-class action. |
| **Corrections** (field-level) | Create/Update | ✅ via confirm (diff → `corrected: true` + `original_value`) | ✅ Review inputs | |
| | Read | ✅ stored on field; surfaced in `/stats`, `/stats/timeline` | ✅ strike-through render on confirmed docs | |
| | Revert / Delete | ❌ | ❌ | A saved correction can't be un-done; no re-open. |

## Known gaps (named in the DoD), each with a one-line risk note

1. **No document delete for erroneous ingests** — *CLOSED TODAY.* A junk scan
   (wrong file, blurry photo, mis-classified receipt) used to be stuck in Review
   forever. Now: `DELETE /documents/{id}` + a quiet "Discard" control.
2. **No client edit/delete** — a typo in a client name or a wrong `expected_docs`
   list is permanent, and a duplicate/test client can never be removed.
3. **No doc→client reassignment after confirm** — a document confirmed to the
   wrong client is stuck there; today the only remedy is discard + re-ingest.
4. **No `expected_docs` checklist editing** — the expected-forms list is frozen
   at client creation, so the checklist can't adapt when a client turns out to
   owe an unexpected form (a surprise K-1) or one fewer than planned.
5. **No un-confirm / re-open** — a wrongly-confirmed doc (bad field values or the
   wrong type) can't be reopened for correction; the reviewer must discard it and
   run it through intake again.

## What shipped today vs. honest backlog

**Shipped (T63):** `DELETE /documents/{id}` with the delete semantics pinned by
`backend/tests/test_delete_document.py` (200 + `{"deleted": id}`, 404 after,
count-aware checklist un-check, `deleted` event, persistence across restart, 404
on unknown id, extracted-doc delete leaves checklists untouched), plus a Review
"Discard this document" affordance with a `confirm()` dialog that names the doc
type and client, wired through both the live and mock API adapters.

**Post-demo backlog (in priority order):**

- **Un-confirm / re-open a confirmed doc** — highest value; removes the
  "delete-and-re-ingest" workaround for a single wrong field.
- **Doc→client reassignment** — move a confirmed doc between clients without
  losing its extracted fields.
- **Checklist editing** — add/remove an `expected_docs` entry per client after
  creation, and manual tick/untick.
- **Client edit + delete** — rename, fix `expected_docs`, remove test/dupe
  clients; guard delete against orphaning documents.
- **Client create UI** — today clients only arrive via seed/API; add an in-app
  "new client" form.
- **Correction revert** — undo a single saved field correction.
