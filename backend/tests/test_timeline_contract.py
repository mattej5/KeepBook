"""Contract tests for the rolling timeline response in docs/API.md."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FIXED_NOW = datetime(2026, 7, 18, 7, 30, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


@pytest.fixture
def timeline_client(tmp_path, monkeypatch):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    main = importlib.import_module("backend.main")
    backend_runtime = importlib.import_module("backend.model_runtime")
    runtime = importlib.import_module("model_runtime")

    def unexpected_model_call(*_args, **_kwargs):
        pytest.fail("timeline request unexpectedly invoked the model")

    monkeypatch.setattr(backend_runtime, "extract", unexpected_model_call)
    monkeypatch.setattr(runtime, "extract", unexpected_model_call)
    monkeypatch.setattr(main.pipeline, "model_extract", unexpected_model_call)
    monkeypatch.setattr(main, "datetime", FrozenDateTime)
    monkeypatch.setattr(main, "EVENTS_PATH", str(tmp_path / "events.jsonl"))

    events = [
        {
            "ts": "2026-07-18T07:05:00Z",
            "type": "extracted",
            "doc_id": "doc_001",
            "doc_type": "W-2",
            "latency_s": 1.0,
            "fields_total": 5,
            "fields_low_confidence": 0,
            "retried": False,
        },
        {
            "ts": "2026-07-18T07:10:00Z",
            "type": "extracted",
            "doc_id": "doc_002",
            "doc_type": "1099-INT",
            "latency_s": 2.0,
            "fields_total": 3,
            "fields_low_confidence": 0,
            "retried": False,
        },
        {
            "ts": "2026-07-18T07:15:00Z",
            "type": "confirmed",
            "doc_id": "doc_001",
            "doc_type": "W-2",
            "fields_corrected": 1,
            "corrected_keys": ["box1_wages"],
            "manual_type_change": False,
        },
    ]
    Path(main.EVENTS_PATH).write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    return TestClient(main.app)


def test_timeline_returns_all_24_hour_buckets_oldest_first(timeline_client):
    response = timeline_client.get("/stats/timeline?hours=24")
    assert response.status_code == 200

    start = FIXED_NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    expected = [
        {
            "hour": (start + timedelta(hours=offset)).strftime("%H:00"),
            "docs": 0,
            "corrections": 0,
        }
        for offset in range(24)
    ]
    expected[-1] = {"hour": "07:00", "docs": 2, "corrections": 1}

    assert response.json()["buckets"] == expected


def test_timeline_includes_zero_count_correction_categories(timeline_client):
    response = timeline_client.get("/stats/timeline?hours=24")
    assert response.status_code == 200

    assert response.json()["totals"]["corrections_by_category"] == {
        "money": 1,
        "tin_ssn": 0,
        "names": 0,
        "doc_type": 0,
    }
