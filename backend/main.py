"""KeepBook backend — FastAPI, on-device tax-document intake.

Implements docs/API.md exactly, on port 8100. All model access goes through
backend/model_runtime.py via backend/pipeline.py. State is a single JSON file
(state.json) rewritten after every mutation, so restart == demo-safe.

Run:  uvicorn main:app --port 8100      (from backend/, venv active)
"""

import base64
import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import stat as stat_mod
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile

import dedup
import pdf_render
import pipeline
from model_runtime import generate_text as model_generate_text

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Real source dir, captured once. Tests monkeypatch BASE_DIR to a tmp dir; the
# git-sha stamp must still resolve against the actual repo, so keep it separate.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
EVENTS_PATH = os.path.join(BASE_DIR, "events.jsonl")
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "frontend"))

os.makedirs(UPLOADS_DIR, exist_ok=True)

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Watched-inbox eligibility is intentionally NARROWER than IMAGE_EXTS: only the
# four common scan/photo formats are auto-ingested from a dropped folder. gif/bmp
# are still accepted on an explicit /intake upload but ignored by the watcher.
INBOX_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
INBOX_POLL_SECONDS = 2.0

# The watcher's log lines (ineligible-file skips, ingests, rejects, scan faults)
# must actually surface. uvicorn configures only its own loggers and leaves the root
# at WARNING, so a bare INFO here would be swallowed. Give this one logger its own
# stderr handler at INFO (idempotent across the test-suite's module reloads) so the
# "log line only" ignore path and the ambient-ingest lines are visible in the same
# stream (stderr -> uvicorn.log) as everything else, without touching global config.
log = logging.getLogger("keepbook.inbox")
if not log.handlers:
    _inbox_log_handler = logging.StreamHandler()
    _inbox_log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    log.addHandler(_inbox_log_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

# ---------------------------------------------------------------------------
# In-memory state, guarded by a single lock. Serialized to STATE_PATH.
# documents/clients are dicts keyed by id (insertion order preserved).
# queue/processing are runtime-only (rebuilt from status on load).
# ---------------------------------------------------------------------------
STATE_LOCK = threading.RLock()
EVENTS_LOCK = threading.Lock()
WAKE = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_event(event: dict) -> None:
    """Append one pipeline event to events.jsonl (docs/API.md "Event log")."""
    row = {"ts": _now_iso(), **event}
    line = json.dumps(row) + "\n"
    with EVENTS_LOCK:
        with open(EVENTS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)

STATE = {
    "documents": {},   # id -> Document
    "clients": {},     # id -> Client
    "seq_doc": 0,
    "seq_client": 0,
    # Watched-inbox re-ingest guard (ROADMAP Phase 2, Tier A #3): content sha256 ->
    # {name, outcome, doc_id?, at, error?}. Persisted so a restart never re-ingests
    # a file already handled, and a rejected (zero-byte/undecodable) file is not
    # retried forever. Keyed on CONTENT, so the same bytes under a new filename are
    # skipped while a new file reusing an old NAME (different content) still ingests.
    "inbox_seen": {},
}
QUEUE = []             # pending doc ids awaiting processing
PROCESSING = None      # id currently being processed, or None

# Watched-inbox config + runtime state. INBOX_DIR is resolved once at startup from
# the KEEPBOOK_INBOX env var (None = feature off, zero behavior change). The two
# dicts below are RUNTIME-ONLY (never persisted) and touched only by the single
# watcher thread, so they need no lock:
#   _INBOX_STABLE — name -> (st_size, st_mtime_ns) last seen; a file is ingested
#                   only once this is UNCHANGED across two consecutive polls
#                   (partial-write / mid-AirDrop safety).
#   _INBOX_DONE   — name -> (st_size, st_mtime_ns) at which we last handled the
#                   file; a read-skip cache so a stable, already-ingested file is
#                   not re-read every poll. Uses mtime_ns so a same-name rewrite
#                   (new content) misses the cache and is re-evaluated.
INBOX_DIR = None
_INBOX_STOP = threading.Event()
_INBOX_STABLE = {}
_INBOX_DONE = {}

# Session-scoped exact-match index for duplicate detection: sha256 -> doc_id.
# In-memory only (never persisted — would change the pinned Document shape) and
# liveness-checked at lookup, so a stale entry pointing at a deleted/absent doc is
# harmless and just falls through to the perceptual (dHash) comparison. After a
# restart it starts empty; a byte-identical re-upload is still flagged because a
# literal duplicate has dHash distance 0, and phash IS persisted. Cleared in
# _load_state (fresh process semantics).
SHA_INDEX = {}

# Config stamp for /health — resolved once at startup (docs/IMPROVEMENTS.md #12).
STARTED_AT = None
GIT_SHA = None


def _git_sha() -> str:
    """Short HEAD sha of the source repo, or 'unknown' if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_SRC_DIR,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - health must never fail on a git hiccup
        return "unknown"


def _reset_processing() -> None:
    """Clear PROCESSING under the lock. Used by the worker's failure guard."""
    global PROCESSING
    with STATE_LOCK:
        PROCESSING = None


def _persist_locked() -> None:
    """Write STATE atomically. Caller must hold STATE_LOCK."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(STATE, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _load_state() -> None:
    """Load STATE from disk and rebuild the processing queue from status."""
    global QUEUE, PROCESSING
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        STATE["documents"] = data.get("documents", {})
        STATE["clients"] = data.get("clients", {})
        STATE["seq_doc"] = data.get("seq_doc", 0)
        STATE["seq_client"] = data.get("seq_client", 0)
        # Additive key; old state files without it load fine (default empty).
        STATE["inbox_seen"] = data.get("inbox_seen", {})
    else:
        STATE["inbox_seen"] = {}
    # Fresh process / reload: forget the runtime stability + read-skip caches so the
    # watcher re-observes the folder from scratch (inbox_seen, loaded above, still
    # prevents re-ingest of anything already handled).
    _INBOX_STABLE.clear()
    _INBOX_DONE.clear()
    # Any document still "pending" was never finished -> re-enqueue.
    QUEUE = [
        doc_id
        for doc_id, doc in STATE["documents"].items()
        if doc.get("status") == "pending"
    ]
    PROCESSING = None
    # Fresh process: the exact-match index does not survive a restart (dedup still
    # works via persisted phash — a literal duplicate is dHash distance 0).
    SHA_INDEX.clear()
    if QUEUE:
        WAKE.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _next_doc_id() -> str:
    STATE["seq_doc"] += 1
    return f"doc_{STATE['seq_doc']:03d}"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return slug or "client"


def _next_client_id(name: str) -> str:
    base = "client_" + _slugify(name)
    cid = base
    n = 2
    while cid in STATE["clients"]:
        cid = f"{base}_{n}"
        n += 1
    return cid


def _abs_image_path(doc: dict) -> str:
    ip = doc.get("image_path")
    if not ip:
        return ""
    return ip if os.path.isabs(ip) else os.path.join(BASE_DIR, ip)


def _wrap_fields(plain: dict, retried: bool = False) -> dict:
    """Turn {key: value_str} into the stored {key: {value, corrected}} shape.

    Adds "low_confidence": true only when a deterministic signal fires
    (docs/API.md) — never a fake probability, and only present when true.
    """
    out = {}
    for k, v in plain.items():
        field = {"value": v, "corrected": False}
        if pipeline.field_low_confidence(k, v, retried):
            field["low_confidence"] = True
        out[k] = field
    return out


def _write_raws(
    doc_id: str, calls: list, doc: dict, latency: float = None, retried: bool = False
) -> str:
    """Persist the exact per-call model I/O for one document (auditability).

    Returns the event-facing reference path (relative to backend/).
    """
    raws_dir = os.path.join(BASE_DIR, "raws")
    os.makedirs(raws_dir, exist_ok=True)
    rel = os.path.join("raws", f"{doc_id}.json")
    record = {
        "doc_id": doc_id,
        "ts": _now_iso(),
        "model_runtime": os.environ.get("MODEL_RUNTIME", "ollama"),
        "model_name": os.environ.get("MODEL_NAME", "gemma4:e4b"),
        "status": doc.get("status"),
        "doc_type": doc.get("doc_type"),
        "latency_s": latency,
        "retried": retried,
        "calls": calls,
    }
    with open(os.path.join(BASE_DIR, rel), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return rel


# ---------------------------------------------------------------------------
# Background worker — sequential, one document at a time.
# ---------------------------------------------------------------------------
def _worker_loop() -> None:
    """Drain the queue forever. Each step is guarded so a persist/IO failure
    records a worker_error event instead of killing the daemon (IMPROVEMENTS #9)."""
    while True:
        WAKE.wait(timeout=1.0)
        try:
            _worker_step()
        except Exception as exc:  # noqa: BLE001 - the worker must never die
            _reset_processing()
            try:
                _append_event({"type": "worker_error", "error": str(exc)})
            except Exception:  # noqa: BLE001 - best-effort signal only
                pass


def _worker_step() -> None:
    global PROCESSING
    with STATE_LOCK:
        if not QUEUE:
            WAKE.clear()
            doc_id = None
        else:
            doc_id = QUEUE.pop(0)
            PROCESSING = doc_id
            doc = STATE["documents"].get(doc_id)
            img_path = _abs_image_path(doc) if doc else ""
    if doc_id is None:
        return

    result = None
    error = None
    t0 = time.time()
    # Raw-I/O capture: wrap the pipeline's bound model hook for the duration
    # of this one document so every call's exact prompt + raw response is
    # recorded (incl. retries). Worker is the only sequential model caller;
    # the original binding (real adapter or a test fake) is restored in
    # finally BEFORE the doc reaches a terminal status.
    calls = []
    orig_extract = pipeline.model_extract

    def _capturing_extract(image_b64, prompt, *args, **kwargs):
        # Stage label for this call. There are no tool calls and no chain-of-
        # thought here, so the truthful trace unit is the pipeline STAGE that
        # issued the call; the pipeline publishes it in pipeline._capture_stage
        # right before calling. Snapshot it now (synchronous, single worker).
        cap = getattr(pipeline, "_capture_stage", None) or {}
        stage = cap.get("stage")
        retry_call = bool(cap.get("retry"))
        entry = {"seq": len(calls) + 1, "prompt": prompt}
        if stage is not None:
            entry["stage"] = stage
        if retry_call:
            entry["retry"] = True
        try:
            resp = orig_extract(image_b64, prompt, *args, **kwargs)
        except Exception as exc:
            entry["error"] = str(exc)
            calls.append(entry)
            raise
        entry["response"] = resp
        calls.append(entry)
        return resp

    pipeline.model_extract = _capturing_extract
    try:
        with open(img_path, "rb") as fh:
            img_b64 = base64.b64encode(fh.read()).decode()
        result = pipeline.run_pipeline(img_b64)
    except Exception as exc:  # noqa: BLE001 - never let the worker die
        error = str(exc)
    finally:
        pipeline.model_extract = orig_extract
    latency = round(time.time() - t0, 2)

    # Compute the terminal values, PERSIST THE RAW TRACE, and only then flip the
    # observable status. A reader keying off status (the /trace endpoint) must
    # never see "extracted" before the matching raws/<id>.json is on disk —
    # otherwise it can serve a stale trace from an earlier document.
    retried = bool(result.get("retried")) if result else False
    if result is None:
        # A raised model/IO call (unreachable model, timeout) is an OUTAGE, not
        # honest confusion — distinct "error" status + machine-readable error,
        # so the UI shows the red banner, never the "unrecognized" copy
        # (IMPROVEMENTS #1).
        new_status, new_doc_type, new_fields, new_error = (
            "error",
            pipeline.UNRECOGNIZED,
            {},
            error,
        )
    else:
        new_status = result["status"]
        new_doc_type = result["doc_type"]
        new_fields = _wrap_fields(result["fields"], result.get("retried"))
        new_error = None

    try:
        raw_ref = _write_raws(
            doc_id,
            calls,
            {"status": new_status, "doc_type": new_doc_type},
            latency,
            retried,
        )
    except OSError:
        raw_ref = None

    low_conf_count = 0
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is not None:
            doc["status"] = new_status
            doc["doc_type"] = new_doc_type
            doc["fields"] = new_fields
            if new_error is not None:
                doc["error"] = new_error
            else:
                doc.pop("error", None)
            low_conf_count = sum(
                1 for f in doc["fields"].values() if f.get("low_confidence")
            )
        PROCESSING = None
        _persist_locked()

    # Event log (append-only; drives /stats/timeline).
    if doc is not None:
        fields_total = len(result["fields"]) if result else 0
        ev_type = (
            new_status
            if new_status in ("extracted", "unrecognized", "error")
            else "extracted"
        )
        event = {
            "type": ev_type,
            "doc_id": doc_id,
            "doc_type": new_doc_type,
            "latency_s": latency,
            "fields_total": fields_total,
            "fields_low_confidence": low_conf_count,
            "retried": retried,
            "re_asks": int(result.get("re_asks", 0)) if result else 0,
            "preprocessed": pipeline._preprocess_enabled(),
            "model": os.environ.get("MODEL_NAME", "gemma4:e4b"),
            "raw_ref": raw_ref,
        }
        if new_error is not None:
            event["error"] = new_error
        _append_event(event)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="KeepBook", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_stale_frontend(request: Request, call_next):
    """Force revalidation on the app shell (html/js/css/manifest).

    The no-build frontend changed many times today and Chrome's heap cache kept
    serving stale app.js against fresh index.html — buttons that render but do
    nothing. no-cache still allows 304s via ETag, so this costs one conditional
    request, not a re-download. Images keep default caching.
    """
    response = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".html", ".js", ".css", ".json")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
def _startup() -> None:
    global STARTED_AT, GIT_SHA, INBOX_DIR
    STARTED_AT = _now_iso()
    GIT_SHA = _git_sha()
    with STATE_LOCK:
        _load_state()
    t = threading.Thread(target=_worker_loop, name="keepbook-worker", daemon=True)
    t.start()

    # Watched inbox (ROADMAP Phase 2, Tier A #3). Unset KEEPBOOK_INBOX = feature
    # fully off, zero behavior change. Set = create the folder if missing (parents
    # ok) and start a stdlib polling thread that mirrors the worker's lifecycle.
    raw_inbox = os.environ.get("KEEPBOOK_INBOX")
    if raw_inbox and raw_inbox.strip():
        INBOX_DIR = os.path.abspath(os.path.expanduser(raw_inbox.strip()))
        try:
            os.makedirs(INBOX_DIR, exist_ok=True)
        except OSError:
            log.exception("inbox: could not create %r; watcher disabled", INBOX_DIR)
            INBOX_DIR = None
        else:
            _INBOX_STOP.clear()
            wt = threading.Thread(
                target=_inbox_watch_loop, name="keepbook-inbox", daemon=True
            )
            wt.start()
            log.info("inbox: watching %s every %ss", INBOX_DIR, INBOX_POLL_SECONDS)


@app.on_event("shutdown")
def _shutdown() -> None:
    # Signal the watcher thread to stop (daemon threads die with the process, but a
    # clean stop keeps repeated in-process TestClient starts/stops tidy).
    _INBOX_STOP.set()


@app.get("/health")
async def health():
    """Config stamp — one curl proves which code/model a process is serving."""
    return {
        "status": "ok",
        "model_runtime": os.environ.get("MODEL_RUNTIME", "ollama"),
        "model_name": os.environ.get("MODEL_NAME", "gemma4:e4b"),
        "preprocess": pipeline._preprocess_enabled(),
        "git_sha": GIT_SHA,
        "started_at": STARTED_AT,
        # Additive: the watched-inbox path, or null when the feature is off.
        "inbox": INBOX_DIR,
    }


# ---------------------------- Intake ---------------------------------------
def _detect_duplicate_locked(sha: str, phash: str, data: bytes) -> str:
    """Nearest CONFIRMED duplicate of a new image, or None. Caller holds lock.

    Exact path: sha256 hit in SHA_INDEX (verified still live) — identical bytes
    are a duplicate by definition, no further check. Perceptual path (two-stage,
    see backend/dedup.py): stage 1 collects existing docs within dedup.THRESHOLD
    Hamming distance of the 256-bit dHash (nearest first); stage 2 confirms each
    candidate by full-resolution pixel difference against its stored image, and
    the first confirmed candidate wins. Stage 2 is what tells "re-encoded same
    form" (flag) apart from "different person's same-type form" (no flag) — the
    hash alone cannot (both sit at distance 0 on template forms).

    Migration: a doc whose stored phash length differs from the current scheme
    (legacy 64-bit state files) gets its phash recomputed here from its stored
    upload when readable — persisted by the caller's _persist_locked — else it is
    skipped for comparison (find_candidates drops length mismatches; never a
    crash, never a false flag). The new document is NOT in STATE yet, so it can
    never match itself.
    """
    exact_id = SHA_INDEX.get(sha)
    if exact_id and exact_id in STATE["documents"]:
        return exact_id
    candidates = []
    for did, d in STATE["documents"].items():
        ph = d.get("phash")
        if ph and len(ph) != dedup.PHASH_HEX_LEN:
            recomputed = dedup.dhash_from_file(_abs_image_path(d))
            if recomputed:
                d["phash"] = recomputed  # lazy upgrade, persisted with this intake
                ph = recomputed
            else:
                continue  # unreadable legacy doc: skip, never false-flag
        candidates.append((did, ph))
    for did, _dist in dedup.find_candidates(phash, candidates):
        doc = STATE["documents"].get(did)
        if doc and dedup.is_pixel_duplicate(data, _abs_image_path(doc)):
            return did
    return None


def _create_doc_locked(
    data: bytes,
    orig_name: str,
    sha: str,
    phash: str,
    source: str = "upload",
    page_number: int = None,
    source_file: str = None,
) -> dict:
    """Create a pending Document from raw image bytes. Caller holds STATE_LOCK.

    The single shared intake code path: the HTTP /intake endpoint (images AND
    rendered PDF pages) and the watched-inbox thread all land here, so validation
    (done by the caller via dedup.compute_hashes), dup-flagging, queueing, and the
    uploads/ COPY are never duplicated. sha/phash are precomputed by the intake
    validator, which already rejected zero-byte/undecodable uploads before any doc
    is made.

    The bytes are WRITTEN (copied) into uploads/<doc_id>.<ext>; the caller's source
    file — an upload buffer or a file sitting in the watched folder — is never moved
    or deleted. `source` is stamped onto the doc only for the watcher ("folder"); an
    /intake upload leaves the key absent (absent == "upload", see docs/API.md).

    For a page rendered from a PDF, page_number (1-based) and source_file (the
    original PDF filename) are set — additive fields, absent on image uploads —
    so a multi-page PDF's pages group visibly and continuation pages can be filed
    by hand.
    """
    ext = os.path.splitext(orig_name or "")[1].lower()
    if ext not in IMAGE_EXTS:
        ext = ".png"
    duplicate_of = _detect_duplicate_locked(sha, phash, data)
    doc_id = _next_doc_id()
    rel_path = os.path.join("uploads", f"{doc_id}{ext}")
    with open(os.path.join(BASE_DIR, rel_path), "wb") as fh:
        fh.write(data)
    doc = {
        "id": doc_id,
        "client_id": None,
        "status": "pending",
        "doc_type": None,
        "image_path": rel_path,
        "received_at": _now_iso(),
        "fields": {},
        "source_name": orig_name or None,
        "phash": phash,
        "duplicate_of": duplicate_of,
    }
    if source == "folder":
        doc["source"] = "folder"
    if page_number is not None:
        doc["page_number"] = page_number
    if source_file is not None:
        doc["source_file"] = source_file
    STATE["documents"][doc_id] = doc
    QUEUE.append(doc_id)
    SHA_INDEX[sha] = doc_id
    return doc


async def _save_bytes(
    data: bytes,
    orig_name: str,
    sha: str,
    phash: str,
    page_number: int = None,
    source_file: str = None,
) -> dict:
    """Async thin wrapper over _create_doc_locked for the HTTP /intake path.

    Kept so the endpoint's call sites are unchanged; the doc carries no `source`
    key (an /intake upload is the implicit default). page_number/source_file are
    set for rendered PDF pages (see _prepare_upload)."""
    return _create_doc_locked(
        data,
        orig_name,
        sha,
        phash,
        source="upload",
        page_number=page_number,
        source_file=source_file,
    )


def _hash_or_400(data: bytes, label: str):
    """Compute (sha256, dHash) or raise HTTP 400 for a zero-byte/undecodable image.

    IMPROVEMENTS #14: a zero-byte or non-image upload is rejected here, BEFORE any
    document is created — the intake never silently accepts an unreadable file.
    """
    try:
        return dedup.compute_hashes(data)
    except dedup.UnreadableImage as exc:
        raise HTTPException(400, f"{label}: {exc}")


# ---------------------- Watched inbox folder (ROADMAP Phase 2, Tier A #3) --------
def _inbox_eligible_ext(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in INBOX_EXTS


def _inbox_scan_once(inbox: str = None) -> list:
    """One polling pass over the watched inbox folder. Returns new doc ids.

    Stdlib-only. Ingests top-level, non-hidden png/jpg/jpeg/webp files that have
    been (size, mtime)-stable across two consecutive scans, routing each through
    the SAME code path as POST /intake (dedup.compute_hashes validation + dup-flag
    + queue) and COPYING it into uploads/ — the original in the inbox is never
    moved, deleted, or renamed. Re-ingest is prevented by content sha256 persisted
    in state.json (inbox_seen), so a restart, or the same bytes under a new name,
    never ingests twice; a new file that merely reuses an old NAME (new content)
    still ingests. Zero-byte / undecodable files are remembered as rejected so they
    are not retried forever. Raises only on a listdir failure of the inbox itself
    (surfaced to the loop's guard); per-file IO errors are swallowed.
    """
    inbox = inbox or INBOX_DIR
    if not inbox or not os.path.isdir(inbox):
        return []
    entries = sorted(os.listdir(inbox))  # top-level only; no os.walk / recursion

    # Phase 1 (no lock): pick files that are stable-and-unseen, read + hash them.
    candidates = []  # (name, data, sha_raw, cur_stat)
    for name in entries:
        if name.startswith("."):  # hidden / dotfiles (.DS_Store, partial temp names)
            continue
        path = os.path.join(inbox, name)
        try:
            st = os.stat(path)
        except OSError:
            _INBOX_STABLE.pop(name, None)
            continue
        if not stat_mod.S_ISREG(st.st_mode):  # dirs, dir-symlinks, fifos -> skip
            continue
        cur = (st.st_size, st.st_mtime_ns)
        if not _inbox_eligible_ext(name):
            # Ignored silently, one log line per newly-seen (name, stat).
            if _INBOX_DONE.get(name) != cur:
                log.info("inbox: ignoring ineligible file %r", name)
                _INBOX_DONE[name] = cur
            continue
        if _INBOX_DONE.get(name) == cur:
            continue  # already handled this exact file; skip the re-read
        prev = _INBOX_STABLE.get(name)
        _INBOX_STABLE[name] = cur
        if prev != cur:
            continue  # not (size,mtime)-stable across two consecutive polls yet
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        candidates.append((name, data, hashlib.sha256(data).hexdigest(), cur))

    if not candidates:
        return []

    # Phase 2 (under lock): dedup by content sha, validate, create docs, persist.
    new_ids = []
    flagged = []   # (doc_id, duplicate_of)
    rejected = []  # (name, error)
    with STATE_LOCK:
        seen = STATE["inbox_seen"]
        for name, data, sha_raw, cur in candidates:
            if sha_raw in seen:
                _INBOX_DONE[name] = cur  # e.g. same content re-dropped under a new name
                continue
            try:
                sha, phash = dedup.compute_hashes(data)
            except dedup.UnreadableImage as exc:
                seen[sha_raw] = {
                    "name": name, "outcome": "rejected",
                    "at": _now_iso(), "error": str(exc),
                }
                _INBOX_DONE[name] = cur
                rejected.append((name, str(exc)))
                continue
            doc = _create_doc_locked(data, name, sha, phash, source="folder")
            seen[sha_raw] = {
                "name": name, "outcome": "ingested",
                "doc_id": doc["id"], "at": _now_iso(),
            }
            _INBOX_DONE[name] = cur
            new_ids.append(doc["id"])
            if doc.get("duplicate_of"):
                flagged.append((doc["id"], doc["duplicate_of"]))
        if new_ids or rejected:
            _persist_locked()

    # Events + wake OUTSIDE the lock (mirrors /intake). Additive event types; the
    # /stats/timeline aggregation ignores unknown types, so it is unaffected.
    for name, err in rejected:
        log.warning("inbox: rejected %r (%s)", name, err)
        _append_event({"type": "inbox_rejected", "name": name, "error": err})
    for doc_id in new_ids:
        log.info("inbox: ingested %s", doc_id)
        _append_event({"type": "inbox_ingested", "doc_id": doc_id})
    for doc_id, dup_of in flagged:
        _append_event({"type": "dup_flagged", "doc_id": doc_id, "duplicate_of": dup_of})
    if new_ids:
        WAKE.set()
    return new_ids


def _inbox_watch_loop() -> None:
    """Poll the watched inbox ~every INBOX_POLL_SECONDS until _INBOX_STOP is set.

    Defensive by construction: a scan raising anything (a permissions flip, a
    disappearing dir, a transient IO fault) is logged and swallowed so the watcher
    thread — and therefore the server — never dies (mirrors the worker's guard)."""
    while not _INBOX_STOP.is_set():
        try:
            _inbox_scan_once()
        except Exception:  # noqa: BLE001 - the watcher must never take down the server
            log.exception("inbox: scan failed; continuing")
        _INBOX_STOP.wait(INBOX_POLL_SECONDS)


def _prepare_upload(data: bytes, filename: str, password) -> list:
    """Expand one uploaded file into per-page intake items, validating everything
    BEFORE any document is created so a single bad file fails the whole request.

    Returns a list of (data, name, sha, phash, page_number, source_file):
      * an image yields exactly one item (page_number/source_file = None);
      * a PDF is rendered here to one PNG per page (each its own item, 1-based
        page_number, source_file = the PDF filename).

    Raises HTTPException 400 on any unreadable/oversized/encrypted-without-password
    file. Encrypted-PDF details use a machine-checkable prefix and carry NO
    password material — only the filename:
      * password_required:<filename>   — encrypted, no usable password given
      * password_incorrect:<filename>  — encrypted, wrong password given
    """
    if pdf_render.is_pdf(data, filename):
        try:
            pages = pdf_render.render_pdf_pages(data, filename, password)
        except pdf_render.PasswordRequired as exc:
            raise HTTPException(400, f"password_required:{exc.filename}")
        except pdf_render.PasswordIncorrect as exc:
            raise HTTPException(400, f"password_incorrect:{exc.filename}")
        except pdf_render.PdfTooManyPages as exc:
            raise HTTPException(400, str(exc))
        except pdf_render.PdfUnreadable as exc:
            raise HTTPException(400, str(exc))
        items = []
        for page_number, page_png in enumerate(pages, start=1):
            sha, phash = _hash_or_400(page_png, filename)
            items.append((page_png, filename, sha, phash, page_number, filename))
        return items

    # Non-PDF: must be a supported image type. Reject anything else loudly instead
    # of silently coercing it to .png and producing a fake "unrecognized".
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in IMAGE_EXTS:
        raise HTTPException(
            400,
            f"unsupported file type {ext or '(none)'!r} for {filename!r}; "
            "images (png, jpg, jpeg, webp, gif, bmp) or PDF (.pdf) only",
        )
    sha, phash = _hash_or_400(data, filename)
    return [(data, filename, sha, phash, None, None)]


@app.post("/intake")
async def intake(request: Request):
    ct = request.headers.get("content-type", "")
    queued = []
    flagged = []  # (doc_id, duplicate_of) pairs to log after the state write

    if ct.startswith("application/json"):
        body = await request.json()
        folder = body.get("folder")
        if not folder or not os.path.isdir(folder):
            raise HTTPException(400, f"folder not found: {folder!r}")
        # Optional password applies to any encrypted PDFs in the folder. Never
        # persisted/logged — passed only to the renderer (see _prepare_upload).
        password = body.get("password")
        accepted_exts = IMAGE_EXTS | {".pdf"}
        names = sorted(
            n
            for n in os.listdir(folder)
            if os.path.splitext(n)[1].lower() in accepted_exts
        )
        if not names:
            raise HTTPException(400, f"no images or PDFs in folder: {folder!r}")
        # Read + validate + render every file BEFORE any doc is created, so one bad
        # file (or a PDF needing a password) fails the whole request cleanly
        # instead of leaving half a batch. PDFs expand to one item per page here.
        prepared = []
        for n in names:
            with open(os.path.join(folder, n), "rb") as fh:
                data = fh.read()
            prepared.extend(_prepare_upload(data, n, password))
        with STATE_LOCK:
            for data, name, sha, phash, page_number, source_file in prepared:
                doc = await _save_bytes(
                    data, name, sha, phash, page_number, source_file
                )
                queued.append(doc["id"])
                if doc.get("duplicate_of"):
                    flagged.append((doc["id"], doc["duplicate_of"]))
            _persist_locked()
    else:
        form = await request.form()
        # Optional password form field applies to any PDFs in this request. Kept
        # only in memory for the render call — never persisted, logged, or echoed
        # in an error (see _prepare_upload / pdf_render). Guard against a client
        # sending a file under the "password" key.
        password = form.get("password")
        if isinstance(password, StarletteUploadFile):
            password = None
        # multi_items() keeps EVERY (key, value) pair — the built frontend sends
        # each file under a repeated "file" key (frontend/js/api.js), and
        # form.values() would collapse them to one. Field name pinned to "file"
        # per docs/API.md; other keys are rejected, not silently accepted.
        uploads = [
            v
            for k, v in form.multi_items()
            if k == "file" and isinstance(v, StarletteUploadFile)
        ]
        if not uploads:
            raise HTTPException(400, 'no files under multipart field "file"')
        # Read + validate + render every upload before creating any doc (see
        # above). Images stay one doc; PDFs expand to one item per rendered page.
        # Unsupported types are rejected loudly inside _prepare_upload.
        prepared = []
        for up in uploads:
            data = await up.read()
            prepared.extend(
                _prepare_upload(data, up.filename or "(unnamed)", password)
            )
        with STATE_LOCK:
            for data, name, sha, phash, page_number, source_file in prepared:
                doc = await _save_bytes(
                    data, name, sha, phash, page_number, source_file
                )
                queued.append(doc["id"])
                if doc.get("duplicate_of"):
                    flagged.append((doc["id"], doc["duplicate_of"]))
            _persist_locked()

    # Append a dup_flagged event per near/exact duplicate (additive; the timeline
    # aggregation ignores unknown event types, so /stats/timeline is unaffected).
    for doc_id, dup_of in flagged:
        _append_event({"type": "dup_flagged", "doc_id": doc_id, "duplicate_of": dup_of})

    WAKE.set()
    return {"queued": queued}


# ----------------------------- Queue ---------------------------------------
@app.get("/queue")
async def get_queue():
    with STATE_LOCK:
        # Count docs still "pending", NOT len(QUEUE): the in-flight document is
        # popped off QUEUE while it is being read, yet it is still pending until
        # the worker writes a terminal status. Counting status keeps that
        # in-flight doc visible so completion isn't declared a doc early
        # (IMPROVEMENTS #3).
        pending = sum(
            1 for d in STATE["documents"].values() if d.get("status") == "pending"
        )
        processing = PROCESSING
        done = sum(
            1
            for d in STATE["documents"].values()
            if d.get("status") in ("extracted", "unrecognized", "confirmed", "error")
        )
    return {"pending": pending, "processing": processing, "done": done}


# --------------------------- Documents -------------------------------------
@app.get("/documents")
async def get_documents():
    # Deep-copy under the lock: the returned docs are serialized by Starlette
    # AFTER this function exits, so handing out live dict references races the
    # worker mid-mutation (IMPROVEMENTS #10).
    with STATE_LOCK:
        return copy.deepcopy(list(STATE["documents"].values()))


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        return copy.deepcopy(doc)


@app.get("/documents/{doc_id}/trace")
async def get_document_trace(doc_id: str):
    """Serve the exact per-call model I/O captured for one document.

    The observability money-shot: one click answers "what did the model see
    and say?" (IMPROVEMENTS #4). Returns the raws/<id>.json record verbatim.

    Keyed on the raws file alone, NOT on state membership: /runs lists runs from
    the event log, which outlives state swaps (demo seed/fallback restores), so a
    run row must stay expandable even after its document left the current state.
    """
    raws_path = os.path.join(BASE_DIR, "raws", f"{doc_id}.json")
    if not os.path.exists(raws_path):
        raise HTTPException(404, f"no trace for {doc_id}")
    with open(raws_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/documents/{doc_id}/image")
async def get_document_image(doc_id: str):
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        path = _abs_image_path(doc)
    if not path or not os.path.exists(path):
        raise HTTPException(404, f"no image for {doc_id}")
    return FileResponse(path)


def _uncheck_client_item_locked(client_id, doc_type) -> None:
    """Count-aware checklist un-check. Caller holds STATE_LOCK and has ALREADY
    removed the doc (delete) or flipped it off "confirmed" (unconfirm), so this
    doc is not counted here. Pulls doc_type from the client's received_docs only
    when NO other confirmed doc of that type remains for the client — the single
    source of truth for the count-aware rule, shared by delete + unconfirm.
    """
    if not (client_id and doc_type and doc_type != pipeline.UNRECOGNIZED):
        return
    client = STATE["clients"].get(client_id)
    if client is None or doc_type not in client.get("received_docs", []):
        return
    still_have = any(
        d.get("client_id") == client_id
        and d.get("doc_type") == doc_type
        and d.get("status") == "confirmed"
        for d in STATE["documents"].values()
    )
    if not still_have:
        client["received_docs"] = [
            t for t in client["received_docs"] if t != doc_type
        ]


@app.post("/documents/{doc_id}/confirm")
async def confirm_document(doc_id: str, request: Request):
    body = await request.json()
    incoming = body.get("fields", {}) or {}

    # Identity is the human gate. Misassigning a document to the wrong client is
    # a confidentiality incident for a tax firm, so client identity must be an
    # affirmative act by the reviewer — never a silently-defaulted dropdown. The
    # affirmative "This document belongs to X" step is enforced in the Review UI
    # (the identity control starts UNCONFIRMED even when the pipeline pre-assigned
    # a client); the pinned /confirm contract still accepts client_id: null, so
    # enforcement lives in the frontend rather than here. page_number lets
    # continuation pages that carry no extractable name be filed by hand under a
    # client + page number. Additive: omitting it preserves the existing contract.
    page_number = body.get("page_number")
    if page_number is not None:
        # Accept an int or an integer-valued string/float (the frontend number
        # input surfaces its value as a string); reject everything else — a
        # truncated "1.5" or a stray "true" must fail loudly, not file silently.
        valid = False
        if isinstance(page_number, bool):
            valid = False
        elif isinstance(page_number, int):
            valid = page_number >= 1
        elif isinstance(page_number, float):
            valid = page_number.is_integer() and page_number >= 1
            if valid:
                page_number = int(page_number)
        elif isinstance(page_number, str) and page_number.strip().isdigit():
            page_number = int(page_number.strip())
            valid = page_number >= 1
        if not valid:
            raise HTTPException(400, "page_number must be an integer >= 1")

    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")

        old_type = doc.get("doc_type")
        manual_type_change = bool(body.get("doc_type")) and body["doc_type"] != old_type
        if body.get("doc_type"):
            doc["doc_type"] = body["doc_type"]
        if body.get("client_id") is not None:
            doc["client_id"] = body["client_id"]
        if page_number is not None:
            doc["page_number"] = page_number

        fields = doc.get("fields", {})
        corrected_keys = []
        for key, raw_val in incoming.items():
            new_val = "" if raw_val is None else str(raw_val)
            cur = fields.get(key, {"value": "", "corrected": False})
            baseline = cur.get("original_value") if cur.get("corrected") else cur.get("value", "")
            if new_val != str(cur.get("value", "")):
                fields[key] = {
                    "value": new_val,
                    "corrected": True,
                    "original_value": baseline,
                }
                # A field the reviewer corrected is no longer low-confidence.
                fields[key].pop("low_confidence", None)
                corrected_keys.append(key)
        doc["fields"] = fields
        doc["status"] = "confirmed"

        # Checklist: a confirmed doc_type joins the client's received_docs.
        client = STATE["clients"].get(doc.get("client_id"))
        if client is not None:
            dt = doc.get("doc_type")
            if dt and dt != pipeline.UNRECOGNIZED and dt not in client["received_docs"]:
                client["received_docs"].append(dt)

        _persist_locked()
        final = json.loads(json.dumps(doc))  # snapshot for use outside the lock

    _append_event({
        "type": "confirmed",
        "doc_id": doc_id,
        "doc_type": final.get("doc_type"),
        "fields_corrected": len(corrected_keys),
        "corrected_keys": corrected_keys,
        "manual_type_change": manual_type_change,
    })
    return final


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    """Remove a document (an erroneous ingest) from state.

    Count-aware checklist un-check: a confirmed doc_type is only pulled from the
    client's received_docs when NO other confirmed doc of that type remains for
    the client. Persists under the same lock + atomic _persist_locked pattern as
    /confirm; logs a "deleted" event; best-effort deletes the uploaded image and
    the raws/<id>.json trace (never fails the request over IO).
    """
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")

        was_confirmed = doc.get("status") == "confirmed"
        client_id = doc.get("client_id")
        doc_type = doc.get("doc_type")
        image_abs = _abs_image_path(doc)

        del STATE["documents"][doc_id]
        if doc_id in QUEUE:
            QUEUE.remove(doc_id)

        # Clear dangling duplicate_of references: any doc that flagged THIS doc as
        # its original just lost its target, so drop the flag (set null) — a
        # duplicate_of must never point at a document that no longer exists.
        for other in STATE["documents"].values():
            if other.get("duplicate_of") == doc_id:
                other["duplicate_of"] = None

        # Un-check the client's checklist item only if this was the last confirmed
        # document of its type for that client (count-aware). Shared with unconfirm.
        if was_confirmed:
            _uncheck_client_item_locked(client_id, doc_type)

        _persist_locked()

    # Best-effort cleanup of on-disk artifacts — never fail the request over IO.
    for path in (image_abs, os.path.join(BASE_DIR, "raws", f"{doc_id}.json")):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    _append_event({"type": "deleted", "doc_id": doc_id, "doc_type": doc_type})
    return {"deleted": doc_id}


@app.post("/documents/{doc_id}/unconfirm")
async def unconfirm_document(doc_id: str):
    """Re-open a confirmed document for correction (the inverse of /confirm).

    Only valid on a "confirmed" doc (409 otherwise). Flips status back to
    "extracted" while PRESERVING doc_type, client_id, page_number, and every
    field exactly as the confirmed state had them — including corrections
    (corrected: true + original_value) — so re-confirm is one click and prior
    edits aren't lost. Un-checks the client's checklist item with the same
    count-aware rule as delete (a checklist item survives while another confirmed
    doc of that type remains). Appends an "unconfirmed" event; persists atomically.
    """
    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        if doc.get("status") != "confirmed":
            raise HTTPException(
                409, f"document {doc_id} is not confirmed (status: {doc.get('status')})"
            )

        client_id = doc.get("client_id")
        doc_type = doc.get("doc_type")

        # Flip off "confirmed" FIRST so the count-aware un-check does not count
        # this doc as a remaining confirmed doc of its type. Fields/corrections/
        # doc_type/client_id are left untouched — a re-confirm is one click.
        doc["status"] = "extracted"
        _uncheck_client_item_locked(client_id, doc_type)

        _persist_locked()
        final = json.loads(json.dumps(doc))  # snapshot for use outside the lock

    _append_event({"type": "unconfirmed", "doc_id": doc_id, "doc_type": doc_type})
    return final


@app.post("/documents/{doc_id}/resolve-duplicate")
async def resolve_duplicate(doc_id: str, request: Request):
    """Human verdict on a flagged near/exact duplicate: keep it (not a dup).

    Body {"action": "keep"} clears duplicate_of (sets null), persists, and appends
    a dup_resolved event. To DISCARD the extra copy instead, the caller uses the
    existing DELETE /documents/{id} — there is no second delete path here. Unknown
    action -> 400; unknown id -> 404. A doc that carries no flag is a NO-OP 200
    (returns the doc unchanged, appends no event) so a double-click or a stale UI
    is idempotent and safe (documented in docs/API.md).
    """
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    action = (body or {}).get("action")
    if action != "keep":
        raise HTTPException(400, 'action must be "keep"')

    with STATE_LOCK:
        doc = STATE["documents"].get(doc_id)
        if doc is None:
            raise HTTPException(404, f"no document {doc_id}")
        if not doc.get("duplicate_of"):
            # No flag to clear — idempotent no-op. Nothing changed, so no event.
            return copy.deepcopy(doc)
        doc["duplicate_of"] = None
        _persist_locked()
        final = json.loads(json.dumps(doc))  # snapshot for use outside the lock

    _append_event({"type": "dup_resolved", "doc_id": doc_id, "action": "keep"})
    return final


# ---------------------------- Clients --------------------------------------
@app.get("/clients")
async def get_clients():
    with STATE_LOCK:
        return list(STATE["clients"].values())


@app.post("/clients")
async def create_client(request: Request):
    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(400, "client name required")
    expected = body.get("expected_docs", []) or []
    with STATE_LOCK:
        cid = _next_client_id(name)
        client = {
            "id": cid,
            "name": name,
            "expected_docs": list(expected),
            "received_docs": [],
        }
        STATE["clients"][cid] = client
        STATE["seq_client"] += 1
        _persist_locked()
        return client


@app.patch("/clients/{client_id}")
async def update_client(client_id: str, request: Request):
    """Edit a client's name and/or expected_docs (CRUD-AUDIT gaps #2/#4).

    Partial update: only the keys present in the body are touched. The client id
    is NEVER regenerated on rename — documents reference client_id, so a rename
    that minted a new id would silently orphan every filed document. name (when
    present) must be non-empty; expected_docs (when present) is a FULL REPLACE of
    the list, deduped with order preserved. received_docs is left untouched, so a
    checklist edit never disturbs what the client has actually sent.
    """
    body = await request.json()
    with STATE_LOCK:
        client = STATE["clients"].get(client_id)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")

        if "name" in body:
            name = body.get("name")
            if not name or not str(name).strip():
                raise HTTPException(400, "client name must be non-empty")
            client["name"] = name

        if "expected_docs" in body:
            raw = body.get("expected_docs")
            if not isinstance(raw, list):
                raise HTTPException(400, "expected_docs must be a list of strings")
            seen = set()
            deduped = []
            for item in raw:
                s = str(item)
                if s not in seen:
                    seen.add(s)
                    deduped.append(s)
            client["expected_docs"] = deduped

        _persist_locked()
        final = json.loads(json.dumps(client))  # snapshot for use outside the lock

    _append_event({"type": "client_updated", "client_id": client_id})
    return final


@app.delete("/clients/{client_id}")
async def delete_client(client_id: str):
    """Remove a client (a duplicate/test client) — guarded against orphaning.

    The safe default: refuse with 409 when ANY document still references the
    client, so a delete can never leave documents pointing at a client that no
    longer exists. The error body reports the document count and points the
    reviewer at the remedy (reassign or discard those documents first). An
    unreferenced client is removed under the same lock/persist/event pattern as
    the other mutations. 404 on an unknown id.
    """
    with STATE_LOCK:
        client = STATE["clients"].get(client_id)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")

        ref_count = sum(
            1 for d in STATE["documents"].values() if d.get("client_id") == client_id
        )
        if ref_count > 0:
            raise HTTPException(
                409,
                detail={
                    "error": f"client has {ref_count} document"
                    + ("" if ref_count == 1 else "s")
                    + " referencing it",
                    "document_count": ref_count,
                    "hint": "reassign or discard those documents first",
                },
            )

        del STATE["clients"][client_id]
        _persist_locked()

    _append_event({"type": "client_deleted", "client_id": client_id})
    return {"deleted": client_id}


# ------------------------ Nudge (missing-doc reminder draft) ---------------
# Visible-autonomy feature: the model DRAFTS a short reminder note listing the
# client's still-missing checklist items; the human copies + sends it by hand
# (no email integration, no send button anywhere — see docs/API.md).
NUDGE_MAX_CHARS = 900


def _nudge_template(client_name: str, missing: list) -> str:
    """Deterministic fallback draft — same content guarantees as the model
    path (greets by name, lists every missing doc verbatim, nothing invented).
    """
    lines = [
        f"Hi {client_name},",
        "",
        "As we prepare your tax return, we're still missing the following "
        "document" + ("" if len(missing) == 1 else "s") + ":",
        "",
    ]
    for m in missing:
        lines.append(f"- {m}")
    lines.append("")
    lines.append("Could you send " + ("this" if len(missing) == 1 else "these") + " over when you have a moment?")
    lines.append("")
    lines.append("Thank you.")
    return "\n".join(lines)


def _nudge_prompt(client_name: str, missing: list) -> str:
    doc_list = "\n".join(f"- {m}" for m in missing)
    return (
        "You are drafting a short, professional reminder note from a tax "
        f"preparation firm to a client named {client_name}.\n"
        "Greet the client by name. Then list EXACTLY these missing documents, "
        f"verbatim, one per line, nothing added or removed:\n{doc_list}\n"
        "Ask them to send those documents in. Say nothing else.\n"
        "HARD RULES: never invent a deadline, dollar amount, or fee that was "
        "not given to you. Never use a placeholder like [Firm Name], [Date], "
        "or [Your Name] — if a detail is unknown, leave it out entirely. Keep "
        "the whole note under 900 characters. Return ONLY the note text, no "
        "preamble, no markdown, no subject line."
    )


def _nudge_draft_ok(draft: str, client_name: str, missing: list) -> bool:
    """Post-check on the model's output (docs/API.md "Nudge draft").

    Deterministic gate, no probabilities: must name the client, must name
    every missing document verbatim, must not carry a bracket placeholder,
    must not be empty or over-length. Any failure -> caller falls back to the
    template, never a 500.
    """
    if not draft or len(draft) > NUDGE_MAX_CHARS:
        return False
    if "[" in draft:
        return False
    if client_name and client_name not in draft:
        return False
    for m in missing:
        if m not in draft:
            return False
    return True


@app.get("/clients/{client_id}/nudge")
def get_client_nudge(client_id: str):
    """Draft a "still waiting on" reminder for one client's missing checklist
    items. Complete client -> draft: null. Never 500s on a model failure or a
    misbehaving model output — falls back to the deterministic template.

    Deliberately a plain `def`, not `async def`: the model call below is a
    blocking synchronous urllib request (~5-25s on this host). Every other
    model call in this backend happens on the dedicated worker thread,
    off FastAPI's event loop. A plain `def` endpoint is run by
    Starlette/FastAPI in its threadpool (`run_in_threadpool`), so this one
    blocking call can't freeze the whole server (e.g. /queue polling) for its
    duration the way it would inside an `async def` route.
    """
    with STATE_LOCK:
        client = STATE["clients"].get(client_id)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")
        client_name = client.get("name", "") or ""
        received = set(client.get("received_docs", []) or [])
        missing = [t for t in client.get("expected_docs", []) or [] if t not in received]

    if not missing:
        return {"client_id": client_id, "missing": [], "draft": None}

    generated_by = "template"
    draft = None
    try:
        raw = model_generate_text(_nudge_prompt(client_name, missing))
        candidate = (raw or "").strip()
        if _nudge_draft_ok(candidate, client_name, missing):
            draft = candidate
            generated_by = "model"
    except Exception:  # noqa: BLE001 - a model outage must never break the draft
        pass

    if draft is None:
        draft = _nudge_template(client_name, missing)

    _append_event({
        "type": "nudge_drafted",
        "client_id": client_id,
        "generated_by": generated_by,
    })
    return {
        "client_id": client_id,
        "missing": missing,
        "draft": draft,
        "generated_by": generated_by,
    }


# Human-readable field labels for the CSV export's field_label column. Mirrors
# frontend/js/app.js FIELD_LABELS so an exported sheet reads the same as the UI.
# Unknown keys fall back to the raw snake_case key (see _field_label).
_CSV_FIELD_LABELS = {
    "employer": "Employer", "ein": "EIN", "employee_name": "Employee", "ssn": "SSN",
    "box1_wages": "Wages (Box 1)", "box2_fed_withheld": "Fed. tax withheld (Box 2)",
    "payer": "Payer", "recipient": "Recipient", "recipient_tin": "Recipient TIN",
    "box1_interest": "Interest income (Box 1)", "box4_fed_withheld": "Fed. tax withheld (Box 4)",
    "box1_nonemployee_comp": "Nonemployee comp. (Box 1)",
    "lender": "Lender", "borrower": "Borrower", "borrower_tin": "Borrower TIN",
    "box1_mortgage_interest": "Mortgage interest (Box 1)",
    "recipient_name": "Recipient", "box1_interest_income": "Interest income (Box 1)",
    "box3_other_income": "Other income (Box 3)",
    "partnership_name": "Partnership", "partner_name": "Partner",
    "partnership_ein": "Partnership EIN", "ordinary_income": "Ordinary income",
    "borrower_name": "Borrower",
}

_CSV_COLUMNS = [
    "client_id", "client_name", "doc_id", "doc_type", "received_at",
    "field_key", "field_label", "value", "corrected", "original_value",
    "low_confidence",
]


def _field_label(key: str) -> str:
    return _CSV_FIELD_LABELS.get(key, key)


@app.get("/clients/{client_id}/export.csv")
async def export_client_csv(client_id: str):
    """Flat CSV of one client's CONFIRMED documents — one row per field.

    Integration surface: anything that imports CSV (spreadsheets, ledgers, tax
    prep) can read this today. Only confirmed docs are exported; a corrected
    field ships its corrected value plus the original_value it replaced, so the
    correction provenance survives the hand-off. See docs/API.md "CSV export".
    """
    with STATE_LOCK:
        client = STATE["clients"].get(client_id)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")
        client_name = client.get("name", "")
        rows = []
        for doc in STATE["documents"].values():
            if doc.get("client_id") != client_id or doc.get("status") != "confirmed":
                continue
            doc_id = doc.get("id", "")
            doc_type = doc.get("doc_type", "")
            received_at = doc.get("received_at", "")
            fields = doc.get("fields") or {}
            if not fields:
                # Classify-only docs (extract: false, e.g. charitable receipt)
                # carry no fields; without this they'd export zero rows and
                # vanish from the sheet. Emit one "document received" row so the
                # confirmed doc still appears. See docs/API.md "CSV export".
                rows.append([
                    client_id,
                    client_name,
                    doc_id,
                    doc_type,
                    received_at,
                    "document",
                    "Document received",
                    doc_type,
                    "false",
                    "",
                    "false",
                ])
                continue
            for key, field in fields.items():
                corrected = bool(field.get("corrected"))
                rows.append([
                    client_id,
                    client_name,
                    doc_id,
                    doc_type,
                    received_at,
                    key,
                    _field_label(key),
                    field.get("value", ""),
                    "true" if corrected else "false",
                    field.get("original_value", "") if corrected else "",
                    "true" if field.get("low_confidence") else "false",
                ])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{client_id}.csv"'},
    )


# ----------------------------- Stats ---------------------------------------
@app.get("/stats")
async def get_stats():
    with STATE_LOCK:
        extracted = 0
        corrected = 0
        for d in STATE["documents"].values():
            if d.get("status") == "unrecognized":
                continue
            for f in (d.get("fields") or {}).values():
                extracted += 1
                if f.get("corrected"):
                    corrected += 1
    rate = round(corrected / extracted, 4) if extracted else 0
    return {
        "fields_extracted": extracted,
        "fields_corrected": corrected,
        "correction_rate": rate,
    }


# ---------------------- Timeline (stretch: Stats for Nerds) ----------------
def _read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    events = []
    with EVENTS_LOCK:
        with open(EVENTS_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    return events


def _parse_ts(ts: str):
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


@app.get("/stats/timeline")
async def stats_timeline(hours: int = 24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = []
    for e in _read_events():
        dt = _parse_ts(e.get("ts", ""))
        if dt is not None and dt >= cutoff:
            e["_dt"] = dt
            events.append(e)

    processed = [e for e in events if e.get("type") in ("extracted", "unrecognized")]
    confirms = [e for e in events if e.get("type") == "confirmed"]

    # Hourly buckets: exactly `hours` zero-filled entries, one per hour, oldest
    # first, ending at the current hour (contract "one per hour, oldest first").
    now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_hour = now_hour - timedelta(hours=hours - 1)
    counts = {}  # absolute hour datetime -> {"docs": n, "corrections": n}
    for e in processed:
        key = e["_dt"].replace(minute=0, second=0, microsecond=0)
        counts.setdefault(key, {"docs": 0, "corrections": 0})["docs"] += 1
    for e in confirms:
        key = e["_dt"].replace(minute=0, second=0, microsecond=0)
        counts.setdefault(key, {"docs": 0, "corrections": 0})["corrections"] += int(
            e.get("fields_corrected", 0)
        )
    bucket_list = []
    for offset in range(hours):
        hour_dt = start_hour + timedelta(hours=offset)
        c = counts.get(hour_dt, {"docs": 0, "corrections": 0})
        bucket_list.append({
            "hour": hour_dt.strftime("%H:00"),
            "docs": c["docs"],
            "corrections": c["corrections"],
        })

    docs_processed = len(processed)
    fields_extracted = sum(int(e.get("fields_total", 0)) for e in processed)
    fields_low_confidence = sum(int(e.get("fields_low_confidence", 0)) for e in processed)
    fields_corrected = sum(int(e.get("fields_corrected", 0)) for e in confirms)
    manual_changes = sum(1 for e in confirms if e.get("manual_type_change"))
    latencies = sorted(
        float(e["latency_s"]) for e in processed if e.get("latency_s") is not None
    )

    if latencies:
        mid = len(latencies) // 2
        median_lat = (
            latencies[mid]
            if len(latencies) % 2
            else (latencies[mid - 1] + latencies[mid]) / 2
        )
        # Nearest-rank p95 over the same sorted latencies.
        p95_lat = latencies[min(len(latencies) - 1, math.ceil(0.95 * len(latencies)) - 1)]
    else:
        median_lat = 0
        p95_lat = 0

    # All four categories always present, zero-filled (contract).
    by_cat = {"money": 0, "tin_ssn": 0, "names": 0, "doc_type": 0}
    for e in confirms:
        for key in e.get("corrected_keys", []) or []:
            cat = pipeline.correction_category(key)
            by_cat[cat] = by_cat.get(cat, 0) + 1
        if e.get("manual_type_change"):
            by_cat["doc_type"] += 1

    totals = {
        "docs_processed": docs_processed,
        "fields_extracted": fields_extracted,
        "fields_low_confidence": fields_low_confidence,
        "fields_corrected": fields_corrected,
        "correction_rate": round(fields_corrected / fields_extracted, 4)
        if fields_extracted
        else 0,
        "first_try_type_acc": round((len(confirms) - manual_changes) / len(confirms), 4)
        if confirms
        else 0,
        "median_latency_s": round(median_lat, 2),
        "p95_latency_s": round(p95_lat, 2),
        "corrections_by_category": by_cat,
    }
    return {"hours": hours, "buckets": bucket_list, "totals": totals}


# ----------------------- Runs (cross-run trace surface) --------------------
def _read_run_raws(raw_ref):
    """Load a raws/<id>.json trace by its event-recorded ref. Returns the parsed
    record or None (missing ref, missing/corrupt file — the seeded-state edge
    case). Best-effort: observability must never fail over IO."""
    if not raw_ref:
        return None
    path = os.path.join(BASE_DIR, str(raw_ref))
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _stage_summary(calls):
    """Per-call stage labels for a run, in call order. Falls back to "call N"
    for any call written before stage labels existed (old raws)."""
    summary = []
    for i, c in enumerate(calls or []):
        stage = c.get("stage")
        summary.append(stage if stage else "call " + str(c.get("seq", i + 1)))
    return summary


@app.get("/runs")
async def get_runs(limit: int = 20):
    """Recent processed documents, newest first — the cross-run trace surface.

    One row per processed doc (extracted / unrecognized events), enriched from
    the raws/<id>.json trace when it is on disk. Seeded/older docs have no raws
    file: those rows report raw_available=false with a null call_count and empty
    stages, and still render. Model output itself is served per-doc by the
    existing GET /documents/<id>/trace; this endpoint is the index over it.
    """
    n = max(1, min(int(limit), 100))
    processed = [
        e for e in _read_events() if e.get("type") in ("extracted", "unrecognized")
    ]
    # Events are appended in processing order (chronological), so reverse gives
    # newest-first — the true order docs were handled, not a 1s-resolution ts sort.
    processed.reverse()

    runs = []
    for e in processed[:n]:
        raws = _read_run_raws(e.get("raw_ref"))
        if raws is not None:
            calls = raws.get("calls") or []
            model_runtime = raws.get("model_runtime")
            call_count = len(calls)
            stages = _stage_summary(calls)
            raw_available = True
        else:
            # No trace on disk (seeded/older doc). We don't know the call count
            # or per-call stages; the event still carries the run's headline.
            model_runtime = os.environ.get("MODEL_RUNTIME", "ollama")
            call_count = None
            stages = []
            raw_available = False
        runs.append({
            "doc_id": e.get("doc_id"),
            "doc_type": e.get("doc_type"),
            "status": e.get("type"),
            "model_runtime": model_runtime,
            "model_name": e.get("model"),
            "latency_s": e.get("latency_s"),
            "preprocessed": e.get("preprocessed"),
            "retried": bool(e.get("retried", False)),
            "call_count": call_count,
            "stages": stages,
            "raw_available": raw_available,
        })
    return {"runs": runs}


# --------------------- Static frontend (mount LAST) ------------------------
# API routes above win; the SPA + assets are served from "/" for everything else.
if os.path.isdir(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
else:
    @app.get("/")
    async def _root():
        return JSONResponse({"service": "KeepBook", "frontend": "not found"})
