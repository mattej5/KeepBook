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

from model_runtime import extract as model_extract


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
# Read at call time so eval/tests can toggle per run.
# ---------------------------------------------------------------------------
REASK_CAP = 3


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _classify_model() -> str:
    return os.environ.get("CASCADE_CLASSIFY_MODEL", "gemma4:e2b")


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
    types = ", ".join(f'"{t}"' for t in DOC_TYPES)
    return (
        "You are a US tax-document intake classifier. Look at this document image "
        "and decide which single IRS form it is.\n"
        "Return STRICT JSON only, no prose, no markdown:\n"
        '{"doc_type": "<TYPE>"}\n'
        f"where <TYPE> is EXACTLY one of: {types}, or "
        f'"{UNRECOGNIZED}" if it is not one of those forms (for example a '
        "receipt, a letter, or any non-tax document).\n"
        "Do not guess a tax form when the document is clearly not one — return "
        f'"{UNRECOGNIZED}" instead.'
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
    # Order matters: check the more specific 1099 variants first.
    if "1099" in t and "nec" in t:
        return "1099-NEC"
    if "1099" in t and "int" in t:
        return "1099-INT"
    if "1099" in t and "misc" in t:
        return "1099-MISC"
    if "1098" in t:
        return "1098"
    if re.search(r"\bw[\s\-]?2\b", t) or "wage and tax" in t:
        return "W-2"
    if re.search(r"\bk[\s\-]?1\b", t) or "schedule k" in t:
        return "K-1"
    if "unrecogni" in t or "unknown" in t or "not a tax" in t:
        return UNRECOGNIZED
    return UNRECOGNIZED


# ---------------------------------------------------------------------------
# Model calls with one retry
# ---------------------------------------------------------------------------
def _call_and_parse(image_b64: str, prompt: str, model: str = None):
    """Call the model, parse JSON, retry once on unparseable output.

    Returns (parsed_dict_or_None, raw_text_of_last_attempt, retried_bool).
    """
    raw = model_extract(image_b64, prompt, model=model)
    parsed = parse_json(raw)
    if parsed is not None:
        return parsed, raw, False
    raw2 = model_extract(image_b64, prompt, model=model)
    parsed2 = parse_json(raw2)
    return parsed2, raw2, True


def classify(image_b64: str, model: str = None):
    parsed, raw, retried = _call_and_parse(image_b64, build_classify_prompt(), model=model)
    if parsed is None:
        return UNRECOGNIZED, raw, retried
    return normalize_doc_type(parsed.get("doc_type")), raw, retried


def classify_cascade(image_b64: str):
    """CASCADE classify: small model first, confirm UNRECOGNIZED on the big one.

    A cheap small-model classify handles the common case. If the small model
    yields UNRECOGNIZED (real reject OR a small-model miss), re-run classify
    once on the extraction model before accepting the reject — this protects
    both the reject path and any form the small model failed to recognize.
    """
    dt, raw, retried = classify(image_b64, model=_classify_model())
    if dt == UNRECOGNIZED:
        dt2, raw2, retried2 = classify(image_b64)  # extraction model (MODEL_NAME)
        return dt2, raw2, (retried or retried2)
    return dt, raw, retried


def extract_fields(image_b64: str, doc_type: str, model: str = None):
    """Return (fields_dict_or_None, raw, retried). fields keyed per FIELD_SCHEMA."""
    parsed, raw, retried = _call_and_parse(
        image_b64, build_extract_prompt(doc_type), model=model
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
        raw = model_extract(image_b64, build_reask_prompt(doc_type, key), model=model)
        parsed = parse_json(raw)
        if not parsed:
            continue
        new_val = parsed.get("value", "")
        new_val = "" if new_val is None else str(new_val).strip()
        if new_val and field_format_ok(key, new_val):
            updated[key] = new_val  # accept: it now passes the format check
    return updated, n


def run_pipeline(image_b64: str) -> dict:
    """Full two-step pipeline for one document image.

    Returns a runtime-neutral result:
      {
        "status": "extracted" | "unrecognized",
        "doc_type": <canonical type or "UNRECOGNIZED">,
        "fields": {key: value_str},   # plain values; {} when unrecognized
        "classify_raw": str,
        "extract_raw": str | None,
        "retried": bool,   # either model call needed a retry (low_confidence signal)
        "re_asks": int,    # focused single-field follow-up calls issued (RE_ASK)
      }
    """
    image_b64 = _maybe_preprocess(image_b64)
    if _flag("CASCADE", "0"):
        doc_type, classify_raw, c_retried = classify_cascade(image_b64)
    else:
        doc_type, classify_raw, c_retried = classify(image_b64)
    if doc_type == UNRECOGNIZED:
        return {
            "status": "unrecognized",
            "doc_type": UNRECOGNIZED,
            "fields": {},
            "classify_raw": classify_raw,
            "extract_raw": None,
            "retried": c_retried,
            "re_asks": 0,
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
        }
    n_reasks = 0
    if _flag("RE_ASK", "0"):
        fields, n_reasks = apply_reask(image_b64, doc_type, fields)
    return {
        "status": "extracted",
        "doc_type": doc_type,
        "fields": fields,
        "classify_raw": classify_raw,
        "extract_raw": extract_raw,
        "retried": c_retried or e_retried,
        "re_asks": n_reasks,
    }
