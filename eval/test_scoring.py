"""Pinned, model-free scoring tests from docs/EVAL.md."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "eval" / "run_eval.py"


@pytest.fixture(scope="module")
def scoring():
    if not RUNNER_PATH.exists():
        pytest.skip("eval/run_eval.py has not landed yet")
    spec = importlib.util.spec_from_file_location("keepbook_run_eval_contract", RUNNER_PATH)
    if spec is None or spec.loader is None:
        pytest.skip("eval/run_eval.py cannot be imported yet")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, ModuleNotFoundError) as exc:
        pytest.skip(f"eval scoring dependencies are not available yet: {exc}")
    missing = [name for name in ("score_field", "norm_money", "norm_string", "main") if not hasattr(module, name)]
    if missing:
        pytest.skip("eval scoring helpers have not landed yet: " + ", ".join(missing))
    return module


@pytest.mark.parametrize("predicted", ["$68,420.15", "68420.15", 68420.15])
def test_money_equivalence_and_tolerance(scoring, predicted):
    assert scoring.score_field("68420.15", predicted) == "correct"


def test_wrong_money_is_wrong_and_silent_wrong(scoring):
    verdict = scoring.score_field("68420.15", "68,420.16")
    assert verdict == "wrong"
    # The runner's documented aggregation counts present wrong values only.
    assert int(verdict == "wrong") == 1


def test_names_casefold_and_strip_punctuation_and_whitespace(scoring):
    assert (
        scoring.score_field(
            "Marcus D. Whitfield", "  marcus   d whitfield  "
        )
        == "correct"
    )


def test_missing_field_is_wrong_but_not_silent_wrong(scoring):
    verdict = scoring.score_field("Marcus D. Whitfield", None)
    assert verdict == "missing"
    assert int(verdict == "wrong") == 0


def _run_one_unrecognized_case(scoring, monkeypatch, tmp_path, predicted_type):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "unknown.png").write_bytes(b"model-free fixture")
    labels = tmp_path / "labels.json"
    labels.write_text(
        json.dumps(
            {"unknown.png": {"doc_type": "UNRECOGNIZED", "fields": {}}}
        ),
        encoding="utf-8",
    )
    output = tmp_path / "results.json"
    fake_pipeline = SimpleNamespace(
        run_pipeline=lambda _image: {"doc_type": predicted_type, "fields": {}}
    )
    monkeypatch.setitem(sys.modules, "pipeline", fake_pipeline)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval.py",
            "--labels",
            str(labels),
            "--docs",
            str(docs),
            "--out",
            str(output),
        ],
    )
    assert scoring.main() == 0
    return json.loads(output.read_text(encoding="utf-8"))["summary"]


def test_unrecognized_expected_but_model_says_w2_is_doc_type_wrong(
    scoring, monkeypatch, tmp_path
):
    summary = _run_one_unrecognized_case(scoring, monkeypatch, tmp_path, "W-2")
    assert summary["doc_type_correct"] == 0
    assert summary["doc_type_total"] == 1
    assert summary["field_total"] == 0


def test_unrecognized_match_is_correct_with_zero_field_checks(
    scoring, monkeypatch, tmp_path
):
    summary = _run_one_unrecognized_case(
        scoring, monkeypatch, tmp_path, "UNRECOGNIZED"
    )
    assert summary["doc_type_correct"] == 1
    assert summary["doc_type_total"] == 1
    assert summary["field_total"] == 0
    assert summary["silent_wrong_values"] == 0

