"""PDF intake — render-to-image, multi-page, encrypted (ROADMAP Phase 2, Tier C #9).

Covers: single-page PDF -> 1 doc with page_number 1 + source_file; 3-page ->
3 docs pages 1..3 grouped by source_file; the 20-page cap (400); encrypted with
no/wrong/right password (password_required / password_incorrect prefixes; docs
only created on the right password); corrupt/zero-byte PDF -> 400; images still
work unchanged; the password never lands in state.json / events.jsonl; %PDF magic
takes precedence over extension; and one bad file fails the whole batch.

Fixtures are built at test time from the committed eval/testset images via
eval/gen_pdf_fixtures.py (no PDF binaries committed). Self-contained fixtures
mirror the fake-adapter pattern in the other backend tests.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
EVAL = ROOT / "eval"
for p in (BACKEND, EVAL):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pdf_render  # noqa: E402  (backend module under test)
import gen_pdf_fixtures as gen  # noqa: E402  (eval fixture generator)

TEST_PASSWORD = gen.TEST_PASSWORD


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    out = tmp_path_factory.mktemp("pdf-fixtures")
    return gen.build_all(str(out))


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    module = importlib.import_module("main")
    root = tmp_path_factory.mktemp("pdf-intake")
    (root / "uploads").mkdir()
    module.BASE_DIR = str(root)
    module.UPLOADS_DIR = str(root / "uploads")
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
    with module.STATE_LOCK:
        module.STATE.clear()
        module.STATE.update({"documents": {}, "clients": {}, "seq_doc": 0, "seq_client": 0})
        module.QUEUE.clear()
        module.PROCESSING = None
        module.WAKE.clear()
        module.SHA_INDEX.clear()
        module._persist_locked()
    events = root / "events.jsonl"
    if events.exists():
        events.unlink()


@pytest.fixture(autouse=True)
def fake_model_adapter(api, monkeypatch):
    module, _client, _root = api
    runtime = importlib.import_module("model_runtime")
    monkeypatch.setenv("PREPROCESS", "0")  # dedup/render run on raw bytes anyway

    def fake_extract(_image_b64, prompt, **_kwargs):
        if "classifier" in prompt:
            return json.dumps({"doc_type": "W-2"})
        return json.dumps({
            "employee_name": "PDF Test", "ssn": "111-22-3333",
            "employer": "KeepBook LLC", "box1_wages": "50000.00",
            "box2_fed_withheld": "5000.00",
        })

    monkeypatch.setattr(runtime, "extract", fake_extract)
    monkeypatch.setattr(module.pipeline, "model_extract", fake_extract)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _post_pdf(client, name, data, password=None):
    files = [("file", (name, data, "application/pdf"))]
    payload = {"password": password} if password is not None else None
    return client.post("/intake", files=files, data=payload)


def _wait_for_status(client, doc_id, expected, timeout=4.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get(f"/documents/{doc_id}").json()
        if last["status"] == expected:
            return last
        time.sleep(0.01)
    pytest.fail(f"{doc_id} never reached {expected}; last: {last}")


def _events(root):
    path = root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x]


# --------------------------------------------------------------------------- #
# is_pdf precedence (pure unit)
# --------------------------------------------------------------------------- #
def test_is_pdf_magic_takes_precedence_over_extension(fixtures):
    single = Path(fixtures["single_page.pdf"]).read_bytes()
    # %PDF magic under a .png name -> still a PDF.
    assert pdf_render.is_pdf(single, "statement.png") is True
    # .pdf extension with non-PDF bytes -> routed to PDF path (will 400 later).
    assert pdf_render.is_pdf(b"not a pdf", "statement.pdf") is True
    # A real image -> not a PDF.
    assert pdf_render.is_pdf(b"\x89PNG\r\n\x1a\n", "scan.png") is False


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_single_page_pdf_makes_one_doc_with_page_number_and_source_file(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["single_page.pdf"]).read_bytes()
    resp = _post_pdf(client, "w2_statement.pdf", data)
    assert resp.status_code == 200
    ids = resp.json()["queued"]
    assert len(ids) == 1
    doc = _wait_for_status(client, ids[0], "extracted")
    assert doc["page_number"] == 1
    assert doc["source_file"] == "w2_statement.pdf"
    assert doc["doc_type"] == "W-2"
    assert doc["image_path"].endswith(".png")  # rendered to PNG


def test_three_page_pdf_makes_three_docs_pages_1_2_3(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["three_page.pdf"]).read_bytes()
    resp = _post_pdf(client, "bundle.pdf", data)
    assert resp.status_code == 200
    ids = resp.json()["queued"]
    assert len(ids) == 3
    docs = [_wait_for_status(client, i, "extracted") for i in ids]
    assert [d["page_number"] for d in docs] == [1, 2, 3]
    assert all(d["source_file"] == "bundle.pdf" for d in docs)
    # Three distinct source pages -> not flagged as duplicates of each other.
    assert all(d["duplicate_of"] is None for d in docs)


def test_page_number_and_source_file_persist_to_state_and_documents(api, fixtures):
    _module, client, root = api
    data = Path(fixtures["single_page.pdf"]).read_bytes()
    doc_id = _post_pdf(client, "persist.pdf", data).json()["queued"][0]
    _wait_for_status(client, doc_id, "extracted")
    # collection endpoint carries it
    listed = {d["id"]: d for d in client.get("/documents").json()}
    assert listed[doc_id]["page_number"] == 1
    assert listed[doc_id]["source_file"] == "persist.pdf"
    # and it is on disk
    persisted = json.loads(Path(_module.STATE_PATH).read_text())
    assert persisted["documents"][doc_id]["page_number"] == 1
    assert persisted["documents"][doc_id]["source_file"] == "persist.pdf"


# --------------------------------------------------------------------------- #
# page cap
# --------------------------------------------------------------------------- #
def test_oversized_pdf_rejected_400_and_no_doc_created(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["oversized_21.pdf"]).read_bytes()
    resp = _post_pdf(client, "big.pdf", data)
    assert resp.status_code == 400
    assert "20" in resp.json()["detail"]  # honest cap message
    assert client.get("/documents").json() == []


# --------------------------------------------------------------------------- #
# encryption
# --------------------------------------------------------------------------- #
def test_encrypted_pdf_without_password_returns_password_required_and_no_doc(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["encrypted.pdf"]).read_bytes()
    resp = _post_pdf(client, "bank.pdf", data)  # no password
    assert resp.status_code == 400
    assert resp.json()["detail"] == "password_required:bank.pdf"
    assert client.get("/documents").json() == []


def test_encrypted_pdf_wrong_password_returns_password_incorrect_and_no_doc(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["encrypted.pdf"]).read_bytes()
    resp = _post_pdf(client, "bank.pdf", data, password="not-the-password")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "password_incorrect:bank.pdf"
    assert client.get("/documents").json() == []


def test_encrypted_pdf_right_password_creates_docs(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["encrypted.pdf"]).read_bytes()
    resp = _post_pdf(client, "bank.pdf", data, password=TEST_PASSWORD)
    assert resp.status_code == 200
    ids = resp.json()["queued"]
    assert len(ids) == 1
    doc = _wait_for_status(client, ids[0], "extracted")
    assert doc["source_file"] == "bank.pdf"
    assert doc["page_number"] == 1


def test_password_never_persisted_after_encrypted_intake(api, fixtures):
    _module, client, root = api
    data = Path(fixtures["encrypted.pdf"]).read_bytes()
    doc_id = _post_pdf(client, "bank.pdf", data, password=TEST_PASSWORD).json()["queued"][0]
    _wait_for_status(client, doc_id, "extracted")
    # state.json and events.jsonl must not contain the password anywhere.
    state_text = Path(_module.STATE_PATH).read_text()
    assert TEST_PASSWORD not in state_text
    events_path = root / "events.jsonl"
    if events_path.exists():
        assert TEST_PASSWORD not in events_path.read_text()
    # nor any raws trace for this doc.
    raws = root / "raws" / f"{doc_id}.json"
    if raws.exists():
        assert TEST_PASSWORD not in raws.read_text()


# --------------------------------------------------------------------------- #
# corrupt / zero-byte
# --------------------------------------------------------------------------- #
def test_corrupt_pdf_rejected_400_and_no_doc(api):
    _module, client, _root = api
    resp = _post_pdf(client, "broken.pdf", b"%PDF-1.4 then garbage that will not parse")
    assert resp.status_code == 400
    assert client.get("/documents").json() == []


def test_zero_byte_pdf_rejected_400_and_no_doc(api):
    _module, client, _root = api
    resp = _post_pdf(client, "empty.pdf", b"")
    assert resp.status_code == 400
    assert client.get("/documents").json() == []


# --------------------------------------------------------------------------- #
# images unchanged + magic precedence + batch atomicity
# --------------------------------------------------------------------------- #
PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00"
    b"\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_image_intake_unchanged_no_page_number_or_source_file(api):
    _module, client, _root = api
    resp = client.post("/intake", files=[("file", ("scan.png", PNG_1x1, "image/png"))])
    assert resp.status_code == 200
    doc_id = resp.json()["queued"][0]
    doc = _wait_for_status(client, doc_id, "extracted")
    assert "page_number" not in doc
    assert "source_file" not in doc


def test_pdf_magic_under_png_name_is_rendered_as_pdf(api, fixtures):
    _module, client, _root = api
    data = Path(fixtures["single_page.pdf"]).read_bytes()
    # Filename lies (.png) but content is %PDF -> magic wins, rendered as a PDF.
    resp = client.post("/intake", files=[("file", ("mislabeled.png", data, "image/png"))])
    assert resp.status_code == 200
    doc_id = resp.json()["queued"][0]
    doc = _wait_for_status(client, doc_id, "extracted")
    assert doc["page_number"] == 1
    assert doc["source_file"] == "mislabeled.png"


def test_one_bad_pdf_fails_the_whole_batch(api, fixtures):
    _module, client, _root = api
    good = Path(fixtures["single_page.pdf"]).read_bytes()
    resp = client.post(
        "/intake",
        files=[
            ("file", ("good.pdf", good, "application/pdf")),
            ("file", ("locked.pdf", Path(fixtures["encrypted.pdf"]).read_bytes(), "application/pdf")),
        ],
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "password_required:locked.pdf"
    # Nothing from the good file either — the batch is all-or-nothing.
    assert client.get("/documents").json() == []


def test_unsupported_type_still_rejected(api):
    _module, client, _root = api
    resp = client.post("/intake", files=[("file", ("notes.txt", b"hello", "text/plain"))])
    assert resp.status_code == 400
