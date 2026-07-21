"""Contract tests for GET /clients/{id}/nudge (Phase 2 Tier A #4 — "still
waiting on" reminder drafts). Visible-autonomy feature: the model DRAFTS the
note, a human copies it. See docs/API.md "Nudge draft".

Covers: complete client -> draft null; gapped client + FAKE adapter -> model
draft when it passes the post-checks; each individual post-check failure mode
falls back to the deterministic template; an adapter exception falls back to
the template; unknown client -> 404; a nudge_drafted event row is appended.

No live model call — model_generate_text is monkeypatched, same discipline as
the other backend tests patch model_extract.
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
        pytest.fail("nudge test unexpectedly invoked the image-extraction model")

    monkeypatch.setattr(main.pipeline, "model_extract", unexpected_model_call)
    monkeypatch.setattr(main, "STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "EVENTS_PATH", str(tmp_path / "events.jsonl"))

    with main.STATE_LOCK:
        saved = {k: main.STATE.get(k) for k in ("documents", "clients", "seq_doc", "seq_client")}
        main.STATE["documents"] = {}
        main.STATE["clients"] = {}
        main.STATE["seq_doc"] = 0
        main.STATE["seq_client"] = 0

    yield TestClient(main.app), main, tmp_path

    with main.STATE_LOCK:
        main.STATE["documents"] = saved.get("documents") or {}
        main.STATE["clients"] = saved.get("clients") or {}
        main.STATE["seq_doc"] = saved.get("seq_doc") or 0
        main.STATE["seq_client"] = saved.get("seq_client") or 0


def _make_client(api, name, expected, received=None):
    body = api.post("/clients", json={"name": name, "expected_docs": expected}).json()
    if received:
        # Simulate confirmed docs by writing received_docs directly — the
        # nudge endpoint only reads client state, never documents.
        cid = body["id"]
        import importlib as _il

        main = _il.import_module("backend.main")
        with main.STATE_LOCK:
            main.STATE["clients"][cid]["received_docs"] = list(received)
            main._persist_locked()
    return body


def _events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_complete_client_returns_null_draft_and_no_call(client, monkeypatch):
    api, main, tmp_path = client

    def unexpected(*_a, **_kw):
        pytest.fail("nudge must not call the model for a complete client")

    monkeypatch.setattr(main, "model_generate_text", unexpected)

    c = _make_client(api, "Complete Client", ["W-2", "1098"], received=["W-2", "1098"])
    resp = api.get(f"/clients/{c['id']}/nudge")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"client_id": c["id"], "missing": [], "draft": None}
    # no nudge_drafted event for a complete client (nothing to draft)
    assert not any(e.get("type") == "nudge_drafted" for e in _events(tmp_path))


def test_unknown_client_is_404(client):
    api, _main, _tmp = client
    assert api.get("/clients/does-not-exist/nudge").status_code == 404


def test_gapped_client_model_draft_accepted_when_it_passes_checks(client, monkeypatch):
    api, main, tmp_path = client
    c = _make_client(api, "Ruth Okafor", ["W-2", "1099-INT", "1098"], received=["1098"])

    def fake_generate(prompt, **_kwargs):
        assert "Ruth Okafor" in prompt
        assert "W-2" in prompt and "1099-INT" in prompt
        return "Hi Ruth Okafor,\n\nWe're still missing:\n- W-2\n- 1099-INT\n\nPlease send these in. Thank you."

    monkeypatch.setattr(main, "model_generate_text", fake_generate)

    resp = api.get(f"/clients/{c['id']}/nudge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == c["id"]
    assert set(body["missing"]) == {"W-2", "1099-INT"}
    assert body["generated_by"] == "model"
    assert "Ruth Okafor" in body["draft"]
    assert "W-2" in body["draft"] and "1099-INT" in body["draft"]

    events = _events(tmp_path)
    nudge_events = [e for e in events if e.get("type") == "nudge_drafted"]
    assert len(nudge_events) == 1
    assert nudge_events[0]["client_id"] == c["id"]
    assert nudge_events[0]["generated_by"] == "model"
    assert "ts" in nudge_events[0]


def test_model_output_missing_a_doc_name_falls_back_to_template(client, monkeypatch):
    api, main, tmp_path = client
    c = _make_client(api, "J. Smith", ["W-2", "K-1"])

    def fake_generate(_prompt, **_kwargs):
        # Drops K-1 entirely — must fail the post-check.
        return "Hi J. Smith,\n\nWe still need your W-2. Thanks."

    monkeypatch.setattr(main, "model_generate_text", fake_generate)
    body = api.get(f"/clients/{c['id']}/nudge").json()
    assert body["generated_by"] == "template"
    assert "J. Smith" in body["draft"]
    assert "W-2" in body["draft"] and "K-1" in body["draft"]
    assert _events(tmp_path)[-1]["generated_by"] == "template"


def test_model_output_with_bracket_placeholder_falls_back_to_template(client, monkeypatch):
    api, main, _tmp = client
    c = _make_client(api, "J. Smith", ["W-2"])

    def fake_generate(_prompt, **_kwargs):
        return "Hi J. Smith,\n\nWe still need: W-2.\n\nThanks,\n[Firm Name]"

    monkeypatch.setattr(main, "model_generate_text", fake_generate)
    body = api.get(f"/clients/{c['id']}/nudge").json()
    assert body["generated_by"] == "template"
    assert "[" not in body["draft"]


def test_model_output_overlong_falls_back_to_template(client, monkeypatch):
    api, main, _tmp = client
    c = _make_client(api, "J. Smith", ["W-2"])

    def fake_generate(_prompt, **_kwargs):
        return "Hi J. Smith, we still need W-2. " + ("x" * 900)

    monkeypatch.setattr(main, "model_generate_text", fake_generate)
    body = api.get(f"/clients/{c['id']}/nudge").json()
    assert body["generated_by"] == "template"
    assert len(body["draft"]) <= main.NUDGE_MAX_CHARS


def test_adapter_raising_falls_back_to_template(client, monkeypatch):
    api, main, tmp_path = client
    c = _make_client(api, "Marcus Whitfield", ["1099-NEC"])

    def boom(_prompt, **_kwargs):
        raise ConnectionError("model runtime unavailable")

    monkeypatch.setattr(main, "model_generate_text", boom)
    resp = api.get(f"/clients/{c['id']}/nudge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["generated_by"] == "template"
    assert "Marcus Whitfield" in body["draft"]
    assert "1099-NEC" in body["draft"]
    assert _events(tmp_path)[-1]["generated_by"] == "template"


def test_timeout_raising_falls_back_to_template(client, monkeypatch):
    api, main, _tmp = client
    c = _make_client(api, "Slow Client", ["1098"])

    def timeout(_prompt, **_kwargs):
        raise TimeoutError("model call timed out")

    monkeypatch.setattr(main, "model_generate_text", timeout)
    body = api.get(f"/clients/{c['id']}/nudge").json()
    assert body["generated_by"] == "template"


def test_nudge_never_touches_stats_timeline_shape(client, monkeypatch):
    """Additive-only: a nudge_drafted event must not perturb /stats/timeline's
    pinned response shape (docs/API.md), since it is not an 'extracted',
    'unrecognized', or 'confirmed' event type."""
    api, main, _tmp = client
    c = _make_client(api, "J. Smith", ["W-2"])
    monkeypatch.setattr(main, "model_generate_text", lambda *_a, **_kw: "irrelevant")
    api.get(f"/clients/{c['id']}/nudge")
    resp = api.get("/stats/timeline?hours=1")
    assert resp.status_code == 200
    totals = resp.json()["totals"]
    assert totals["docs_processed"] == 0
    assert totals["fields_extracted"] == 0
