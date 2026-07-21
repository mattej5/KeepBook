"""Contract tests for the watched intake folder (ROADMAP Phase 2, Tier A #3 / T70).

The watcher is a stdlib polling thread that ingests files dropped into
KEEPBOOK_INBOX through the SAME code path as POST /intake, COPYING (never moving)
each file into uploads/. These tests drive the scan function directly
(`_inbox_scan_once`) so stability/dedup behavior is exercised deterministically
without sleeping through real poll intervals.

Covers: off-by-default (no env -> no thread, /health inbox null); eligible file
ingested exactly once; (size,mtime) stability delays a growing file; junk
extension ignored; zero-byte remembered + not retried; restart no-reingest;
same-content-new-name no-reingest; same-name-new-content ingests; original file
never deleted/moved; old state file without inbox_seen still loads; the watch loop
swallows a scan fault (server stays up).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _png(w: int, h: int, color) -> bytes:
    """A small, decodable PNG. Distinct (w,h,color) -> distinct bytes + dHash."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def mod():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    return importlib.import_module("main")


@pytest.fixture()
def env(mod, tmp_path, monkeypatch):
    """A clean, isolated main module rooted at a tmp dir, plus a scratch inbox.

    Resets every piece of module-global watcher/pipeline state between tests so the
    shared `main` singleton never bleeds across cases.
    """
    root = tmp_path / "backend"
    (root / "uploads").mkdir(parents=True)
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    monkeypatch.setattr(mod, "BASE_DIR", str(root))
    monkeypatch.setattr(mod, "UPLOADS_DIR", str(root / "uploads"))
    monkeypatch.setattr(mod, "STATE_PATH", str(root / "state.json"))
    monkeypatch.setattr(mod, "EVENTS_PATH", str(root / "events.jsonl"))
    monkeypatch.setattr(mod, "INBOX_DIR", None)
    monkeypatch.delenv("KEEPBOOK_INBOX", raising=False)
    monkeypatch.setenv("PREPROCESS", "0")

    with mod.STATE_LOCK:
        mod.STATE.clear()
        mod.STATE.update({
            "documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0,
            "inbox_seen": {},
        })
        mod.QUEUE.clear()
        mod.PROCESSING = None
        mod.WAKE.clear()
        mod.SHA_INDEX.clear()
        mod._INBOX_STABLE.clear()
        mod._INBOX_DONE.clear()
        mod._INBOX_STOP.set()  # ensure no loop from a prior test is running
    yield mod, str(inbox), root
    mod._INBOX_STOP.set()


def _events(root):
    p = Path(root) / "events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x] if p.exists() else []


def _scan(mod, inbox):
    return mod._inbox_scan_once(inbox)


# --------------------------------------------------------------------------- #
# off by default
# --------------------------------------------------------------------------- #
def test_watcher_off_by_default_no_thread_and_health_inbox_null(mod, monkeypatch, tmp_path):
    """Unset KEEPBOOK_INBOX => no watcher thread, INBOX_DIR None, /health inbox null."""
    monkeypatch.delenv("KEEPBOOK_INBOX", raising=False)
    root = tmp_path / "b"
    (root / "uploads").mkdir(parents=True)
    monkeypatch.setattr(mod, "BASE_DIR", str(root))
    monkeypatch.setattr(mod, "UPLOADS_DIR", str(root / "uploads"))
    monkeypatch.setattr(mod, "STATE_PATH", str(root / "state.json"))
    monkeypatch.setattr(mod, "EVENTS_PATH", str(root / "events.jsonl"))
    monkeypatch.setattr(mod, "INBOX_DIR", None)  # fresh-process import default

    with TestClient(mod.app) as client:
        # No env -> startup starts no watcher thread and leaves INBOX_DIR None.
        assert mod.INBOX_DIR is None
        assert client.get("/health").json()["inbox"] is None
        assert "keepbook-inbox" not in {t.name for t in threading.enumerate()}


def test_health_reports_inbox_path_when_set(mod, monkeypatch, tmp_path):
    inbox = tmp_path / "watch-me"
    root = tmp_path / "b2"
    (root / "uploads").mkdir(parents=True)
    monkeypatch.setattr(mod, "BASE_DIR", str(root))
    monkeypatch.setattr(mod, "UPLOADS_DIR", str(root / "uploads"))
    monkeypatch.setattr(mod, "STATE_PATH", str(root / "state.json"))
    monkeypatch.setattr(mod, "EVENTS_PATH", str(root / "events.jsonl"))
    monkeypatch.setenv("KEEPBOOK_INBOX", str(inbox))
    try:
        with TestClient(mod.app) as client:
            # Folder is created at startup if missing.
            assert inbox.is_dir()
            assert client.get("/health").json()["inbox"] == str(inbox.resolve())
            assert "keepbook-inbox" in {t.name for t in threading.enumerate()}
    finally:
        mod._INBOX_STOP.set()
        monkeypatch.setattr(mod, "INBOX_DIR", None)


# --------------------------------------------------------------------------- #
# core ingestion + stability
# --------------------------------------------------------------------------- #
def test_eligible_file_ingested_exactly_once(env):
    mod, inbox, _root = env
    Path(inbox, "scan.png").write_bytes(_png(40, 30, (200, 60, 60)))

    assert _scan(mod, inbox) == []          # poll 1: seen, not yet stable
    ids = _scan(mod, inbox)                  # poll 2: stable -> ingest
    assert len(ids) == 1
    doc_id = ids[0]
    assert mod.STATE["documents"][doc_id]["source"] == "folder"

    # Further polls never re-ingest the same file.
    assert _scan(mod, inbox) == []
    assert _scan(mod, inbox) == []
    assert len(mod.STATE["documents"]) == 1


def test_stability_delays_a_growing_file(env):
    mod, inbox, _root = env
    p = Path(inbox, "growing.png")

    p.write_bytes(b"\x89PNG partial")        # not yet a valid/complete file
    assert _scan(mod, inbox) == []            # poll 1: record (size,mtime)
    assert len(mod.STATE["documents"]) == 0

    p.write_bytes(_png(50, 40, (30, 120, 200)))  # grew: new size + mtime
    assert _scan(mod, inbox) == []            # poll 2: changed -> still unstable
    assert len(mod.STATE["documents"]) == 0

    ids = _scan(mod, inbox)                    # poll 3: unchanged -> stable -> ingest
    assert len(ids) == 1
    assert len(mod.STATE["documents"]) == 1


def test_junk_extension_ignored(env):
    mod, inbox, _root = env
    Path(inbox, "notes.txt").write_text("not an image")
    Path(inbox, "archive.pdf").write_bytes(b"%PDF-1.4 ...")
    Path(inbox, "clip.gif").write_bytes(b"GIF89a")  # gif is NOT in INBOX_EXTS

    for _ in range(3):
        assert _scan(mod, inbox) == []
    assert mod.STATE["documents"] == {}
    assert mod.STATE["inbox_seen"] == {}   # ineligible files are never remembered


def test_hidden_file_ignored(env):
    mod, inbox, _root = env
    Path(inbox, ".DS_Store").write_bytes(_png(10, 10, (0, 0, 0)))
    for _ in range(3):
        assert _scan(mod, inbox) == []
    assert mod.STATE["documents"] == {}


def test_subdirectory_not_recursed(env):
    mod, inbox, _root = env
    sub = Path(inbox, "nested")
    sub.mkdir()
    Path(sub, "deep.png").write_bytes(_png(20, 20, (10, 10, 10)))
    for _ in range(3):
        assert _scan(mod, inbox) == []
    assert mod.STATE["documents"] == {}


# --------------------------------------------------------------------------- #
# rejection + not-retried-forever
# --------------------------------------------------------------------------- #
def test_zero_byte_remembered_and_not_retried(env):
    mod, inbox, root = env
    Path(inbox, "empty.png").write_bytes(b"")

    assert _scan(mod, inbox) == []            # poll 1: record
    assert _scan(mod, inbox) == []            # poll 2: stable -> reject
    assert mod.STATE["documents"] == {}

    empty_sha = hashlib.sha256(b"").hexdigest()
    assert mod.STATE["inbox_seen"][empty_sha]["outcome"] == "rejected"
    rejected_events = [e for e in _events(root) if e.get("type") == "inbox_rejected"]
    assert len(rejected_events) == 1

    # It is remembered, so subsequent polls do not retry (no second reject event).
    for _ in range(3):
        assert _scan(mod, inbox) == []
    rejected_events = [e for e in _events(root) if e.get("type") == "inbox_rejected"]
    assert len(rejected_events) == 1


def test_undecodable_image_remembered_and_not_retried(env):
    mod, inbox, root = env
    Path(inbox, "fake.png").write_bytes(b"this is not a real png")
    for _ in range(4):
        _scan(mod, inbox)
    assert mod.STATE["documents"] == {}
    rejected = [e for e in _events(root) if e.get("type") == "inbox_rejected"]
    assert len(rejected) == 1


# --------------------------------------------------------------------------- #
# re-ingest protection (content sha256)
# --------------------------------------------------------------------------- #
def test_same_content_new_name_not_reingested(env):
    mod, inbox, _root = env
    data = _png(40, 30, (90, 160, 40))
    Path(inbox, "a.png").write_bytes(data)
    _scan(mod, inbox); _scan(mod, inbox)
    assert len(mod.STATE["documents"]) == 1

    Path(inbox, "b.png").write_bytes(data)   # identical bytes, new filename
    _scan(mod, inbox); _scan(mod, inbox); _scan(mod, inbox)
    assert len(mod.STATE["documents"]) == 1  # skipped on content sha


def test_same_name_new_content_is_ingested(env):
    mod, inbox, _root = env
    p = Path(inbox, "reused.png")
    p.write_bytes(_png(40, 30, (200, 30, 30)))
    _scan(mod, inbox); _scan(mod, inbox)
    assert len(mod.STATE["documents"]) == 1

    # Same NAME, genuinely different content (different dims -> different size+sha).
    p.write_bytes(_png(64, 48, (30, 30, 200)))
    _scan(mod, inbox)                          # poll 1: stat changed -> record
    ids = _scan(mod, inbox)                    # poll 2: stable -> ingest new content
    assert len(ids) == 1
    assert len(mod.STATE["documents"]) == 2


# --------------------------------------------------------------------------- #
# the original file is NEVER moved / deleted / renamed
# --------------------------------------------------------------------------- #
def test_original_file_untouched_after_ingest(env):
    mod, inbox, root = env
    p = Path(inbox, "keepme.png")
    data = _png(40, 30, (120, 120, 20))
    p.write_bytes(data)
    _scan(mod, inbox); _scan(mod, inbox)

    assert len(mod.STATE["documents"]) == 1
    # The original still sits in the inbox, byte-identical, same name.
    assert p.exists()
    assert p.read_bytes() == data
    assert [x.name for x in Path(inbox).iterdir()] == ["keepme.png"]
    # And it was COPIED into uploads/.
    doc = next(iter(mod.STATE["documents"].values()))
    assert Path(root, doc["image_path"]).exists()


# --------------------------------------------------------------------------- #
# restart / persistence
# --------------------------------------------------------------------------- #
def _fresh_from_disk(mod, root, inbox):
    """A distinct main.py module instance reloading state.json from disk (restart)."""
    name = f"keepbook_inbox_fresh_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, BACKEND / "main.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.BASE_DIR = str(root)
    fresh.UPLOADS_DIR = str(root / "uploads")
    fresh.STATE_PATH = mod.STATE_PATH
    fresh.EVENTS_PATH = mod.EVENTS_PATH
    with fresh.STATE_LOCK:
        fresh._load_state()
    return fresh


def test_restart_does_not_reingest(env):
    mod, inbox, root = env
    Path(inbox, "once.png").write_bytes(_png(40, 30, (60, 60, 160)))
    _scan(mod, inbox); _scan(mod, inbox)
    assert len(mod.STATE["documents"]) == 1

    fresh = _fresh_from_disk(mod, root, inbox)
    assert len(fresh.STATE["documents"]) == 1          # persisted doc loaded
    assert fresh.STATE["inbox_seen"]                    # inbox_seen survived restart
    # The very same file is still in the folder; a fresh process must not re-ingest.
    fresh._inbox_scan_once(inbox)
    fresh._inbox_scan_once(inbox)
    fresh._inbox_scan_once(inbox)
    assert len(fresh.STATE["documents"]) == 1


def test_old_state_without_inbox_seen_loads(env):
    mod, inbox, root = env
    legacy = {
        "documents": {
            "doc_001": {
                "id": "doc_001", "client_id": None, "status": "confirmed",
                "doc_type": "W-2", "image_path": "uploads/doc_001.png",
                "received_at": "2026-07-01T00:00:00Z", "fields": {},
            }
        },
        "clients": {}, "seq_doc": 1, "seq_client": 0,
    }
    Path(mod.STATE_PATH).write_text(json.dumps(legacy))

    fresh = _fresh_from_disk(mod, root, inbox)
    assert fresh.STATE["inbox_seen"] == {}      # missing key defaults to empty
    # A drop into the inbox still ingests normally against a legacy state file.
    Path(inbox, "new.png").write_bytes(_png(40, 30, (10, 200, 10)))
    fresh._inbox_scan_once(inbox); fresh._inbox_scan_once(inbox)
    assert len(fresh.STATE["documents"]) == 2


# --------------------------------------------------------------------------- #
# the watcher never takes down the server
# --------------------------------------------------------------------------- #
def test_watch_loop_swallows_scan_faults(env, monkeypatch):
    mod, _inbox, _root = env
    calls = {"n": 0}

    def boom(inbox=None):
        calls["n"] += 1
        raise RuntimeError("induced scan fault")

    monkeypatch.setattr(mod, "_inbox_scan_once", boom)
    monkeypatch.setattr(mod, "INBOX_POLL_SECONDS", 0.01)
    mod._INBOX_STOP.clear()
    t = threading.Thread(target=mod._inbox_watch_loop, name="test-inbox-loop", daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 1.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        # The loop kept iterating despite every scan raising, and never died.
        assert calls["n"] >= 3
        assert t.is_alive()
    finally:
        mod._INBOX_STOP.set()
        t.join(timeout=1.0)
    assert not t.is_alive()
