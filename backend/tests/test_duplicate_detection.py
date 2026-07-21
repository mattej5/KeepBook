"""Contract tests for duplicate-submission detection (ROADMAP Phase 2 Tier A #1).

Covers: zero-byte/undecodable intake -> 400 (IMPROVEMENTS #14); exact-duplicate
flagged; near-duplicate (re-encoded, different bytes -> perceptual path) flagged;
distinct documents (cross-type AND the demo-critical same-type different-people
case) not flagged; resolve-duplicate clears + persists + events; delete clears
dangling duplicate_of; phash/duplicate_of survive restart; an old state file
without the new fields still loads; and legacy 16-hex (64-bit-scheme) phash
values are recomputed from the stored image or safely skipped.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
TESTSET = ROOT / "eval" / "testset"

# Real distinct forms from the eval set. w2_clean_01 is the base; k1_clean_01 is a
# DIFFERENT type (far outside the stage-1 prefilter, must not be flagged);
# w2_clean_02 is the SAME type but a different person (stage-1 hash COLLIDES at
# distance 0 — the stage-2 pixel confirm is what must reject it).
BASE_IMG = TESTSET / "w2_clean_01.png"
DISTINCT_IMG = TESTSET / "k1_clean_01.png"
SAME_TYPE_DISTINCT_IMG = TESTSET / "w2_clean_02.png"


def _reencoded(png_bytes: bytes) -> bytes:
    """A JPEG q70 re-encode of the same page: different BYTES, near-identical dHash
    (distance 1). Exercises the perceptual path, not the exact-sha256 shortcut."""
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return buf.getvalue()


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    module = importlib.import_module("main")
    root = tmp_path_factory.mktemp("dup-detection")
    (root / "uploads").mkdir()

    module.BASE_DIR = str(root)
    module.UPLOADS_DIR = str(root / "uploads")
    module.STATE_PATH = str(root / "state.json")
    module.EVENTS_PATH = str(root / "events.jsonl")

    with TestClient(module.app) as client:
        yield module, client, root


@pytest.fixture(autouse=True)
def clean_state(api):
    module, _client, root = api
    deadline = time.monotonic() + 2
    while module.PROCESSING is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    with module.STATE_LOCK:
        module.STATE.clear()
        module.STATE.update({"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0})
        module.QUEUE.clear()
        module.PROCESSING = None
        module.WAKE.clear()
        module.SHA_INDEX.clear()
        module._persist_locked()
    events = root / "events.jsonl"
    if events.exists():
        events.unlink()


@pytest.fixture(autouse=True)
def fake_model_adapter(api, monkeypatch):
    module, _client, _root = api
    runtime = importlib.import_module("model_runtime")
    # Skip cv2 preprocessing (dedup runs on the raw intake bytes, independent of it).
    monkeypatch.setenv("PREPROCESS", "0")

    def fake_extract(_image_b64, prompt, **_kwargs):
        if "classifier" in prompt:
            return json.dumps({"doc_type": "W-2"})
        return json.dumps({
            "employee_name": "Dup Test", "ssn": "111-22-3333",
            "employer": "KeepBook LLC", "box1_wages": "50000.00",
            "box2_fed_withheld": "5000.00",
        })

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _intake(client, name, data):
    return client.post("/intake", files=[("file", (name, data, "application/octet-stream"))])


def _events(root):
    path = root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


def _fresh_app_from_disk(module, root):
    """A distinct main.py module instance that reloads state.json from disk."""
    name = f"keepbook_fresh_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, BACKEND / "main.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.BASE_DIR = str(root)
    fresh.UPLOADS_DIR = str(root / "uploads")
    fresh.STATE_PATH = module.STATE_PATH
    fresh.EVENTS_PATH = module.EVENTS_PATH
    with fresh.STATE_LOCK:
        fresh._load_state()
    return fresh


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_zero_byte_upload_rejected_400_and_no_doc_created(api):
    _module, client, _root = api
    resp = _intake(client, "empty.png", b"")
    assert resp.status_code == 400
    assert client.get("/documents").json() == []


def test_pil_unreadable_upload_rejected_400_and_no_doc_created(api):
    _module, client, _root = api
    resp = _intake(client, "junk.png", b"this is not a PNG at all")
    assert resp.status_code == 400
    assert client.get("/documents").json() == []


def test_exact_duplicate_is_flagged_and_logs_event(api):
    _module, client, root = api
    data = BASE_IMG.read_bytes()
    first_id = _intake(client, "scan.png", data).json()["queued"][0]
    second_id = _intake(client, "scan_again.png", data).json()["queued"][0]

    import dedup

    first = client.get(f"/documents/{first_id}").json()
    second = client.get(f"/documents/{second_id}").json()
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first_id
    assert isinstance(second["phash"], str)
    assert len(second["phash"]) == dedup.PHASH_HEX_LEN  # 256-bit scheme: 64 hex

    ev = _events(root)
    assert any(
        e.get("type") == "dup_flagged" and e.get("doc_id") == second_id
        and e.get("duplicate_of") == first_id
        for e in ev
    )


def test_near_duplicate_reencoded_is_flagged(api):
    _module, client, _root = api
    data = BASE_IMG.read_bytes()
    reenc = _reencoded(data)
    assert reenc != data  # different bytes -> not an exact-sha match

    first_id = _intake(client, "emailed.png", data).json()["queued"][0]
    second_id = _intake(client, "photo.jpg", reenc).json()["queued"][0]

    second = client.get(f"/documents/{second_id}").json()
    assert second["duplicate_of"] == first_id


def test_distinct_documents_are_not_flagged(api):
    _module, client, _root = api
    a_id = _intake(client, "w2.png", BASE_IMG.read_bytes()).json()["queued"][0]
    b_id = _intake(client, "k1.png", DISTINCT_IMG.read_bytes()).json()["queued"][0]

    assert client.get(f"/documents/{a_id}").json()["duplicate_of"] is None
    assert client.get(f"/documents/{b_id}").json()["duplicate_of"] is None


def test_different_people_same_type_docs_are_not_flagged(api):
    """The demo-critical case: two DIFFERENT people's W-2s share a blank template,
    so their 256-bit dHashes collide at distance 0 (stage 1 passes them as
    candidates). The stage-2 pixel confirm must REJECT the pair — no flag."""
    _module, client, _root = api
    a_id = _intake(client, "w2_person_a.png", BASE_IMG.read_bytes()).json()["queued"][0]
    b_id = _intake(client, "w2_person_b.png", SAME_TYPE_DISTINCT_IMG.read_bytes()).json()["queued"][0]

    assert client.get(f"/documents/{a_id}").json()["duplicate_of"] is None
    assert client.get(f"/documents/{b_id}").json()["duplicate_of"] is None


def test_resolve_duplicate_keep_clears_flag_persists_and_logs(api):
    module, client, root = api
    data = BASE_IMG.read_bytes()
    first_id = _intake(client, "a.png", data).json()["queued"][0]
    dup_id = _intake(client, "b.png", data).json()["queued"][0]
    assert client.get(f"/documents/{dup_id}").json()["duplicate_of"] == first_id

    resolved = client.post(f"/documents/{dup_id}/resolve-duplicate", json={"action": "keep"})
    assert resolved.status_code == 200
    assert resolved.json()["duplicate_of"] is None

    # persisted to disk
    persisted = json.loads(Path(module.STATE_PATH).read_text())
    assert persisted["documents"][dup_id]["duplicate_of"] is None

    ev = _events(root)
    assert any(
        e.get("type") == "dup_resolved" and e.get("doc_id") == dup_id and e.get("action") == "keep"
        for e in ev
    )


def test_resolve_duplicate_edge_cases(api):
    _module, client, _root = api
    # unknown id -> 404
    assert client.post("/documents/nope/resolve-duplicate", json={"action": "keep"}).status_code == 404
    # bad action -> 400
    data = BASE_IMG.read_bytes()
    doc_id = _intake(client, "solo.png", data).json()["queued"][0]
    assert client.post(f"/documents/{doc_id}/resolve-duplicate", json={"action": "nope"}).status_code == 400
    # doc without a flag -> idempotent no-op 200
    ok = client.post(f"/documents/{doc_id}/resolve-duplicate", json={"action": "keep"})
    assert ok.status_code == 200
    assert ok.json()["duplicate_of"] is None


def test_deleting_original_clears_dangling_duplicate_of(api):
    module, client, _root = api
    data = BASE_IMG.read_bytes()
    first_id = _intake(client, "orig.png", data).json()["queued"][0]
    dup_id = _intake(client, "copy.png", data).json()["queued"][0]
    assert client.get(f"/documents/{dup_id}").json()["duplicate_of"] == first_id

    assert client.delete(f"/documents/{first_id}").status_code == 200

    assert client.get(f"/documents/{dup_id}").json()["duplicate_of"] is None
    persisted = json.loads(Path(module.STATE_PATH).read_text())
    assert persisted["documents"][dup_id]["duplicate_of"] is None


def test_phash_and_duplicate_of_survive_restart(api):
    module, client, root = api
    data = BASE_IMG.read_bytes()
    first_id = _intake(client, "one.png", data).json()["queued"][0]
    dup_id = _intake(client, "two.png", data).json()["queued"][0]
    before = client.get(f"/documents/{dup_id}").json()

    fresh = _fresh_app_from_disk(module, root)
    fresh_client = TestClient(fresh.app)
    after = fresh_client.get(f"/documents/{dup_id}").json()
    assert after["phash"] == before["phash"]
    assert after["duplicate_of"] == first_id


def test_old_state_file_without_new_fields_still_loads(api):
    module, client, root = api
    # A pre-feature state file: a confirmed doc with NO phash / duplicate_of keys.
    # image_path deliberately points at a file that NEVER exists in this suite so
    # the absent-phash recompute deterministically fails and the doc is skipped
    # for comparison (the recompute-success path is pinned by the dedicated
    # pre-feature tests below).
    legacy = {
        "documents": {
            "doc_001": {
                "id": "doc_001", "client_id": None, "status": "confirmed",
                "doc_type": "W-2", "image_path": "uploads/legacy_never_written.png",
                "received_at": "2026-07-01T00:00:00Z", "fields": {},
            }
        },
        "clients": {}, "seq_doc": 1, "seq_client": 0,
    }
    Path(module.STATE_PATH).write_text(json.dumps(legacy))

    fresh = _fresh_app_from_disk(module, root)
    fresh_client = TestClient(fresh.app)
    resp = fresh_client.get("/documents/doc_001")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["id"] == "doc_001"
    assert doc.get("duplicate_of") is None  # absent key reads as no flag
    # A new distinct-type upload against the legacy (phash-less, image-less) doc
    # must not crash and must not be flagged.
    new_id = _intake(fresh_client, "new.png", DISTINCT_IMG.read_bytes()).json()["queued"][0]
    assert fresh_client.get(f"/documents/{new_id}").json()["duplicate_of"] is None


def _seed_legacy_doc(module, root, image_src=None, image_rel="uploads/doc_001.png"):
    """Insert a doc carrying an OLD-SCHEME 16-hex phash (64-bit era) into state.

    image_src copies a real image to the doc's stored path; None leaves the path
    dangling (the unreadable-legacy case). status 'confirmed' so the worker never
    touches it; seq_doc bumped so new intakes mint fresh ids.
    """
    if image_src is not None:
        dest = root / image_rel
        dest.parent.mkdir(exist_ok=True)
        shutil.copyfile(image_src, dest)
    with module.STATE_LOCK:
        module.STATE["documents"]["doc_001"] = {
            "id": "doc_001", "client_id": None, "status": "confirmed",
            "doc_type": "W-2", "image_path": image_rel,
            "received_at": "2026-07-01T00:00:00Z", "fields": {},
            "phash": "0123456789abcdef",  # 16 hex = old 64-bit scheme
            "duplicate_of": None,
        }
        module.STATE["seq_doc"] = 1
        module._persist_locked()


def test_legacy_16hex_phash_is_recomputed_from_stored_image_and_dup_flagged(api):
    """Migration pin: a legacy 64-bit phash (16 hex) with its image still on disk
    is recomputed under the current scheme at the next intake, persisted, and a
    re-encoded copy is correctly flagged against the legacy doc."""
    module, client, _root = api
    import dedup

    _seed_legacy_doc(module, Path(module.BASE_DIR), image_src=BASE_IMG)
    reenc = _reencoded(BASE_IMG.read_bytes())
    new_id = _intake(client, "copy.jpg", reenc).json()["queued"][0]

    assert client.get(f"/documents/{new_id}").json()["duplicate_of"] == "doc_001"
    legacy = client.get("/documents/doc_001").json()
    assert len(legacy["phash"]) == dedup.PHASH_HEX_LEN  # upgraded in place
    persisted = json.loads(Path(module.STATE_PATH).read_text())
    assert len(persisted["documents"]["doc_001"]["phash"]) == dedup.PHASH_HEX_LEN


def test_legacy_phash_with_missing_image_is_skipped_never_crashes_or_false_flags(api):
    """Migration pin: a legacy 16-hex phash whose stored image is GONE cannot be
    recomputed — the doc is skipped for comparison. Intake succeeds, nothing is
    flagged off a length mismatch, and the unreadable legacy phash is untouched."""
    module, client, _root = api
    _seed_legacy_doc(module, Path(module.BASE_DIR), image_src=None,
                     image_rel="uploads/gone.png")

    new_id = _intake(client, "new.jpg", _reencoded(BASE_IMG.read_bytes())).json()["queued"][0]

    assert client.get(f"/documents/{new_id}").json()["duplicate_of"] is None
    assert client.get("/documents/doc_001").json()["phash"] == "0123456789abcdef"


def test_prefeature_doc_without_phash_key_flags_redrop_after_restart(api):
    """Coordinator integration pin (seed-state gap): a PRE-FEATURE doc has NO
    phash key at all — not a legacy-length one. After a restart-load (SHA_INDEX
    empty by design), an exact re-drop of its image bytes must still be flagged:
    the absent phash is recomputed from the stored image at comparison time and
    persisted, and the perceptual path (distance 0 + stage-2 identical) covers
    what the restart-ephemeral exact-match index cannot."""
    module, client, root = api
    import dedup

    uploads = root / "uploads"
    uploads.mkdir(exist_ok=True)
    shutil.copyfile(BASE_IMG, uploads / "doc_001.png")
    prefeature = {
        "documents": {
            "doc_001": {
                "id": "doc_001", "client_id": None, "status": "confirmed",
                "doc_type": "W-2", "image_path": "uploads/doc_001.png",
                "received_at": "2026-07-01T00:00:00Z", "fields": {},
                # deliberately NO phash / duplicate_of keys (pre-feature seed)
            }
        },
        "clients": {}, "seq_doc": 1, "seq_client": 0,
    }
    Path(module.STATE_PATH).write_text(json.dumps(prefeature))

    fresh = _fresh_app_from_disk(module, root)
    fresh_client = TestClient(fresh.app)
    new_id = _intake(fresh_client, "redrop.png", BASE_IMG.read_bytes()).json()["queued"][0]

    assert fresh_client.get(f"/documents/{new_id}").json()["duplicate_of"] == "doc_001"
    # The pre-feature doc's recomputed phash is persisted to disk.
    persisted = json.loads(Path(module.STATE_PATH).read_text())
    assert len(persisted["documents"]["doc_001"]["phash"]) == dedup.PHASH_HEX_LEN


def test_prefeature_doc_with_missing_image_is_skipped_never_crashes(api):
    """Absent phash + unreadable image: the doc cannot be hashed, so it is
    skipped for comparison — intake succeeds, nothing is flagged, no crash, and
    the doc still carries no phash."""
    module, client, root = api
    prefeature = {
        "documents": {
            "doc_001": {
                "id": "doc_001", "client_id": None, "status": "confirmed",
                "doc_type": "W-2", "image_path": "uploads/long_gone.png",
                "received_at": "2026-07-01T00:00:00Z", "fields": {},
            }
        },
        "clients": {}, "seq_doc": 1, "seq_client": 0,
    }
    Path(module.STATE_PATH).write_text(json.dumps(prefeature))

    fresh = _fresh_app_from_disk(module, root)
    fresh_client = TestClient(fresh.app)
    new_id = _intake(fresh_client, "new.png", BASE_IMG.read_bytes()).json()["queued"][0]

    assert fresh_client.get(f"/documents/{new_id}").json()["duplicate_of"] is None
    assert "phash" not in fresh_client.get("/documents/doc_001").json()
