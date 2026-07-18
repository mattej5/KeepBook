"""Regression coverage for cleartext SSN/TIN corrections across backend APIs."""

from __future__ import annotations

import csv
import importlib
import io
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

    main = importlib.import_module("main")
    root = tmp_path_factory.mktemp("tin-correction-state")
    uploads = root / "uploads"
    uploads.mkdir()

    patch = pytest.MonkeyPatch()
    patch.setattr(main, "BASE_DIR", str(root))
    patch.setattr(main, "UPLOADS_DIR", str(uploads))
    patch.setattr(main, "STATE_PATH", str(root / "state.json"))
    patch.setattr(main, "EVENTS_PATH", str(root / "events.jsonl"))

    with TestClient(main.app) as client:
        yield main, client, root

    with main.STATE_LOCK:
        main.STATE.clear()
        main.STATE.update({"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0})
        main.QUEUE.clear()
        main.PROCESSING = None
    patch.undo()


@pytest.fixture(autouse=True)
def clean_state(api):
    main, _client, root = api
    deadline = time.monotonic() + 2
    while main.PROCESSING is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    with main.STATE_LOCK:
        main.STATE.clear()
        main.STATE.update({"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0})
        main.QUEUE.clear()
        main.PROCESSING = None
        main.WAKE.clear()
        main._persist_locked()
    events = root / "events.jsonl"
    if events.exists():
        events.unlink()


def _patch_adapter(monkeypatch, main, doc_type: str, fields: dict[str, str]):
    runtime = importlib.import_module("model_runtime")

    def fake_extract(_image_b64, prompt, **_kwargs):
        if "classifier" in prompt:
            return json.dumps({"doc_type": doc_type})
        return json.dumps(fields)

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(main.pipeline, "model_extract", fake_extract)


def _wait_for_extracted(client: TestClient, doc_id: str) -> dict:
    deadline = time.monotonic() + 3
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/documents/{doc_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] == "extracted":
            return last
        time.sleep(0.01)
    pytest.fail(f"document {doc_id} did not reach extracted; last state: {last}")


def _intake(client: TestClient, filename: str) -> str:
    response = client.post(
        "/intake", files=[("file", (filename, PNG, "image/png"))]
    )
    assert response.status_code == 200
    return response.json()["queued"][0]


def _create_client(client: TestClient, expected_doc: str) -> str:
    response = client.post(
        "/clients", json={"name": "TIN Correction", "expected_docs": [expected_doc]}
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_ssn_correction_persists_cleartext_tracks_timeline_and_exports(
    api, monkeypatch
):
    main, client, root = api
    old_ssn = "412-55-9083"
    new_ssn = "412-55-9084"
    _patch_adapter(
        monkeypatch,
        main,
        "W-2",
        {
            "employee_name": "Marcus D. Whitfield",
            "ssn": old_ssn,
            "employer": "Cascade Logistics LLC",
            "box1_wages": "68,420.15",
            "box2_fed_withheld": "9,183.44",
        },
    )
    client_id = _create_client(client, "W-2")
    doc_id = _intake(client, "w2.png")
    _wait_for_extracted(client, doc_id)

    response = client.post(
        f"/documents/{doc_id}/confirm",
        json={"client_id": client_id, "doc_type": "W-2", "fields": {"ssn": new_ssn}},
    )
    assert response.status_code == 200
    assert response.json()["fields"]["ssn"] == {
        "value": new_ssn,
        "corrected": True,
        "original_value": old_ssn,
    }

    persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert persisted["documents"][doc_id]["fields"]["ssn"]["value"] == new_ssn

    timeline = client.get("/stats/timeline")
    assert timeline.status_code == 200
    assert timeline.json()["totals"]["corrections_by_category"]["tin_ssn"] == 1

    exported = client.get(f"/clients/{client_id}/export.csv")
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    ssn_row = next(row for row in rows if row["field_key"] == "ssn")
    assert ssn_row["value"] == new_ssn
    assert ssn_row["corrected"] == "true"
    assert ssn_row["original_value"] == old_ssn


def test_recipient_tin_correction_preserves_original_and_persists_cleartext(
    api, monkeypatch
):
    main, client, root = api
    old_tin = "82-7654321"
    new_tin = "82-7654322"
    _patch_adapter(
        monkeypatch,
        main,
        "1099-NEC",
        {
            "payer": "Northstar Consulting LLC",
            "recipient_name": "Jordan Lee",
            "recipient_tin": old_tin,
            "box1_nonemployee_comp": "24,500.00",
        },
    )
    client_id = _create_client(client, "1099-NEC")
    doc_id = _intake(client, "1099-nec.png")
    _wait_for_extracted(client, doc_id)

    response = client.post(
        f"/documents/{doc_id}/confirm",
        json={
            "client_id": client_id,
            "doc_type": "1099-NEC",
            "fields": {"recipient_tin": new_tin},
        },
    )
    assert response.status_code == 200
    assert response.json()["fields"]["recipient_tin"] == {
        "value": new_tin,
        "corrected": True,
        "original_value": old_tin,
    }

    persisted = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert (
        persisted["documents"][doc_id]["fields"]["recipient_tin"]["value"]
        == new_tin
    )
