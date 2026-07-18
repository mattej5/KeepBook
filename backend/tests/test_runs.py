"""Contract tests for GET /runs — the cross-run trace surface.

Deterministic, no GPU: the model adapter is replaced with a canned fake (the
established fake-adapter pattern). Pins the /runs shape, newest-first ordering,
the stage summary read from raws/<id>.json, and graceful behaviour when a run
has no raws file on disk (the seeded-state edge case).
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
    b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)

RUN_KEYS = {
    "doc_id",
    "doc_type",
    "status",
    "model_runtime",
    "model_name",
    "latency_s",
    "preprocessed",
    "retried",
    "call_count",
    "stages",
    "raw_available",
}


def _import_backend():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    try:
        module = importlib.import_module("main")
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend API module is not available yet: {exc}")
    for name in ("app", "STATE", "QUEUE", "STATE_LOCK", "_persist_locked", "_append_event"):
        if not hasattr(module, name):
            pytest.skip(f"backend API is still missing {name}")
    return module


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    try:
        from fastapi.testclient import TestClient
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"FastAPI TestClient dependencies are not installed: {exc}")

    module = _import_backend()
    root = tmp_path_factory.mktemp("runs-state")
    uploads = root / "uploads"
    uploads.mkdir()
    module.BASE_DIR = str(root)
    module.UPLOADS_DIR = str(uploads)
    module.STATE_PATH = str(root / "state.json")
    module.EVENTS_PATH = str(root / "events.jsonl")

    with TestClient(module.app) as client:
        yield module, client, root


@pytest.fixture(autouse=True)
def clean_state(api):
    module, _client, root = api
    deadline = time.monotonic() + 2
    while getattr(module, "PROCESSING", None) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    with module.STATE_LOCK:
        module.STATE.clear()
        module.STATE.update({"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0})
        module.QUEUE.clear()
        module.PROCESSING = None
        module.WAKE.clear()
        module._persist_locked()
    events = root / "events.jsonl"
    if events.exists():
        events.unlink()
    raws = root / "raws"
    if raws.exists():
        for p in raws.glob("*.json"):
            p.unlink()


def _canned_w2(prompt: str) -> str:
    if "classifier" in prompt:
        return json.dumps({"doc_type": "W-2", "handwritten": False})
    return json.dumps(
        {
            "employee_name": "Marcus D. Whitfield",
            "ssn": "412-55-9083",
            "employer": "Cascade Logistics LLC",
            "box1_wages": "68,420.15",
            "box2_fed_withheld": "9,183.44",
        }
    )


def _patch_adapter(monkeypatch, module, response):
    runtime = importlib.import_module("model_runtime")

    def fake_extract(_image_b64, prompt, **_kwargs):
        return response(prompt) if callable(response) else response

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


def _wait_for_status(client, doc_id, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/documents/{doc_id}").json()
        if last.get("status") == expected:
            return last
        time.sleep(0.01)
    pytest.fail(f"{doc_id} never reached {expected}; last: {last}")


def _intake_one(client, name="doc.png"):
    r = client.post("/intake", files=[("file", (name, PNG, "image/png"))])
    assert r.status_code == 200
    return r.json()["queued"][0]


def test_runs_empty_when_no_events(api):
    _module, client, _root = api
    r = client.get("/runs")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


def test_runs_shape_and_stage_summary_from_real_capture(api, monkeypatch):
    module, client, _root = api
    _patch_adapter(monkeypatch, module, _canned_w2)

    doc_id = _intake_one(client, "w2.png")
    _wait_for_status(client, doc_id, "extracted")

    body = client.get("/runs").json()
    assert set(body) == {"runs"}
    assert len(body["runs"]) == 1
    run = body["runs"][0]

    # Exact contract shape — no extra / missing keys.
    assert set(run) == RUN_KEYS
    assert run["doc_id"] == doc_id
    assert run["doc_type"] == "W-2"
    assert run["status"] == "extracted"
    assert run["model_name"]  # event-recorded MODEL_NAME
    assert run["raw_available"] is True
    assert run["retried"] is False
    # Two model calls: classify then extract, in order. Labels are truthful
    # pipeline stages, not invented tool names.
    assert run["call_count"] == 2
    assert run["stages"] == ["classify", "extract"]
    assert isinstance(run["preprocessed"], bool)


def test_runs_newest_first(api, monkeypatch):
    module, client, _root = api
    _patch_adapter(monkeypatch, module, _canned_w2)

    first = _intake_one(client, "first.png")
    _wait_for_status(client, first, "extracted")
    second = _intake_one(client, "second.png")
    _wait_for_status(client, second, "extracted")

    runs = client.get("/runs").json()["runs"]
    assert [r["doc_id"] for r in runs] == [second, first]  # newest first


def test_runs_limit_is_honoured(api, monkeypatch):
    module, client, _root = api
    _patch_adapter(monkeypatch, module, _canned_w2)

    ids = []
    for i in range(3):
        did = _intake_one(client, f"d{i}.png")
        _wait_for_status(client, did, "extracted")
        ids.append(did)

    runs = client.get("/runs?limit=2").json()["runs"]
    assert len(runs) == 2
    # The two newest, newest first.
    assert [r["doc_id"] for r in runs] == [ids[2], ids[1]]


def test_runs_graceful_when_raws_missing(api):
    """Seeded/older docs have an event but no raws/<id>.json on disk. The row
    must still render: raw_available false, call_count null, stages []."""
    module, client, _root = api
    module._append_event({
        "type": "extracted",
        "doc_id": "seed_doc_1",
        "doc_type": "1099-INT",
        "latency_s": 3.1,
        "retried": False,
        "preprocessed": True,
        "model": "gemma4:e4b",
        "raw_ref": "raws/does-not-exist.json",
    })

    runs = client.get("/runs").json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert set(run) == RUN_KEYS
    assert run["doc_id"] == "seed_doc_1"
    assert run["doc_type"] == "1099-INT"
    assert run["raw_available"] is False
    assert run["call_count"] is None
    assert run["stages"] == []
    assert run["latency_s"] == 3.1
    assert run["model_name"] == "gemma4:e4b"


def test_runs_missing_raw_ref_entirely(api):
    """An event with no raw_ref key at all is still a valid (graceful) row."""
    module, client, _root = api
    module._append_event({
        "type": "unrecognized",
        "doc_id": "seed_doc_2",
        "doc_type": "UNRECOGNIZED",
        "latency_s": 1.4,
        "model": "gemma4:e4b",
    })
    runs = client.get("/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["raw_available"] is False
    assert runs[0]["call_count"] is None
    assert runs[0]["status"] == "unrecognized"


def test_runs_labels_the_strict_json_retry(api, monkeypatch):
    """A classify whose first output is unparseable retries once; the second
    call is the strict-JSON retry and must be labelled retry=True in the raws
    trace, still under the classify stage (verified via /documents/<id>/trace)."""
    module, client, _root = api
    state = {"classify_calls": 0}

    def flaky(prompt):
        if "classifier" in prompt:
            state["classify_calls"] += 1
            if state["classify_calls"] == 1:
                return "not json at all"
            return json.dumps({"doc_type": "W-2", "handwritten": False})
        return _canned_w2(prompt)

    _patch_adapter(monkeypatch, module, flaky)
    doc_id = _intake_one(client, "flaky.png")
    _wait_for_status(client, doc_id, "extracted")

    trace = client.get(f"/documents/{doc_id}/trace").json()
    calls = trace["calls"]
    # classify (fail) -> classify retry -> extract
    assert [c.get("stage") for c in calls] == ["classify", "classify", "extract"]
    assert calls[0].get("retry") in (None, False)
    assert calls[1].get("retry") is True
    assert calls[2].get("retry") in (None, False)

    run = client.get("/runs").json()["runs"][0]
    assert run["retried"] is True
    assert run["call_count"] == 3
    assert run["stages"] == ["classify", "classify", "extract"]
