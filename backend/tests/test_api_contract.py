"""Pinned API contract tests from docs/API.md.

These tests deliberately exercise the public HTTP surface.  Backend imports are
lazy so a partially-built backend produces an explicit skip instead of a
collection error.
"""

from __future__ import annotations

import importlib
import importlib.util
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


def _import_backend():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    try:
        module = importlib.import_module("main")
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend API module is not available yet: {exc}")
    required = {
        "app",
        "STATE",
        "QUEUE",
        "STATE_LOCK",
        "_load_state",
        "_persist_locked",
    }
    missing = sorted(name for name in required if not hasattr(module, name))
    if missing:
        pytest.skip("backend API is still missing required symbols: " + ", ".join(missing))
    return module


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    try:
        from fastapi.testclient import TestClient
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"FastAPI TestClient dependencies are not installed: {exc}")

    module = _import_backend()
    root = tmp_path_factory.mktemp("api-state")
    uploads = root / "uploads"
    uploads.mkdir()

    # Keep every test mutation away from the developer's real backend state.
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


def _canned_w2(prompt: str) -> str:
    if "classifier" in prompt:
        return json.dumps({"doc_type": "W-2"})
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
    """Patch the adapter and the pipeline's imported reference to it."""

    try:
        runtime = importlib.import_module("model_runtime")
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend model adapter is not available yet: {exc}")
    if not hasattr(runtime, "extract") or not hasattr(module.pipeline, "model_extract"):
        pytest.skip("backend model adapter/pipeline hook is not available yet")

    def fake_extract(_image_b64, prompt):
        return response(prompt) if callable(response) else response

    monkeypatch.setattr(runtime, "extract", fake_extract)
    # pipeline imports the adapter function by name, so patch that bound hook too.
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


def _wait_for_status(client, doc_id: str, expected: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/documents/{doc_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] == expected:
            return last
        time.sleep(0.01)
    pytest.fail(f"document {doc_id} did not reach {expected}; last state: {last}")


def _new_app_from_disk(module, root):
    """Load main.py as a distinct module, yielding a fresh FastAPI instance."""

    name = f"keepbook_fresh_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, BACKEND / "main.py")
    if spec is None or spec.loader is None:
        pytest.skip("cannot construct a fresh backend app module yet")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.BASE_DIR = str(root)
    fresh.UPLOADS_DIR = str(root / "uploads")
    fresh.STATE_PATH = module.STATE_PATH
    fresh.EVENTS_PATH = module.EVENTS_PATH
    with fresh.STATE_LOCK:
        fresh._load_state()
    return fresh


def test_intake_worker_confirm_image_stats_and_persistence(api, monkeypatch):
    module, client, root = api
    _patch_adapter(monkeypatch, module, _canned_w2)

    created = client.post(
        "/clients",
        json={"name": "Smith, J.", "expected_docs": ["W-2", "1099-INT"]},
    )
    assert created.status_code == 200
    client_id = created.json()["id"]

    response = client.post(
        "/intake",
        files=[
            ("file", ("first.png", PNG, "image/png")),
            ("file", ("second.png", PNG, "image/png")),
        ],
    )
    assert response.status_code == 200
    queued = response.json()
    assert list(queued) == ["queued"]
    assert len(queued["queued"]) == 2

    first_id, second_id = queued["queued"]
    first = _wait_for_status(client, first_id, "extracted")
    _wait_for_status(client, second_id, "extracted")
    assert first["doc_type"] == "W-2"
    assert first["client_id"] is None
    assert set(first["fields"]) == {
        "employee_name",
        "ssn",
        "employer",
        "box1_wages",
        "box2_fed_withheld",
    }
    for field in first["fields"].values():
        assert "value" in field
        assert field["corrected"] is False

    # Extraction alone must not satisfy the client's checklist.
    clients = {item["id"]: item for item in client.get("/clients").json()}
    assert clients[client_id]["received_docs"] == []

    original = first["fields"]["box2_fed_withheld"]["value"]
    confirmed_response = client.post(
        f"/documents/{first_id}/confirm",
        json={
            "client_id": client_id,
            "doc_type": "W-2",
            "fields": {"box2_fed_withheld": "9,200.00"},
        },
    )
    assert confirmed_response.status_code == 200
    confirmed = confirmed_response.json()
    assert confirmed["status"] == "confirmed"
    corrected = confirmed["fields"]["box2_fed_withheld"]
    assert corrected == {
        "value": "9,200.00",
        "corrected": True,
        "original_value": original,
    }
    clients = {item["id"]: item for item in client.get("/clients").json()}
    assert clients[client_id]["received_docs"] == ["W-2"]

    image = client.get(f"/documents/{first_id}/image")
    assert image.status_code == 200
    assert image.content == PNG
    assert image.headers["content-type"].startswith("image/")

    stats = client.get("/stats")
    assert stats.status_code == 200
    assert stats.json() == {
        "fields_extracted": 10,
        "fields_corrected": 1,
        "correction_rate": 0.1,
    }

    # A distinct module creates a fresh FastAPI app and reloads the JSON state.
    fresh = _new_app_from_disk(module, root)
    from fastapi.testclient import TestClient

    fresh_client = TestClient(fresh.app)
    persisted = fresh_client.get(f"/documents/{first_id}")
    assert persisted.status_code == 200
    assert persisted.json() == confirmed
    fresh_clients = {item["id"]: item for item in fresh_client.get("/clients").json()}
    assert fresh_clients[client_id]["received_docs"] == ["W-2"]


def test_unparseable_twice_becomes_unrecognized_then_manual_confirm_succeeds(
    api, monkeypatch
):
    module, client, _root = api
    calls = []

    def junk(prompt):
        calls.append(prompt)
        return "this is not JSON"

    _patch_adapter(monkeypatch, module, junk)
    created = client.post(
        "/clients", json={"name": "Manual Review", "expected_docs": ["1098"]}
    )
    assert created.status_code == 200
    client_id = created.json()["id"]

    intake = client.post(
        "/intake", files=[("file", ("unknown.png", PNG, "image/png"))]
    )
    assert intake.status_code == 200
    doc_id = intake.json()["queued"][0]
    unrecognized = _wait_for_status(client, doc_id, "unrecognized")
    assert len(calls) == 2
    assert unrecognized["doc_type"] == "UNRECOGNIZED"
    assert unrecognized["fields"] == {}
    assert client.get("/clients").json()[0]["received_docs"] == []

    manual = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": "1098", "fields": {}},
    )
    assert manual.status_code == 200
    assert manual.json()["status"] == "confirmed"
    assert manual.json()["doc_type"] == "1098"
    assert manual.json()["client_id"] == client_id
    assert client.get("/clients").json()[0]["received_docs"] == ["1098"]


def test_intake_requires_the_pinned_file_field(api):
    _module, client, _root = api
    response = client.post(
        "/intake", files=[("upload", ("wrong-key.png", PNG, "image/png"))]
    )
    assert 400 <= response.status_code < 500


def test_state_machine_edges(api, monkeypatch):
    module, client, _root = api
    _patch_adapter(monkeypatch, module, _canned_w2)

    assert client.post(
        "/documents/does-not-exist/confirm",
        json={"client_id": "client_x", "doc_type": "W-2", "fields": {}},
    ).status_code == 404

    empty = client.post("/intake", files=[])
    assert 400 <= empty.status_code < 500

    intake = client.post(
        "/intake", files=[("file", ("double.png", PNG, "image/png"))]
    )
    doc_id = intake.json()["queued"][0]
    extracted = _wait_for_status(client, doc_id, "extracted")
    payload = {
        "client_id": None,
        "doc_type": "W-2",
        "fields": {"box1_wages": "70,000.00"},
    }
    first = client.post(f"/documents/{doc_id}/confirm", json=payload)
    assert first.status_code == 200
    first_doc = first.json()
    assert first_doc["fields"]["box1_wages"]["original_value"] == extracted["fields"][
        "box1_wages"
    ]["value"]

    # docs/API.md is silent: accept idempotency or a clean client error, never 500.
    second = client.post(f"/documents/{doc_id}/confirm", json=payload)
    assert second.status_code == 200 or 400 <= second.status_code < 500
    if second.status_code == 200:
        assert second.json() == first_doc

