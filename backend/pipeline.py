"""Production classify + extract pipeline for KeepBook.

This is the single source of the prompts and parsing that the backend worker
runs in production. eval/run_eval.py imports THIS module (not a copy) so the
eval always measures the exact prompt path that will demo.

Two model calls per document (docs/API.md "Processing loop" permits this):
  1. classify  -> strict-JSON doc_type, mapped to the canonical enum
  2. extract   -> strict-JSON, type-specific field keys

Strict JSON, temperature 0 (enforced by model_runtime). One retry on
unparseable JSON, then the document is marked UNRECOGNIZED.
"""

import base64
import binascii
import json
import os
import re

import regions
from model_runtime import extract as model_extract


# ---------------------------------------------------------------------------
# Observability: pipeline-STAGE labels for the raw-I/O capture.
# ---------------------------------------------------------------------------
# This pipeline has no tool calls, and Gemma emits no chain-of-thought, so the
# truthful granularity of a "trace" is the pipeline STAGE that issued each model
# call (classify / extract / region:<field> / ensemble:<model> / reask:<field>)
# and whether that particular call was the strict-JSON retry. We publish the
# stage of the *next* model_extract call in this module-level marker; the raw-
# capture wrapper in backend/main.py snapshots it when it records the call.
#
# Deliberately NOT passed as a model_extract argument: the adapter call signature
# stays `model_extract(image_b64, prompt, model=...)` so the real adapter and the
# fixed-signature test fakes that patch model_extract are untouched (additive).
# The worker is the only sequential model caller, so this single marker is race-
# free: the pipeline sets it immediately before each call and the wrapper reads
# it synchronously in the same thread.
_capture_stage = {"stage": None, "retry": False}


def _mark_stage(stage, retry=False):
    """Publish the stage/retry of the next model_extract call for the capturer."""
    _capture_stage["stage"] = stage
    _capture_stage["retry"] = bool(retry)


# ---------------------------------------------------------------------------
# Optional intake preprocessing (backend/preprocess.py).
# Enabled by default; PREPROCESS=0 disables it (the A/B "off" arm). Runs on the
# raw image bytes before the model ever sees them: document-region crop,
# perspective/deskew, illumination flattening, upscale-if-small. Any failure
# falls back to the untouched image, so it can never break or degrade intake.
# ---------------------------------------------------------------------------
def _preprocess_enabled() -> bool:
    return os.environ.get("PREPROCESS", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _maybe_preprocess(image_b64: str) -> str:
    if not _preprocess_enabled():
        return image_b64
    try:
        raw = base64.b64decode(image_b64)
    except (binascii.Error, ValueError):
        return image_b64
    try:
        from preprocess import preprocess  # lazy: no cv2 import unless enabled

        cleaned = preprocess(raw)
        if cleaned and cleaned != raw:
            return base64.b64encode(cleaned).decode()
    except Exception:  # noqa: BLE001 - preprocessing must never break intake
        return image_b64
    return image_b64

# ---------------------------------------------------------------------------
# Canonical doc types + per-type field schema.
# Field keys are aligned to eval/labels.json (the scored ground truth) and
# docs/API.md. UNRECOGNIZED never carries fields.
# ---------------------------------------------------------------------------
DOC_TYPES = ["W-2", "1099-NEC", "1099-INT", "1099-MISC", "K-1", "1098"]
UNRECOGNIZED = "UNRECOGNIZED"

# Classify-only doc types (T65): the model classifies + the human assigns and
# confirms them, but NO field extraction runs (extract: false). One model call,
# not two — cheaper AND immune to the silent-wrong failure class, because a
# document with zero extracted fields cannot carry a well-formed-but-wrong value.
# They still satisfy checklist items once confirmed (checklist match is by
# doc_type string, so a confirmed "1099-DIV" checks off an expected "1099-DIV").
# These deliberately have NO FIELD_SCHEMA entry; run_pipeline short-circuits
# before extract_fields ever indexes FIELD_SCHEMA[doc_type].
CLASSIFY_ONLY_TYPES = [
    "1099-DIV", "1099-B", "1099-R", "1099-G",
    "1098-T", "1098-E", "1095-A",
    "property tax statement", "charitable receipt", "brokerage statement",
    "W-9", "engagement letter",
]
CLASSIFY_ONLY_SET = set(CLASSIFY_ONLY_TYPES)
# Full classification enum shown to the model (extract types first).
ALL_DOC_TYPES = DOC_TYPES + CLASSIFY_ONLY_TYPES

FIELD_SCHEMA = {
    "W-2": ["employee_name", "ssn", "employer", "box1_wages", "box2_fed_withheld"],
    "1099-NEC": ["payer", "recipient_name", "recipient_tin", "box1_nonemployee_comp"],
    "1099-INT": ["payer", "recipient_name", "box1_interest_income"],
    "1099-MISC": ["payer", "recipient_name", "recipient_tin", "box3_other_income"],
    "K-1": ["partnership_name", "partner_name", "partnership_ein", "ordinary_income"],
    "1098": ["lender", "borrower_name", "box1_mortgage_interest"],
}


# ---------------------------------------------------------------------------
# Model-call strategies (env-flagged, independently toggleable)
#   RE_ASK=1 (default OFF) -> after extraction, one focused follow-up per
#               empty/malformed field (fill-only, never overwrites a value that
#               already passes its format check). Cap REASK_CAP calls per doc.
#               Default OFF per the A/B gate (eval/results_e4b_tools.json vs
#               eval/results.json): field accuracy rose 41->44 but 4 empty
#               fields came back as well-formed WRONG values (silent-wrongs
#               23->27) and median latency rose ~5s. All 4 bad fills were
#               free-text name fields, where field_format_ok accepts any
#               non-empty string, so the acceptance gate has no teeth there.
#               The never-overwrite guard itself held: zero correct->wrong.
#   CASCADE=1 (default OFF) -> classify on the small model
#               (CASCADE_CLASSIFY_MODEL), extract on MODEL_NAME. A small-model
#               UNRECOGNIZED is confirmed on the extraction model before the
#               reject is accepted. Default OFF because measured on the demo
#               host (24GB unified memory, one resident ollama model): warm e2b
#               classify is no faster than e4b (~6.7s vs ~5.9s median) and each
#               cascade doc pays a 7-13s e2b<->e4b model reload, so total
#               latency rises instead of dropping.
#   REGION_PASS=1 (default OFF until gated) -> after the whole-image extraction,
#               for each field that is empty, fails its format check, OR is a
#               name/entity field (names have no format to fail — exactly where
#               whole-image reads drift on small text), crop that field's region
#               (backend/regions.py, fractional boxes so they survive preprocess
#               and any resolution) and make ONE single-field call on the crop.
#               Acceptance has TEETH so it can't repeat the RE_ASK silent-wrong
#               failure: format-checkable fields must pass their format check;
#               name fields are accepted only if non-empty AND not a box-label
#               echo (regions.looks_like_label) AND length >= 3. A value that
#               already passes its format check is never overwritten EXCEPT a
#               name field, where the higher-resolution crop read is preferred
#               over a differing whole-image read (counted separately). Cap
#               REGION_CAP crop calls per document.
# Read at call time so eval/tests can toggle per run.
# ---------------------------------------------------------------------------
REASK_CAP = 3
REGION_CAP = 6


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _classify_model() -> str:
    return os.environ.get("CASCADE_CLASSIFY_MODEL", "gemma4:e2b")


def _hand_ensemble_model() -> str:
    """The SMALL model used to cross-check handwritten docs (HAND_ENSEMBLE)."""
    return os.environ.get("HAND_ENSEMBLE_MODEL", "gemma4:e2b")


# Human box descriptions for the focused re-ask prompt. These are IRS form
# layout references only (box numbers / labels) — never test-set values.
FIELD_DESCRIPTIONS = {
    ("W-2", "employee_name"): "box e, the employee's name",
    ("W-2", "ssn"): "box a, the employee's Social Security number",
    ("W-2", "employer"): "box c, the employer's name",
    ("W-2", "box1_wages"): "box 1, Wages, tips, other compensation",
    ("W-2", "box2_fed_withheld"): "box 2, Federal income tax withheld",
    ("1099-NEC", "payer"): "the PAYER'S name (top-left payer box)",
    ("1099-NEC", "recipient_name"): "the RECIPIENT'S name",
    ("1099-NEC", "recipient_tin"): "the RECIPIENT'S TIN",
    ("1099-NEC", "box1_nonemployee_comp"): "box 1, Nonemployee compensation",
    ("1099-INT", "payer"): "the PAYER'S name (top-left payer box)",
    ("1099-INT", "recipient_name"): "the RECIPIENT'S name",
    ("1099-INT", "box1_interest_income"): "box 1, Interest income",
    ("1099-MISC", "payer"): "the PAYER'S name (top-left payer box)",
    ("1099-MISC", "recipient_name"): "the RECIPIENT'S name",
    ("1099-MISC", "recipient_tin"): "the RECIPIENT'S TIN",
    ("1099-MISC", "box3_other_income"): "box 3, Other income",
    ("K-1", "partnership_name"): "Part I, the partnership's name",
    ("K-1", "partner_name"): "Part II, the partner's name",
    ("K-1", "partnership_ein"): "Part I item A, the partnership's EIN",
    ("K-1", "ordinary_income"): "Part III box 1, Ordinary business income (loss)",
    ("1098", "lender"): "the RECIPIENT'S/LENDER'S name",
    ("1098", "borrower_name"): "the PAYER'S/BORROWER'S name",
    ("1098", "box1_mortgage_interest"): "box 1, Mortgage interest received from payer(s)",
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def build_classify_prompt() -> str:
    types = ", ".join(f'"{t}"' for t in ALL_DOC_TYPES)
    return (
        "You are a US tax-document intake classifier. Look at this document image "
        "and decide which single tax document it is.\n"
        "Return STRICT JSON only, no prose, no markdown:\n"
        '{"doc_type": "<TYPE>", "handwritten": true}\n'
        f"where <TYPE> is EXACTLY one of: {types}, or "
        f'"{UNRECOGNIZED}" if it is not clearly one of those documents (for '
        "example a pay stub, a bank statement, a utility bill, or any document not "
        "on the list above).\n"
        "Only choose a type when the document clearly IS that document. When you "
        f'are unsure, return "{UNRECOGNIZED}" — never force an unrelated document '
        "into a type just because it is the closest match.\n"
        'Set "handwritten" to true if the values filled into the form (the names, '
        "numbers and dollar amounts) are written by hand in pen or pencil, or false "
        "if those entries are typed or machine-printed. Judge ONLY the filled-in "
        "entries — the blank form template itself is always printed."
    )


def build_extract_prompt(doc_type: str) -> str:
    fields = FIELD_SCHEMA[doc_type]
    shape = ", ".join(f'"{k}": "..."' for k in fields)
    return (
        f"You are a US tax-document data extractor. This is a {doc_type} form.\n"
        "Read the values printed on the form and return STRICT JSON only, no "
        "prose, no markdown:\n"
        f"{{{shape}}}\n"
        "Use the EXACT values printed on the form. For money fields return the "
        "number as printed (digits, commas and decimal point are fine). If a "
        'field is genuinely not present, use an empty string "".'
    )


def build_reask_prompt(doc_type: str, key: str) -> str:
    """Focused single-field follow-up prompt (RE_ASK strategy)."""
    desc = FIELD_DESCRIPTIONS.get((doc_type, key), key)
    return (
        f"Look at this {doc_type} tax form. Read ONLY {desc}.\n"
        "Return STRICT JSON only, no prose, no markdown:\n"
        '{"value": "..."}\n'
        "Use the EXACT value printed on the form. If it is genuinely not "
        'present, use an empty string "".'
    )


def build_region_prompt(doc_type: str, key: str) -> str:
    """Single-field prompt for a cropped region (REGION_PASS strategy)."""
    desc = regions.region_desc(doc_type, key) or FIELD_DESCRIPTIONS.get(
        (doc_type, key), key
    )
    return (
        f"This is a cropped region of a {doc_type} tax form. "
        f"Read the value of {desc}.\n"
        "Return STRICT JSON only, no prose, no markdown:\n"
        '{"value": "..."}\n'
        "Return the EXACT value printed on the form, not the box label. If the "
        'value is genuinely not present, use an empty string "".'
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_json(text):
    """Best-effort strict-JSON parse of a model response. Returns dict or None."""
    if not text:
        return None
    s = text.strip()
    # Strip ```json ... ``` / ``` ... ``` fences if present.
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # Grab the outermost {...} span.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    snippet = s[start : end + 1]
    try:
        obj = json.loads(snippet)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


_SSN_RE = re.compile(r"^\d{3}-?\d{2}-?\d{4}$")
_MONEY_KEY_TOKENS = ("box", "wage", "income", "comp", "interest", "withheld", "mortgage")


def is_money_key(key: str) -> bool:
    k = key.lower()
    return any(tok in k for tok in _MONEY_KEY_TOKENS)


def is_tin_key(key: str) -> bool:
    k = key.lower()
    return k == "ssn" or "tin" in k or "ein" in k


def is_name_field(key: str) -> bool:
    """A free-text field with no format to fail (name / entity / payer / lender).

    These are exactly the fields where field_format_ok has no teeth (it accepts
    any non-empty string), so REGION_PASS always re-reads them from a crop.
    """
    return not is_money_key(key) and not is_tin_key(key)


def parse_money(value):
    """Parse a money string to float, or None. Mirrors eval/run_eval.norm_money."""
    try:
        return float(re.sub(r"[,$\s]", "", str(value)))
    except (ValueError, TypeError):
        return None


def field_format_ok(key: str, value) -> bool:
    """Deterministic format check for the low_confidence signal. No probabilities."""
    v = str(value or "").strip()
    if not v:
        return False
    k = key.lower()
    if k == "ssn":
        return bool(_SSN_RE.match(v))
    if "tin" in k or "ein" in k:
        return len(re.sub(r"\D", "", v)) == 9  # SSN- or EIN-format = 9 digits
    if is_money_key(k):
        cleaned = re.sub(r"[,$\s]", "", v)
        try:
            float(cleaned)
            return True
        except ValueError:
            return False
    return True  # names / free-text: no format to fail


def field_low_confidence(key: str, value, retried: bool = False) -> bool:
    """True from honest signals only: extraction retried, empty, or bad format."""
    return bool(retried) or not field_format_ok(key, value)


def compute_low_confidence(doc_type: str, fields: dict, retried: bool = False) -> dict:
    """Per-field low_confidence flags from deterministic signals only.

    Base signal (field_low_confidence): extraction retried, empty, or a format
    check that fails (SSN regex; TIN/EIN = 9 digits, which already covers
    recipient_tin and partnership_ein; money must parse). Extended with
    cross-field validators, none of which use probabilities:
      * value echoes a known box-label string  -> flag
      * money field parses to a non-positive number -> flag
      * W-2: box 2 (fed withheld) must parse < box 1 (wages); else flag box 2.
    """
    lc = {}
    for key, value in fields.items():
        flag = field_low_confidence(key, value, retried)
        if regions.looks_like_label(value):
            flag = True
        if is_money_key(key):
            m = parse_money(value)
            if m is not None and m <= 0:
                flag = True
        lc[key] = flag
    # W-2 arithmetic sanity: federal withholding is a fraction of wages.
    if doc_type == "W-2":
        b1 = parse_money(fields.get("box1_wages", ""))
        b2 = parse_money(fields.get("box2_fed_withheld", ""))
        if b1 is not None and b2 is not None and b2 >= b1:
            lc["box2_fed_withheld"] = True
    return lc


def correction_category(key: str) -> str:
    """Map a corrected field key to a stats category (docs/API.md)."""
    k = key.lower()
    if any(tok in k for tok in ("box", "wage", "income", "comp", "interest")):
        return "money"
    if k == "ssn" or "tin" in k or "ein" in k:
        return "tin_ssn"
    return "names"  # *name* / payer / employer / lender / partnership* / other strings


def normalize_doc_type(raw) -> str:
    """Map a free-form model doc_type string to the canonical enum.

    Gemma often answers 'Form W-2 Wage and Tax Statement' instead of 'W-2'.
    """
    if not raw or not isinstance(raw, str):
        return UNRECOGNIZED
    t = raw.strip().lower()
    if not t:
        return UNRECOGNIZED
    # Order matters: check the more specific 1099 variants first. The word-based
    # subtypes (nec/int/misc/div) are checked before the single-letter ones
    # (b/r/g), which are matched adjacent to "1099" so they can't false-fire.
    if "1099" in t and "nec" in t:
        return "1099-NEC"
    if "1099" in t and "int" in t:
        return "1099-INT"
    if "1099" in t and "misc" in t:
        return "1099-MISC"
    if "1099" in t and "div" in t:
        return "1099-DIV"
    if re.search(r"1099[\s\-]?r\b", t):
        return "1099-R"
    if re.search(r"1099[\s\-]?g\b", t):
        return "1099-G"
    if re.search(r"1099[\s\-]?b\b", t):
        return "1099-B"
    # 1098 family: -T (tuition) / -E (student loan) before the bare 1098 (mortgage).
    if re.search(r"1098[\s\-]?t\b", t) or "tuition" in t:
        return "1098-T"
    if re.search(r"1098[\s\-]?e\b", t) or "student loan" in t:
        return "1098-E"
    if "1098" in t:
        return "1098"
    # 1095 (Health Insurance Marketplace) — only -A is in the enum.
    if "1095" in t:
        return "1095-A"
    if re.search(r"\bw[\s\-]?2\b", t) or "wage and tax" in t:
        return "W-2"
    if re.search(r"\bw[\s\-]?9\b", t) or "request for taxpayer identification" in t:
        return "W-9"
    if re.search(r"\bk[\s\-]?1\b", t) or "schedule k" in t:
        return "K-1"
    # Non-form classify-only documents (no IRS form number to key on).
    if "property tax" in t:
        return "property tax statement"
    if "charitable" in t or "donation receipt" in t or "donation acknowledg" in t:
        return "charitable receipt"
    if "brokerage" in t or "consolidated 1099" in t:
        return "brokerage statement"
    if "engagement" in t:
        return "engagement letter"
    if "unrecogni" in t or "unknown" in t or "not a tax" in t:
        return UNRECOGNIZED
    return UNRECOGNIZED


# ---------------------------------------------------------------------------
# Model calls with one retry
# ---------------------------------------------------------------------------
def _call_and_parse(image_b64: str, prompt: str, model: str = None, stage: str = None):
    """Call the model, parse JSON, retry once on unparseable output.

    Returns (parsed_dict_or_None, raw_text_of_last_attempt, retried_bool).

    `stage` is a trace label only (published to the raw capturer); it never
    reaches the adapter, so the call signature is unchanged.
    """
    _mark_stage(stage, retry=False)
    raw = model_extract(image_b64, prompt, model=model)
    parsed = parse_json(raw)
    if parsed is not None:
        return parsed, raw, False
    _mark_stage(stage, retry=True)  # the strict-JSON retry
    raw2 = model_extract(image_b64, prompt, model=model)
    parsed2 = parse_json(raw2)
    return parsed2, raw2, True


def _coerce_bool(v) -> bool:
    """Coerce a model's JSON handwritten flag to a real bool.

    Gemma sometimes returns the string "true"/"false" instead of a JSON bool.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "handwritten")
    return False


def classify(image_b64: str, model: str = None):
    """Return (doc_type, handwritten, raw, retried).

    handwritten is the model's judgement of whether the FILLED-IN values are
    pen/pencil vs machine-printed (build_classify_prompt asks for it as a second
    JSON key). Defaults False when absent/unparseable — the ensemble path only
    activates on a positive signal, so a missing key fails safe to normal intake.
    """
    parsed, raw, retried = _call_and_parse(
        image_b64, build_classify_prompt(), model=model, stage="classify"
    )
    if parsed is None:
        return UNRECOGNIZED, False, raw, retried
    return (
        normalize_doc_type(parsed.get("doc_type")),
        _coerce_bool(parsed.get("handwritten")),
        raw,
        retried,
    )


def classify_cascade(image_b64: str):
    """CASCADE classify: small model first, confirm UNRECOGNIZED on the big one.

    A cheap small-model classify handles the common case. If the small model
    yields UNRECOGNIZED (real reject OR a small-model miss), re-run classify
    once on the extraction model before accepting the reject — this protects
    both the reject path and any form the small model failed to recognize.
    """
    dt, hand, raw, retried = classify(image_b64, model=_classify_model())
    if dt == UNRECOGNIZED:
        dt2, hand2, raw2, retried2 = classify(image_b64)  # extraction model (MODEL_NAME)
        return dt2, hand2, raw2, (retried or retried2)
    return dt, hand, raw, retried


def extract_fields(image_b64: str, doc_type: str, model: str = None, stage: str = "extract"):
    """Return (fields_dict_or_None, raw, retried). fields keyed per FIELD_SCHEMA.

    `stage` labels the call for the trace: the primary extraction pass is
    "extract"; the HAND_ENSEMBLE cross-check passes "ensemble:<model>".
    """
    parsed, raw, retried = _call_and_parse(
        image_b64, build_extract_prompt(doc_type), model=model, stage=stage
    )
    if parsed is None:
        return None, raw, retried
    fields = {}
    for key in FIELD_SCHEMA[doc_type]:
        val = parsed.get(key, "")
        fields[key] = "" if val is None else str(val).strip()
    return fields, raw, retried


def apply_reask(image_b64: str, doc_type: str, fields: dict, model: str = None):
    """RE_ASK strategy: fill only empty/malformed fields, never overwrite a good one.

    For each field whose current value FAILS its deterministic format check
    (empty, or e.g. a TIN without 9 digits, or unparseable money), issue one
    focused single-field follow-up call. Accept the new value ONLY if it now
    passes the same format check; otherwise keep the original (its
    low_confidence signal stays set). A field whose value already passes is
    never touched — this is the guard against silently replacing a correct
    value with a wrong one. At most REASK_CAP calls per document.

    Returns (updated_fields, n_reasks).
    """
    updated = dict(fields)
    n = 0
    for key in FIELD_SCHEMA[doc_type]:
        if n >= REASK_CAP:
            break
        if field_format_ok(key, updated.get(key, "")):
            continue  # already well-formed -> never re-ask / never overwrite
        n += 1
        _mark_stage("reask:" + key)
        raw = model_extract(image_b64, build_reask_prompt(doc_type, key), model=model)
        parsed = parse_json(raw)
        if not parsed:
            continue
        new_val = parsed.get("value", "")
        new_val = "" if new_val is None else str(new_val).strip()
        if new_val and field_format_ok(key, new_val):
            updated[key] = new_val  # accept: it now passes the format check
    return updated, n


def apply_region_pass(image_b64: str, doc_type: str, fields: dict, model: str = None):
    """REGION_PASS strategy: re-read fields from per-field region crops.

    A field is re-read when it has a region entry AND (it is empty, OR it fails
    its format check, OR it is a name/entity field — names can't be format-
    validated, so they are always re-checked at higher crop resolution). For
    each, crop the region (fractional box, so it survives preprocess) and make
    one single-field call on the crop.

    Acceptance (with teeth, unlike free-text RE_ASK):
      * format-checkable field (money / TIN / SSN): accept only if the crop read
        passes its format check. Such a field is re-read only when its current
        value already failed, so this never overwrites a good value.
      * name/entity field: accept only if the crop read is non-empty, is not a
        box-label echo, and has length >= 3. If the current value was
        empty/failed -> fill. If the current value was a good (non-empty) name
        and the crop read DIFFERS -> prefer the crop read (higher resolution
        strictly dominates) and count it as a replacement.

    At most REGION_CAP crop calls per document.

    Returns (updated_fields, n_calls, replacements) where replacements is a list
    of (key, old_value, new_value) for good-name -> crop-name swaps.
    """
    updated = dict(fields)
    n = 0
    replacements = []
    for key in FIELD_SCHEMA[doc_type]:
        if n >= REGION_CAP:
            break
        if not regions.has_region(doc_type, key):
            continue
        cur = updated.get(key, "")
        name_field = is_name_field(key)
        if not name_field and field_format_ok(key, cur):
            continue  # good money/TIN -> never re-ask, never overwrite
        crop_b64 = regions.crop_region_b64(image_b64, doc_type, key)
        if crop_b64 is None:
            continue
        n += 1
        _mark_stage("region:" + key)
        raw = model_extract(crop_b64, build_region_prompt(doc_type, key), model=model)
        parsed = parse_json(raw)
        if not parsed:
            continue
        new_val = parsed.get("value", "")
        new_val = "" if new_val is None else str(new_val).strip()
        if not new_val:
            continue
        if name_field:
            if regions.looks_like_label(new_val) or len(new_val) < 3:
                continue  # reject label echoes / too-short reads
            if not cur:
                updated[key] = new_val  # fill an empty/failed name
            elif norm_string(new_val) != norm_string(cur):
                updated[key] = new_val  # prefer the higher-resolution crop read
                replacements.append((key, cur, new_val))
        else:
            if field_format_ok(key, new_val):
                updated[key] = new_val  # accept: crop read now passes format
    return updated, n, replacements


def norm_string(v) -> str:
    """Casefolded, punctuation-stripped compare key (matches eval scoring)."""
    s = re.sub(r"[^\w\s]", " ", str(v))
    return " ".join(s.split()).casefold()


def _fields_agree(key: str, a, b) -> bool:
    """Do two model reads of one field agree under the eval's normalized rule?

    Mirrors eval/run_eval.score_field: money compared within a 0.01 tolerance,
    everything else (names, TIN/SSN/EIN) compared casefolded + depunctuated.
    Either side empty -> not an agreement (an empty read carries no signal).
    """
    a_s, b_s = str(a).strip(), str(b).strip()
    if not a_s or not b_s:
        return False
    if is_money_key(key):
        ma, mb = parse_money(a_s), parse_money(b_s)
        if ma is None or mb is None:
            return False
        return abs(ma - mb) <= 0.01
    return norm_string(a_s) == norm_string(b_s)


def apply_hand_ensemble(image_b64: str, doc_type: str, fields: dict, model_small: str):
    """HAND_ENSEMBLE strategy: cross-check the e4b read against ONE e2b read.

    Handwriting is ASSUMED to need a human, so the e4b value is ALWAYS kept and
    the caller flags every field low_confidence. The small model is a
    cross-check signal only — never substituted into the output. This makes ONE
    small-model call for the whole field set (a single e4b<->e2b model swap per
    document, never per field — per-field swapping on a one-resident-model host
    would cost 7-13s each and is the failure mode this design exists to avoid).

    Per field: agreed (small model matches under _fields_agree) leaves disputed
    False; disagreement OR an empty small-model read sets disputed True so the UI
    can surface it hotter.

    Returns (disputed_map, {"agreed": n, "disputed": n}).
    """
    e2b_fields, _raw, _retried = extract_fields(
        image_b64, doc_type, model=model_small, stage="ensemble:" + str(model_small)
    )
    e2b_fields = e2b_fields or {}
    disputed = {}
    for key in FIELD_SCHEMA[doc_type]:
        e4b_val = fields.get(key, "")
        e2b_val = str(e2b_fields.get(key, "")).strip()
        disputed[key] = not (e2b_val and _fields_agree(key, e4b_val, e2b_val))
    counts = {
        "agreed": sum(1 for v in disputed.values() if not v),
        "disputed": sum(1 for v in disputed.values() if v),
    }
    return disputed, counts


def run_pipeline(image_b64: str) -> dict:
    """Full two-step pipeline for one document image.

    Returns a runtime-neutral result:
      {
        "status": "extracted" | "unrecognized",
        "doc_type": <canonical type or "UNRECOGNIZED">,
        "fields": {key: value_str},   # plain values; {} when unrecognized or classify-only
        "classify_raw": str,
        "extract_raw": str | None,
        "retried": bool,   # either model call needed a retry (low_confidence signal)
        "re_asks": int,    # focused single-field follow-up calls issued (RE_ASK)
        "region_calls": int,          # single-field crop calls issued (REGION_PASS)
        "region_replacements": list,  # (key, old, new) good-name -> crop-name swaps
        "handwritten": bool,          # classifier judged the filled-in values hand-written
        "hand_ensemble": dict|None,   # {"agreed": n, "disputed": n} when HAND_ENSEMBLE ran
        "disputed": {key: bool},      # small-model disagreed/empty (surface hotter)
        "low_confidence": {key: bool},  # per-field deterministic review flags
      }
    """
    image_b64 = _maybe_preprocess(image_b64)
    if _flag("CASCADE", "0"):
        doc_type, handwritten, classify_raw, c_retried = classify_cascade(image_b64)
    else:
        doc_type, handwritten, classify_raw, c_retried = classify(image_b64)
    if doc_type == UNRECOGNIZED:
        return {
            "status": "unrecognized",
            "doc_type": UNRECOGNIZED,
            "fields": {},
            "classify_raw": classify_raw,
            "extract_raw": None,
            "retried": c_retried,
            "re_asks": 0,
            "region_calls": 0,
            "region_replacements": [],
            "handwritten": handwritten,
            "hand_ensemble": None,
            "disputed": {},
            "low_confidence": {},
        }
    if doc_type in CLASSIFY_ONLY_SET:
        # Classify-only type (T65): extract: false. No extraction call is made,
        # so this document lands "extracted" with zero fields, awaiting client
        # assignment + human confirm. With no extracted values there is nothing
        # to be silently wrong about — the failure class is designed out.
        return {
            "status": "extracted",
            "doc_type": doc_type,
            "fields": {},
            "classify_raw": classify_raw,
            "extract_raw": None,
            "retried": c_retried,
            "re_asks": 0,
            "region_calls": 0,
            "region_replacements": [],
            "handwritten": handwritten,
            "hand_ensemble": None,
            "disputed": {},
            "low_confidence": {},
            "classify_only": True,
        }
    fields, extract_raw, e_retried = extract_fields(image_b64, doc_type)
    if fields is None:
        # Extraction JSON unparseable after one retry -> honest UNRECOGNIZED.
        return {
            "status": "unrecognized",
            "doc_type": UNRECOGNIZED,
            "fields": {},
            "classify_raw": classify_raw,
            "extract_raw": extract_raw,
            "retried": c_retried or e_retried,
            "re_asks": 0,
            "region_calls": 0,
            "region_replacements": [],
            "handwritten": handwritten,
            "hand_ensemble": None,
            "disputed": {},
            "low_confidence": {},
        }
    retried = c_retried or e_retried
    n_reasks = 0
    if _flag("RE_ASK", "0"):
        fields, n_reasks = apply_reask(image_b64, doc_type, fields)
    n_region = 0
    region_replacements = []
    if _flag("REGION_PASS", "0"):
        fields, n_region, region_replacements = apply_region_pass(
            image_b64, doc_type, fields
        )
    low_confidence = compute_low_confidence(doc_type, fields, retried)
    hand_ensemble = None
    disputed = {}
    if handwritten and _flag("HAND_ENSEMBLE", "0"):
        # Handwritten intake: cross-check the e4b read against one e2b read, then
        # flag EVERY field for human review regardless of outcome (the human
        # gate is the feature, not the fallback). Agreement keeps the value with
        # the flag; disagreement keeps the e4b value + marks it disputed (hotter).
        disputed, hand_ensemble = apply_hand_ensemble(
            image_b64, doc_type, fields, _hand_ensemble_model()
        )
        low_confidence = {key: True for key in fields}
    return {
        "status": "extracted",
        "doc_type": doc_type,
        "fields": fields,
        "classify_raw": classify_raw,
        "extract_raw": extract_raw,
        "retried": retried,
        "re_asks": n_reasks,
        "region_calls": n_region,
        "region_replacements": region_replacements,
        "handwritten": handwritten,
        "hand_ensemble": hand_ensemble,
        "disputed": disputed,
        "low_confidence": low_confidence,
    }
