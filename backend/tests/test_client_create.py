"""Contract tests for POST /clients (client-create UI's backend dependency).

The "+ New client" dashboard affordance (frontend/js) posts to this endpoint.
These pin the exact behavior that UI relies on so an additive change never
silently breaks it:

- name is required (missing/empty -> 400)
- the id is minted SERVER-SIDE from the name (slug), never trusted from input
- expected_docs is echoed verbatim; received_docs starts empty (0/N checklist)
- DUPLICATE names are allowed and get DISTINCT ids
- the created client persists (survives a state reload)

No model is ever invoked; state is isolated to a tmp dir.
"""

from __future__ import annotations

import importlib
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
        pytest.fail("client create unexpectedly invoked the model")

    monkeypatch.setattr(main.pipeline, "model_extract", unexpected_model_call)
    monkeypatch.setattr(main, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "EVENTS_PATH", str(tmp_path / "events.jsonl"))

    with main.STATE_LOCK:
        saved = {k: main.STATE.get(k) for k in ("documents", "clients", "seq_doc", "seq_client")}
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


def test_create_mints_id_from_name_and_echoes_expected(client):
    api, _main = client
    resp = api.post("/clients", json={"name": "Okafor, Ruth", "expected_docs": ["W-2", "1098"]})
    assert resp.status_code == 200
    body = resp.json()
    # id is minted server-side from the name (never taken from the request)
    assert body["id"] == "client_okafor_ruth"
    assert body["name"] == "Okafor, Ruth"
    assert body["expected_docs"] == ["W-2", "1098"]
    # brand-new client => empty received_docs => a 0/N all-MISSING checklist
    assert body["received_docs"] == []


def test_missing_name_is_400(client):
    api, _main = client
    assert api.post("/clients", json={"expected_docs": ["W-2"]}).status_code == 400
    assert api.post("/clients", json={"name": ""}).status_code == 400


def test_expected_docs_optional_defaults_empty(client):
    api, _main = client
    body = api.post("/clients", json={"name": "Solo Client"}).json()
    assert body["expected_docs"] == []
    assert body["received_docs"] == []


def test_duplicate_names_allowed_with_distinct_ids(client):
    api, _main = client
    first = api.post("/clients", json={"name": "Chen Partnership"}).json()
    second = api.post("/clients", json={"name": "Chen Partnership"}).json()
    assert first["id"] != second["id"]
    assert first["id"] == "client_chen_partnership"
    assert second["id"] == "client_chen_partnership_2"
    # both names identical — the id is the only differentiator
    assert first["name"] == second["name"] == "Chen Partnership"


def test_created_client_is_readable_and_persists(client):
    api, main = client
    created = api.post("/clients", json={"name": "Marchetti-Reyes, Elena", "expected_docs": ["K-1"]}).json()
    # present in GET /clients right away
    listed = api.get("/clients").json()
    assert any(c["id"] == created["id"] for c in listed)
    # and survives a reload from the persisted state file
    with main.STATE_LOCK:
        main._load_state()
    assert created["id"] in main.STATE["clients"]
