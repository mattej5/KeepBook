"""Contract tests for PATCH /clients/{id} and DELETE /clients/{id}.

The dashboard client-edit affordance (frontend/js) depends on these. They pin
the behavior the UI relies on so an additive change never silently breaks it:

- rename updates name but NEVER regenerates the id (documents reference
  client_id — a new id would orphan every filed document)
- expected_docs is a FULL REPLACE, deduped with order preserved; received_docs
  is left untouched (a checklist edit doesn't disturb what the client has sent)
- name, when present, must be non-empty (400); name is only validated when the
  key is present, so an expected_docs-only PATCH is fine
- delete is GUARDED: 409 with a document count while any document references the
  client; 200 once nothing does
- both mutations persist across a state reload; unknown ids are 404

No model is ever invoked; state is isolated to a tmp dir. Documents are seeded
directly into STATE (no worker, no image files) so the tests are deterministic.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


@pytest.fixture
def client(tmp_path, monkeypatch):
    try:
        from fastapi.testclient import TestClient
    except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
        pytest.skip(f"FastAPI TestClient dependencies are not installed: {exc}")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    main = importlib.import_module("backend.main")

    def unexpected_model_call(*_args, **_kwargs):
        pytest.fail("client edit unexpectedly invoked the model")

    monkeypatch.setattr(main.pipeline, "model_extract", unexpected_model_call)
    monkeypatch.setattr(main, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "EVENTS_PATH", str(tmp_path / "events.jsonl"))

    with main.STATE_LOCK:
        saved = {
            k: main.STATE.get(k)
            for k in ("documents", "clients", "seq_doc", "seq_client")
        }
        main.STATE["documents"] = {}
        main.STATE["clients"] = {}
        main.STATE["seq_doc"] = 0
        main.STATE["seq_client"] = 0

    yield TestClient(main.app), main

    with main.STATE_LOCK:
        main.STATE["documents"] = saved.get("documents") or {}
        main.STATE["clients"] = saved.get("clients") or {}
        main.STATE["seq_doc"] = saved.get("seq_doc") or 0
        main.STATE["seq_client"] = saved.get("seq_client") or 0


def _create(api, name, expected_docs=None):
    resp = api.post(
        "/clients", json={"name": name, "expected_docs": expected_docs or []}
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def _seed_document(main, doc_id, client_id, doc_type="W-2", status="confirmed"):
    """Put a document straight into STATE so the delete guard has something to
    count. No image, no worker — the guard only reads client_id."""
    with main.STATE_LOCK:
        main.STATE["documents"][doc_id] = {
            "id": doc_id,
            "client_id": client_id,
            "status": status,
            "doc_type": doc_type,
            "fields": {},
            "received_at": "2026-07-18T00:00:00Z",
        }
        main._persist_locked()


def _clients_by_id(api):
    resp = api.get("/clients")
    assert resp.status_code == 200
    return {c["id"]: c for c in resp.json()}


# --------------------------- rename / id stability -------------------------
def test_rename_keeps_id_stable_and_reflects_in_get(client):
    api, _main = client
    cid = _create(api, "Chen", ["W-2"])

    resp = api.patch(f"/clients/{cid}", json={"name": "Chen Partners"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == cid  # id is NEVER regenerated on rename
    assert body["name"] == "Chen Partners"

    listed = _clients_by_id(api)
    assert cid in listed
    assert listed[cid]["name"] == "Chen Partners"


def test_rename_does_not_orphan_referencing_documents(client):
    api, main = client
    cid = _create(api, "Chen", ["W-2"])
    _seed_document(main, "doc_001", cid)

    api.patch(f"/clients/{cid}", json={"name": "Chen Partners"})

    # The document's client_id still points at the (unchanged) client id.
    doc = api.get("/documents/doc_001").json()
    assert doc["client_id"] == cid


# --------------------------- expected_docs replace -------------------------
def test_expected_docs_full_replace_dedups_preserves_order_keeps_received(client):
    api, main = client
    cid = _create(api, "Marcus", ["W-2", "1099-INT"])
    # Simulate a confirmed W-2 already on file for this client.
    with main.STATE_LOCK:
        main.STATE["clients"][cid]["received_docs"] = ["W-2"]
        main._persist_locked()

    resp = api.patch(
        f"/clients/{cid}",
        json={"expected_docs": ["1099-DIV", "W-2", "1099-DIV", "K-1"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # dedup, order preserved
    assert body["expected_docs"] == ["1099-DIV", "W-2", "K-1"]
    # received_docs is NOT disturbed by a checklist edit
    assert body["received_docs"] == ["W-2"]

    listed = _clients_by_id(api)[cid]
    assert listed["expected_docs"] == ["1099-DIV", "W-2", "K-1"]
    assert listed["received_docs"] == ["W-2"]


def test_expected_docs_only_patch_leaves_name_untouched(client):
    api, _main = client
    cid = _create(api, "Solo", ["W-2"])
    # name key absent -> name not validated, not changed
    resp = api.patch(f"/clients/{cid}", json={"expected_docs": ["K-1"]})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Solo"
    assert resp.json()["expected_docs"] == ["K-1"]


# ------------------------------- validation --------------------------------
def test_empty_name_is_400(client):
    api, _main = client
    cid = _create(api, "Chen", ["W-2"])
    assert api.patch(f"/clients/{cid}", json={"name": ""}).status_code == 400
    assert api.patch(f"/clients/{cid}", json={"name": "   "}).status_code == 400
    # the rejected PATCH did not mutate the stored name
    assert _clients_by_id(api)[cid]["name"] == "Chen"


def test_patch_unknown_client_is_404(client):
    api, _main = client
    assert api.patch("/clients/nope", json={"name": "X"}).status_code == 404


# --------------------------- delete (guarded) ------------------------------
def test_delete_is_guarded_409_while_documents_reference_the_client(client):
    api, main = client
    cid = _create(api, "Marcus", ["W-2"])
    _seed_document(main, "doc_001", cid)
    _seed_document(main, "doc_002", cid)

    resp = api.delete(f"/clients/{cid}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["document_count"] == 2
    # the client is still present — nothing was orphaned
    assert cid in _clients_by_id(api)


def test_delete_succeeds_when_no_documents_reference_the_client(client):
    api, _main = client
    cid = _create(api, "Test Dupe", ["W-2"])

    resp = api.delete(f"/clients/{cid}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": cid}
    assert cid not in _clients_by_id(api)


def test_delete_unknown_client_is_404(client):
    api, _main = client
    assert api.delete("/clients/nope").status_code == 404


# ------------------------------- persistence -------------------------------
def test_rename_persists_across_reload(client):
    api, main = client
    cid = _create(api, "Chen", ["W-2"])
    api.patch(f"/clients/{cid}", json={"name": "Chen Partners", "expected_docs": ["K-1"]})

    with main.STATE_LOCK:
        main._load_state()
    reloaded = main.STATE["clients"][cid]
    assert reloaded["name"] == "Chen Partners"
    assert reloaded["expected_docs"] == ["K-1"]


def test_delete_persists_across_reload(client):
    api, main = client
    cid = _create(api, "Test Dupe", ["W-2"])
    api.delete(f"/clients/{cid}")

    with main.STATE_LOCK:
        main._load_state()
    assert cid not in main.STATE["clients"]


# --------------------------------- events ----------------------------------
def test_update_and_delete_append_events(client):
    api, main = client
    cid = _create(api, "Chen", ["W-2"])
    api.patch(f"/clients/{cid}", json={"name": "Chen Partners"})
    api.delete(f"/clients/{cid}")

    rows = [
        json.loads(line)
        for line in Path(main.EVENTS_PATH).read_text().splitlines()
        if line
    ]
    assert any(
        e.get("type") == "client_updated" and e.get("client_id") == cid for e in rows
    )
    assert any(
        e.get("type") == "client_deleted" and e.get("client_id") == cid for e in rows
    )
