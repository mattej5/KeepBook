"""Contract tests for GET /clients/{id}/export.csv (docs/API.md "CSV export").

Pins the load-bearing guarantees of the integration hand-off:
  * only CONFIRMED documents are exported (extraction alone never leaks),
  * a corrected field exports its corrected value with the replaced value in
    its own original_value column (correction provenance survives),
  * stdlib CSV escaping (comma-bearing money values are quoted),
  * 404 for an unknown client, and a valid header-only CSV for a client with
    no confirmed docs.

State is seeded directly (no model calls) — the endpoint is a pure read over
STATE, mirroring backend/state.demo.json.
"""

from __future__ import annotations

import csv
import importlib
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


def _seed_documents():
    return {
        # Marcus: one CONFIRMED W-2 carrying the box2 correction + a
        # low-confidence flag. Money values contain commas -> escaping probe.
        "doc_004": {
            "id": "doc_004",
            "client_id": "client_marcus_whitfield",
            "status": "confirmed",
            "doc_type": "W-2",
            "image_path": "uploads/doc_004.png",
            "received_at": "2026-07-18T07:36:15Z",
            "fields": {
                "employee_name": {"value": "Marcus D. Whitfield", "corrected": False},
                "ssn": {"value": "412-55-9083", "corrected": False},
                "employer": {"value": "Cascade Logistics LLC", "corrected": False},
                "box1_wages": {"value": "68,420.15", "corrected": False, "low_confidence": True},
                "box2_fed_withheld": {
                    "value": "9,183.44",
                    "corrected": True,
                    "original_value": "70,110.00",
                },
            },
            "source_name": "w2_test.png",
        },
        # Ruth: one CONFIRMED 1099-INT ...
        "doc_001": {
            "id": "doc_001",
            "client_id": "client_ruth_okafor",
            "status": "confirmed",
            "doc_type": "1099-INT",
            "image_path": "uploads/doc_001.png",
            "received_at": "2026-07-18T07:10:00Z",
            "fields": {
                "payer": {"value": "Cascade Federal Bank", "corrected": False},
                "box1_interest_income": {"value": "7,706.17", "corrected": False},
            },
            "source_name": "1099int_clean_01.png",
        },
        # ... and one EXTRACTED W-2 that MUST be excluded from her export.
        "doc_003": {
            "id": "doc_003",
            "client_id": "client_ruth_okafor",
            "status": "extracted",
            "doc_type": "W-2",
            "image_path": "uploads/doc_003.png",
            "received_at": "2026-07-18T07:46:00Z",
            "fields": {
                "employee_name": {"value": "Gunnar N. Nakamura", "corrected": False},
                "box1_wages": {"value": "97,262.94", "corrected": False},
            },
            "source_name": "w2_clean_02.png",
        },
        # Unassigned / unrecognized — never exported.
        "doc_005": {
            "id": "doc_005",
            "client_id": None,
            "status": "unrecognized",
            "doc_type": "UNRECOGNIZED",
            "image_path": "uploads/doc_005.png",
            "received_at": "2026-07-18T07:47:00Z",
            "fields": {},
            "source_name": "receipt_01.png",
        },
    }


def _seed_clients():
    return {
        "client_marcus_whitfield": {
            "id": "client_marcus_whitfield",
            "name": "Marcus Whitfield",
            "expected_docs": ["W-2", "1099-INT"],
            "received_docs": ["W-2"],
        },
        "client_ruth_okafor": {
            "id": "client_ruth_okafor",
            "name": "Ruth Okafor",
            "expected_docs": ["W-2", "1099-INT", "1098"],
            "received_docs": ["1099-INT"],
        },
        # Chen has no documents at all -> header-only export.
        "client_chen_partnership": {
            "id": "client_chen_partnership",
            "name": "Chen Partnership",
            "expected_docs": ["K-1", "1098"],
            "received_docs": [],
        },
    }


@pytest.fixture
def csv_client(tmp_path, monkeypatch):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    main = importlib.import_module("backend.main")
    backend_runtime = importlib.import_module("backend.model_runtime")
    runtime = importlib.import_module("model_runtime")

    def unexpected_model_call(*_args, **_kwargs):
        pytest.fail("CSV export unexpectedly invoked the model")

    monkeypatch.setattr(backend_runtime, "extract", unexpected_model_call)
    monkeypatch.setattr(runtime, "extract", unexpected_model_call)
    monkeypatch.setattr(main.pipeline, "model_extract", unexpected_model_call)
    # Keep any accidental persist away from the developer's real state files.
    monkeypatch.setattr(main, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "EVENTS_PATH", str(tmp_path / "events.jsonl"))

    with main.STATE_LOCK:
        saved = {k: main.STATE.get(k) for k in ("documents", "clients", "seq_doc", "seq_client")}
        main.STATE["documents"] = _seed_documents()
        main.STATE["clients"] = _seed_clients()
        main.STATE["seq_doc"] = 5
        main.STATE["seq_client"] = 3

    yield TestClient(main.app), main

    with main.STATE_LOCK:
        main.STATE["documents"] = saved.get("documents") or {}
        main.STATE["clients"] = saved.get("clients") or {}
        main.STATE["seq_doc"] = saved.get("seq_doc") or 0
        main.STATE["seq_client"] = saved.get("seq_client") or 0


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_export_headers_content_type_and_filename(csv_client):
    client, main = csv_client
    resp = client.get("/clients/client_marcus_whitfield/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert (
        resp.headers["content-disposition"]
        == 'attachment; filename="client_marcus_whitfield.csv"'
    )
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    assert header == main._CSV_COLUMNS


def test_export_is_one_row_per_field_with_correction_provenance(csv_client):
    client, _main = csv_client
    resp = client.get("/clients/client_marcus_whitfield/export.csv")
    rows = _rows(resp.text)

    # Marcus has exactly one confirmed doc (doc_004) with five fields.
    assert len(rows) == 5
    assert {r["doc_id"] for r in rows} == {"doc_004"}
    assert {r["client_id"] for r in rows} == {"client_marcus_whitfield"}
    assert {r["client_name"] for r in rows} == {"Marcus Whitfield"}
    assert {r["doc_type"] for r in rows} == {"W-2"}

    by_key = {r["field_key"]: r for r in rows}

    # The correction: corrected value is exported, original preserved beside it.
    box2 = by_key["box2_fed_withheld"]
    assert box2["value"] == "9,183.44"
    assert box2["corrected"] == "true"
    assert box2["original_value"] == "70,110.00"
    assert box2["field_label"] == "Fed. tax withheld (Box 2)"

    # An uncorrected field: corrected false, original_value empty.
    ssn = by_key["ssn"]
    assert ssn["corrected"] == "false"
    assert ssn["original_value"] == ""
    assert ssn["value"] == "412-55-9083"

    # low_confidence surfaces as a real column.
    assert by_key["box1_wages"]["low_confidence"] == "true"
    assert ssn["low_confidence"] == "false"


def test_export_uses_stdlib_csv_escaping(csv_client):
    client, _main = csv_client
    resp = client.get("/clients/client_marcus_whitfield/export.csv")
    # Comma-bearing money values must be quoted, not split across columns.
    assert '"9,183.44"' in resp.text
    assert '"70,110.00"' in resp.text
    # And they round-trip to a single field intact.
    box2 = next(r for r in _rows(resp.text) if r["field_key"] == "box2_fed_withheld")
    assert box2["value"] == "9,183.44"


def test_export_excludes_non_confirmed_documents(csv_client):
    client, _main = csv_client
    resp = client.get("/clients/client_ruth_okafor/export.csv")
    rows = _rows(resp.text)
    doc_ids = {r["doc_id"] for r in rows}
    # The extracted (not confirmed) W-2 must not appear.
    assert "doc_003" not in doc_ids
    assert doc_ids == {"doc_001"}
    assert {r["field_key"] for r in rows} == {"payer", "box1_interest_income"}


def test_export_empty_client_returns_header_only_csv(csv_client):
    client, main = csv_client
    resp = client.get("/clients/client_chen_partnership/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert _rows(resp.text) == []  # no data rows
    lines = [ln for ln in resp.text.splitlines() if ln.strip()]
    assert lines == [",".join(main._CSV_COLUMNS)]  # header present, nothing else


def test_export_unknown_client_is_404(csv_client):
    client, _main = csv_client
    resp = client.get("/clients/client_nope/export.csv")
    assert resp.status_code == 404


def test_export_emits_one_row_for_fieldless_confirmed_doc(csv_client):
    """A confirmed classify-only doc (extract:false -> no fields) still exports
    one 'document received' row instead of vanishing from the sheet.

    Additive: this test injects its own confirmed field-less doc into STATE
    (the fixture restores documents/clients after yield), so it never perturbs
    the seeded field-bearing rows the other tests pin.
    """
    client, main = csv_client
    with main.STATE_LOCK:
        main.STATE["documents"]["doc_fieldless"] = {
            "id": "doc_fieldless",
            "client_id": "client_ruth_okafor",
            "status": "confirmed",
            "doc_type": "charitable receipt",
            "image_path": "uploads/doc_fieldless.png",
            "received_at": "2026-07-18T08:00:00Z",
            "fields": {},
            "source_name": "charity_01.png",
        }

    resp = client.get("/clients/client_ruth_okafor/export.csv")
    assert resp.status_code == 200
    rows = _rows(resp.text)

    fieldless = [r for r in rows if r["doc_id"] == "doc_fieldless"]
    assert len(fieldless) == 1
    row = fieldless[0]
    assert row["field_key"] == "document"
    assert row["field_label"] == "Document received"
    assert row["value"] == "charitable receipt"
    assert row["doc_type"] == "charitable receipt"
    assert row["corrected"] == "false"
    assert row["original_value"] == ""
    assert row["low_confidence"] == "false"

    # Ruth's field-bearing 1099-INT rows are untouched — additive, not replacing.
    assert {r["field_key"] for r in rows if r["doc_id"] == "doc_001"} == {
        "payer",
        "box1_interest_income",
    }
