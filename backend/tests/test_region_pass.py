"""Unit tests for the REGION_PASS strategy (backend/regions.py + pipeline hooks).

Deterministic, no GPU: model_extract is monkeypatched with canned crop reads,
crops are taken from real testset images so the fractional boxes are exercised.
"""
import base64
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pipeline  # noqa: E402
import regions  # noqa: E402

_TESTSET = os.path.abspath(os.path.join(_BACKEND, "..", "eval", "testset"))


def _b64(name):
    with open(os.path.join(_TESTSET, name), "rb") as fh:
        return base64.b64encode(fh.read()).decode()


# --------------------------------------------------------------------------- regions
def test_region_box_and_skip():
    assert regions.region_box("1098", "lender") is not None
    assert regions.region_box("1098", "no_such_field") is None
    assert regions.has_region("K-1", "partner_name")
    assert not regions.has_region("W-2", "not_a_field")


def test_looks_like_label_true_on_box_labels():
    assert regions.looks_like_label("RECIPIENT'S/LENDER'S name")
    assert regions.looks_like_label("PAYER'S name")
    assert regions.looks_like_label("Wages, tips, other compensation")
    assert regions.looks_like_label("")


def test_looks_like_label_false_on_real_values():
    assert not regions.looks_like_label("Copperline Bank")
    assert not regions.looks_like_label("Blue Ridge Analytics Inc.")
    assert not regions.looks_like_label("Rashid P. Bergstrom")


def test_crop_region_b64_produces_decodable_png():
    from PIL import Image
    import io

    crop = regions.crop_region_b64(_b64("1098_clean_01.png"), "1098", "lender")
    assert crop is not None
    im = Image.open(io.BytesIO(base64.b64decode(crop)))
    assert im.size[0] >= 8 and im.size[1] >= 8
    # missing region -> None (caller skips)
    assert regions.crop_region_b64(_b64("1098_clean_01.png"), "1098", "nope") is None


# --------------------------------------------------------------- field classification
def test_field_classification():
    assert pipeline.is_name_field("lender")
    assert pipeline.is_name_field("employee_name")
    assert not pipeline.is_name_field("recipient_tin")
    assert not pipeline.is_name_field("partnership_ein")
    assert not pipeline.is_name_field("box1_wages")
    assert pipeline.is_tin_key("ssn")
    assert pipeline.is_tin_key("partnership_ein")


# --------------------------------------------------------------- cross-field validators
def test_low_confidence_w2_box2_ge_box1():
    f = {"box1_wages": "1000.00", "box2_fed_withheld": "2000.00"}
    lc = pipeline.compute_low_confidence("W-2", f)
    assert lc["box2_fed_withheld"] is True


def test_low_confidence_w2_normal_clear():
    f = {
        "employee_name": "Jane Doe", "ssn": "432-47-9397", "employer": "Acme LLC",
        "box1_wages": "50000.00", "box2_fed_withheld": "6000.00",
    }
    lc = pipeline.compute_low_confidence("W-2", f)
    assert not any(lc.values())


def test_low_confidence_money_nonpositive_flagged():
    lc = pipeline.compute_low_confidence("1098", {"box1_mortgage_interest": "0.00"})
    assert lc["box1_mortgage_interest"] is True


def test_low_confidence_label_echo_flagged():
    lc = pipeline.compute_low_confidence("1098", {"lender": "RECIPIENT'S/LENDER'S name"})
    assert lc["lender"] is True


def test_low_confidence_bad_tin_flagged():
    # recipient_tin / partnership_ein must be 9 digits.
    assert pipeline.compute_low_confidence("1099-NEC", {"recipient_tin": "12-345"})["recipient_tin"]
    assert pipeline.compute_low_confidence("K-1", {"partnership_ein": "1"})["partnership_ein"]
    assert not pipeline.compute_low_confidence("K-1", {"partnership_ein": "73-8796124"})["partnership_ein"]


# --------------------------------------------------------------- apply_region_pass
@pytest.fixture
def canned_model(monkeypatch):
    box = {"reads": {}}

    def fake_extract(image_b64, prompt, model=None):
        import json
        for needle, val in box["reads"].items():
            if needle in prompt:
                return json.dumps({"value": val})
        return json.dumps({"value": ""})

    monkeypatch.setattr(pipeline, "model_extract", fake_extract)
    return box


def test_region_replaces_wrong_name_keeps_good_money(canned_model):
    canned_model["reads"] = {
        "LENDER": "Copperline Bank",
        "BORROWER": "PAYER'S/BORROWER'S name",  # label echo -> rejected
        "Mortgage interest": "17725.14",
    }
    fields = {"lender": "Coppell Bank", "borrower_name": "", "box1_mortgage_interest": "17725.14"}
    upd, n, repl = pipeline.apply_region_pass(_b64("1098_clean_01.png"), "1098", fields)
    assert upd["lender"] == "Copperline Bank"
    assert upd["borrower_name"] == ""  # label echo rejected
    assert upd["box1_mortgage_interest"] == "17725.14"  # good money never re-asked
    assert repl == [("lender", "Coppell Bank", "Copperline Bank")]


def test_region_noop_when_read_matches(canned_model):
    canned_model["reads"] = {"LENDER": "Coppell Bank", "BORROWER": "Jo Smith",
                             "Mortgage interest": "17725.14"}
    fields = {"lender": "Coppell Bank", "borrower_name": "Jo Smith",
              "box1_mortgage_interest": "17725.14"}
    _, _, repl = pipeline.apply_region_pass(_b64("1098_clean_01.png"), "1098", fields)
    assert repl == []


def test_region_fills_empty_k1(canned_model):
    canned_model["reads"] = {
        "employer identification number": "73-8796124",
        "Partnership's name": "Blue Ridge Analytics Inc.",
        "partner": "Rashid P. Bergstrom",
        "Ordinary business income": "114563.75",
    }
    fields = {"partnership_ein": "", "partnership_name": "", "partner_name": "",
              "ordinary_income": ""}
    upd, n, _ = pipeline.apply_region_pass(_b64("k1_clean_01.png"), "K-1", fields)
    assert upd["partnership_ein"] == "73-8796124"
    assert upd["ordinary_income"] == "114563.75"
    assert upd["partner_name"] == "Rashid P. Bergstrom"
    assert n == 4


def test_region_rejects_bad_money_crop(canned_model):
    # empty money field; crop returns non-numeric -> rejected, stays empty.
    canned_model["reads"] = {"Ordinary business income": "not a number",
                             "employer identification number": "73-8796124",
                             "Partnership's name": "X Co", "partner": "Y Z"}
    fields = {"partnership_ein": "", "partnership_name": "", "partner_name": "",
              "ordinary_income": ""}
    upd, _, _ = pipeline.apply_region_pass(_b64("k1_clean_01.png"), "K-1", fields)
    assert upd["ordinary_income"] == ""  # bad money crop not accepted


def test_region_cap_respected(canned_model):
    canned_model["reads"] = {}  # every crop returns empty -> still counts a call
    # W-2 has 5 fields; names(2) always + failed ones. Cap is 6, schema has 5.
    fields = {"employee_name": "", "ssn": "bad", "employer": "",
              "box1_wages": "", "box2_fed_withheld": ""}
    _, n, _ = pipeline.apply_region_pass(_b64("w2_clean_01.png"), "W-2", fields)
    assert n <= pipeline.REGION_CAP
