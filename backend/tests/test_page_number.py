"""T61: page_number round-trip on /documents/{id}/confirm.

Continuation pages (page 2 of a K-1, the back of a 1098) carry no extractable
client name, so the reviewer files them by hand under a client + page number.
These tests pin that page_number is optional, validated (int >= 1), persisted on
the document, and returned by both GET /documents/{id} and GET /documents.

Additive contract: omitting page_number must leave confirm behaving exactly as
before (that invariant is owned by test_api_contract.py; here we assert the doc
gains no page_number key when none is sent). Self-contained fixtures mirror the
fake-adapter pattern in test_api_contract.py so this file has no cross-test
import coupling.
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


def _import_backend():
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    try:
        module = importlib.import_module("main")
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend API module is not available yet: {exc}")
    required = {"app", "STATE", "QUEUE", "STATE_LOCK", "_load_state", "_persist_locked"}
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
    root = tmp_path_factory.mktemp("page-state")
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
    try:
        runtime = importlib.import_module("model_runtime")
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"backend model adapter is not available yet: {exc}")
    if not hasattr(runtime, "extract") or not hasattr(module.pipeline, "model_extract"):
        pytest.skip("backend model adapter/pipeline hook is not available yet")

    def fake_extract(_image_b64, prompt, **_kwargs):
        return response(prompt) if callable(response) else response

    monkeypatch.setattr(runtime, "extract", fake_extract)
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


def _extracted_doc(client, monkeypatch, module):
    _patch_adapter(monkeypatch, module, _canned_w2)
    created = client.post("/clients", json={"name": "Whitfield, M.", "expected_docs": ["W-2"]})
    assert created.status_code == 200
    client_id = created.json()["id"]
    intake = client.post("/intake", files=[("file", ("page2.png", PNG, "image/png"))])
    assert intake.status_code == 200
    doc_id = intake.json()["queued"][0]
    _wait_for_status(client, doc_id, "extracted")
    return client_id, doc_id


def test_page_number_round_trips_through_confirm(api, monkeypatch):
    module, client, _root = api
    client_id, doc_id = _extracted_doc(client, monkeypatch, module)

    confirmed = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": "W-2", "fields": {}, "page_number": 2},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["page_number"] == 2

    # GET /documents/{id} returns it...
    one = client.get(f"/documents/{doc_id}")
    assert one.status_code == 200
    assert one.json()["page_number"] == 2

    # ...and so does the collection endpoint.
    listed = {d["id"]: d for d in client.get("/documents").json()}
    assert listed[doc_id]["page_number"] == 2


def test_page_number_is_optional_and_additive(api, monkeypatch):
    """Omitting page_number leaves the confirm contract untouched — no key added."""
    module, client, _root = api
    client_id, doc_id = _extracted_doc(client, monkeypatch, module)

    confirmed = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": "W-2", "fields": {}},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert "page_number" not in confirmed.json()
    assert "page_number" not in client.get(f"/documents/{doc_id}").json()


def test_invalid_page_number_is_rejected_without_confirming(api, monkeypatch):
    module, client, _root = api
    client_id, doc_id = _extracted_doc(client, monkeypatch, module)

    for bad in (0, -1, "abc", 1.5, True):
        resp = client.post(
            f"/documents/{doc_id}/confirm",
            json={"client_id": client_id, "doc_type": "W-2", "fields": {}, "page_number": bad},
        )
        assert 400 <= resp.status_code < 500, f"page_number={bad!r} should be rejected"

    # A rejected confirm must not have mutated the document.
    still = client.get(f"/documents/{doc_id}").json()
    assert still["status"] == "extracted"
    assert "page_number" not in still


def test_numeric_string_page_number_is_coerced(api, monkeypatch):
    """Frontend number inputs surface as strings; a clean integer string is fine."""
    module, client, _root = api
    client_id, doc_id = _extracted_doc(client, monkeypatch, module)

    confirmed = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": "W-2", "fields": {}, "page_number": "3"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["page_number"] == 3
