"""Regression tests for the demo-critical backend hardening defects."""

from __future__ import annotations

import importlib
import json
import sys
import threading
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
    root = tmp_path_factory.mktemp("demo-hardening")
    uploads = root / "uploads"
    uploads.mkdir()

    module.BASE_DIR = str(root)
    module.UPLOADS_DIR = str(uploads)
    module.STATE_PATH = str(root / "state.json")
    module.EVENTS_PATH = str(root / "events.jsonl")

    with TestClient(module.app) as client:
        yield module, client, root


@pytest.fixture(autouse=True)
def clean_state(api, monkeypatch):
    module, _client, root = api
    deadline = time.monotonic() + 2
    while module.PROCESSING is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert module.PROCESSING is None, "previous test left the worker processing"

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
    monkeypatch.setenv("PREPROCESS", "0")


def _patch_adapter(monkeypatch, module, response):
    runtime = importlib.import_module("model_runtime")

    def fake_extract(_image_b64, prompt, **_kwargs):
        # Keep parity with the production adapter's optional model= override.
        return response(prompt) if callable(response) else response

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


def _intake(client, name="document.png"):
    response = client.post("/intake", files=[("file", (name, PNG, "image/png"))])
    assert response.status_code == 200
    return response.json()["queued"][0]


def _wait_for(client, doc_id, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/documents/{doc_id}")
        assert response.status_code == 200
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.01)
    pytest.fail(f"condition not reached for {doc_id}; last document: {last}")


def _events_for(root, doc_id, timeout=3.0):
    path = root / "events.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            events = [json.loads(line) for line in path.read_text().splitlines() if line]
            matching = [event for event in events if event.get("doc_id") == doc_id]
            if matching:
                return matching
        time.sleep(0.01)
    pytest.fail(f"no event recorded for {doc_id}")


def test_model_failure_is_distinct_from_legitimate_junk(api, monkeypatch):
    module, client, root = api

    def connection_failure(_prompt):
        raise ConnectionError("model runtime unavailable")

    _patch_adapter(monkeypatch, module, connection_failure)
    failed_id = _intake(client, "model-failure.png")
    failed = _wait_for(client, failed_id, lambda doc: doc["status"] != "pending")
    failed_events = _events_for(root, failed_id)

    calls = []

    def junk(prompt):
        calls.append(prompt)
        return "not JSON"

    _patch_adapter(monkeypatch, module, junk)
    junk_id = _intake(client, "legitimate-junk.png")
    junk_doc = _wait_for(client, junk_id, lambda doc: doc["status"] != "pending")
    junk_events = _events_for(root, junk_id)

    failure_signal = failed.get("status") == "error" or "error" in failed
    assert failure_signal, f"model failure lacks a machine-readable error signal: {failed}"
    assert any(event.get("error") for event in failed_events), (
        f"model failure event does not record the error: {failed_events}"
    )
    assert len(calls) == 2
    assert junk_doc["status"] == "unrecognized"
    assert "error" not in junk_doc
    assert not any(event.get("error") for event in junk_events)


def test_queue_pending_count_includes_in_flight_pending_document(api, monkeypatch):
    module, client, _root = api
    entered = threading.Event()
    release = threading.Event()

    def gated_response(_prompt):
        entered.set()
        assert release.wait(timeout=1), "test did not release the gated model adapter"
        return "not JSON"

    _patch_adapter(monkeypatch, module, gated_response)
    doc_id = _intake(client, "slow.png")

    try:
        assert entered.wait(timeout=1), "worker did not enter the gated model adapter"
        queue = client.get("/queue").json()
        document = client.get(f"/documents/{doc_id}").json()
    finally:
        release.set()
        _wait_for(client, doc_id, lambda doc: doc["status"] != "pending")

    assert document["status"] == "pending"
    assert queue["processing"] == doc_id
    assert queue["pending"] == 1, (
        "queue reported zero pending while its in-flight document was still pending: "
        f"queue={queue}, document_status={document['status']}"
    )


def test_document_trace_returns_raw_model_io_and_unknown_is_404(api, monkeypatch):
    module, client, root = api

    def canned(prompt):
        if "classifier" in prompt:
            return json.dumps({"doc_type": "W-2"})
        return json.dumps(
            {
                "employee_name": "Test Person",
                "ssn": "123-45-6789",
                "employer": "Test Employer",
                "box1_wages": "100.00",
                "box2_fed_withheld": "10.00",
            }
        )

    _patch_adapter(monkeypatch, module, canned)
    doc_id = _intake(client, "trace.png")
    _wait_for(client, doc_id, lambda doc: doc["status"] == "extracted")

    raw_path = root / "raws" / f"{doc_id}.json"
    deadline = time.monotonic() + 3
    while not raw_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert raw_path.exists(), "worker did not persist the raw model trace"
    expected = json.loads(raw_path.read_text())

    response = client.get(f"/documents/{doc_id}/trace")
    assert response.status_code == 200
    assert response.json() == expected
    assert all("prompt" in call and "response" in call for call in response.json()["calls"])
    assert client.get("/documents/does-not-exist/trace").status_code == 404
