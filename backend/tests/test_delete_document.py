"""Red contract tests for DELETE /documents/{id} (T63)."""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
    b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    module = importlib.import_module("main")
    root = tmp_path_factory.mktemp("delete-document")
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
    while module.PROCESSING is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert module.PROCESSING is None, "previous test left the worker processing"

    with module.STATE_LOCK:
        module.STATE.clear()
        module.STATE.update(
            {"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0}
        )
        module.QUEUE.clear()
        module.PROCESSING = None
        module.WAKE.clear()
        module._persist_locked()

    events = root / "events.jsonl"
    if events.exists():
        events.unlink()


@pytest.fixture(autouse=True)
def fake_model_adapter(api, monkeypatch):
    module, _client, _root = api
    runtime = importlib.import_module("model_runtime")

    def fake_extract(_image_b64, prompt, **_kwargs):
        if "classifier" in prompt:
            return json.dumps({"doc_type": "W-2"})
        return json.dumps(
            {
                "employee_name": "Delete Contract",
                "ssn": "123-45-6789",
                "employer": "KeepBook Test LLC",
                "box1_wages": "100.00",
                "box2_fed_withheld": "10.00",
            }
        )

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


def _create_client(client, name="Delete Client", expected_docs=None):
    response = client.post(
        "/clients",
        json={"name": name, "expected_docs": expected_docs or ["W-2"]},
    )
    assert response.status_code == 200
    return response.json()["id"]


def _create_extracted_document(client, name="delete-me.png"):
    response = client.post(
        "/intake", files=[("file", (name, PNG, "image/png"))]
    )
    assert response.status_code == 200
    doc_id = response.json()["queued"][0]

    deadline = time.monotonic() + 3
    last = None
    while time.monotonic() < deadline:
        fetched = client.get(f"/documents/{doc_id}")
        assert fetched.status_code == 200
        last = fetched.json()
        if last["status"] == "extracted":
            return doc_id
        time.sleep(0.01)
    pytest.fail(f"document {doc_id} did not reach extracted; last state: {last}")


def _confirm(client, doc_id, client_id, doc_type="W-2"):
    response = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": doc_type, "fields": {}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


def _clients_by_id(client):
    response = client.get("/clients")
    assert response.status_code == 200
    return {item["id"]: item for item in response.json()}


def _delete(client, doc_id):
    response = client.delete(f"/documents/{doc_id}")
    assert response.status_code == 200
    assert response.json() == {"deleted": doc_id}


def test_delete_confirmed_document_returns_ack_and_removes_it_from_reads(api):
    _module, client, _root = api
    client_id = _create_client(client)
    doc_id = _create_extracted_document(client)
    _confirm(client, doc_id, client_id)

    _delete(client, doc_id)

    assert client.get(f"/documents/{doc_id}").status_code == 404
    remaining_ids = {doc["id"] for doc in client.get("/documents").json()}
    assert doc_id not in remaining_ids


def test_delete_last_confirmed_document_of_type_unchecks_client_item(api):
    _module, client, _root = api
    client_id = _create_client(client)
    doc_id = _create_extracted_document(client)
    _confirm(client, doc_id, client_id)
    assert _clients_by_id(client)[client_id]["received_docs"] == ["W-2"]

    _delete(client, doc_id)

    assert _clients_by_id(client)[client_id]["received_docs"] == []


def test_delete_is_count_aware_for_two_confirmed_documents_of_same_type(api):
    _module, client, _root = api
    client_id = _create_client(client)
    first_id = _create_extracted_document(client, "first-w2.png")
    second_id = _create_extracted_document(client, "second-w2.png")
    _confirm(client, first_id, client_id)
    _confirm(client, second_id, client_id)
    assert _clients_by_id(client)[client_id]["received_docs"] == ["W-2"]

    _delete(client, first_id)

    assert _clients_by_id(client)[client_id]["received_docs"] == ["W-2"]
    assert client.get(f"/documents/{second_id}").status_code == 200


def test_delete_appends_event_and_persists_document_removal(api):
    module, client, root = api
    client_id = _create_client(client)
    doc_id = _create_extracted_document(client)
    _confirm(client, doc_id, client_id)

    _delete(client, doc_id)

    event_rows = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text().splitlines()
        if line
    ]
    assert any(
        event.get("type") == "deleted" and event.get("doc_id") == doc_id
        for event in event_rows
    )
    persisted = json.loads(Path(module.STATE_PATH).read_text())
    assert doc_id not in persisted["documents"]


def test_delete_nonexistent_document_returns_404(api):
    _module, client, _root = api

    response = client.delete("/documents/does-not-exist")

    assert response.status_code == 404


def test_delete_extracted_document_leaves_client_checklist_unchanged(api):
    _module, client, _root = api
    client_id = _create_client(client, expected_docs=["W-2", "1099-INT"])
    confirmed_id = _create_extracted_document(client, "keep-confirmed.png")
    _confirm(client, confirmed_id, client_id, doc_type="1099-INT")
    extracted_id = _create_extracted_document(client, "delete-extracted.png")
    before = _clients_by_id(client)[client_id]["received_docs"]
    assert before == ["1099-INT"]

    _delete(client, extracted_id)

    assert _clients_by_id(client)[client_id]["received_docs"] == before
    assert client.get(f"/documents/{extracted_id}").status_code == 404
    assert client.get(f"/documents/{confirmed_id}").status_code == 200
