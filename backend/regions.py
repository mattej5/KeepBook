"""Per-(doc_type, field) crop regions for the REGION_PASS strategy.

Regions are stored as FRACTIONS of image width/height, never pixels, so the same
table survives the conditional preprocess step and any rendering resolution: a
clean render is ~1700x1057 (W-2/1099/1098) or ~1700x2157 (K-1), and a
preprocessed phone photo de-warps back to ~1700x1055, so the same fractional box
lands on the same form box in both cases (verified by cropping real testset
images and preprocessed photos and reading the crops — the box label + value are
inside every crop).

Each box is padded generously so the printed box label (e.g. "1 Wages, tips" or
"RECIPIENT'S/LENDER'S name") is INSIDE the crop, giving the model the context it
needs when it sees only a fragment of the form.

Fractions were derived from eval/gen_forms.py's draw coordinates (the ground
truth that PLACES each value) and the blank-form layouts, then hand-verified.

Public API:
  region_box(doc_type, field)   -> (x0, y0, x1, y1) fractions, or None
  region_desc(doc_type, field)  -> human box description for the prompt, or None
  crop_region_b64(image_b64, doc_type, field) -> cropped-region base64, or None
  looks_like_label(value)       -> True if value echoes a known box-label string

Fields without a region entry return None everywhere -> the caller skips them.
"""

import base64
import binascii
import io
import re

try:  # PIL is present in both venvs; guard so an import failure can't break intake
    from PIL import Image
except Exception:  # noqa: BLE001
    Image = None


# ---------------------------------------------------------------------------
# Region table: doc_type -> field -> {"box": (x0,y0,x1,y1) fractions,
#                                     "desc": prompt description,
#                                     "label": on-form box label string}
# box coordinates are fractions of (width, height), 0..1.
# ---------------------------------------------------------------------------
REGIONS = {
    "W-2": {
        "ssn": {
            "box": (0.1755, 0.0860, 0.4931, 0.1805),
            "desc": 'box a, "Employee\'s social security number"',
            "label": "Employee's social security number",
        },
        "employer": {
            "box": (0.0500, 0.2077, 0.4500, 0.3163),
            "desc": 'box c, "Employer\'s name, address, and ZIP code"',
            "label": "Employer's name, address, and ZIP code",
        },
        "employee_name": {
            "box": (0.0500, 0.4622, 0.4500, 0.5660),
            "desc": 'box e, "Employee\'s first name and initial, Last name"',
            "label": "Employee's first name and initial Last name",
        },
        "box1_wages": {
            "box": (0.4386, 0.1374, 0.7033, 0.2460),
            "desc": 'box 1, "Wages, tips, other compensation"',
            "label": "Wages, tips, other compensation",
        },
        "box2_fed_withheld": {
            "box": (0.6513, 0.1374, 0.8925, 0.2460),
            "desc": 'box 2, "Federal income tax withheld"',
            "label": "Federal income tax withheld",
        },
    },
    "1099-NEC": {
        "payer": {
            "box": (0.0621, 0.0846, 0.4209, 0.2407),
            "desc": 'the "PAYER\'S name" box (top-left), the payer\'s name only',
            "label": "PAYER'S name street address city or town state or province country ZIP or foreign postal code and telephone no.",
        },
        "recipient_name": {
            "box": (0.0654, 0.4704, 0.4242, 0.5697),
            "desc": 'the "RECIPIENT\'S name" box, the recipient\'s name only',
            "label": "RECIPIENT'S name",
        },
        "recipient_tin": {
            "box": (0.2235, 0.3684, 0.4235, 0.4725),
            "desc": 'the "RECIPIENT\'S TIN" box',
            "label": "RECIPIENT'S TIN",
        },
        "box1_nonemployee_comp": {
            "box": (0.3980, 0.2928, 0.6510, 0.4016),
            "desc": 'box 1, "Nonemployee compensation"',
            "label": "Nonemployee compensation",
        },
    },
    "1099-INT": {
        "payer": {
            "box": (0.0621, 0.0926, 0.4209, 0.2488),
            "desc": 'the "PAYER\'S name" box (top-left), the payer\'s name only',
            "label": "PAYER'S name street address city or town state or province country ZIP or foreign postal code and telephone no.",
        },
        "recipient_name": {
            "box": (0.0654, 0.4688, 0.4242, 0.5729),
            "desc": 'the "RECIPIENT\'S name" box, the recipient\'s name only',
            "label": "RECIPIENT'S name",
        },
        "box1_interest_income": {
            "box": (0.3915, 0.1889, 0.6503, 0.3072),
            "desc": 'box 1, "Interest income"',
            "label": "Interest income",
        },
    },
    "1098": {
        "lender": {
            "box": (0.0621, 0.0925, 0.4209, 0.2486),
            "desc": 'the "RECIPIENT\'S/LENDER\'S name" box (top-left), the lender\'s name only',
            "label": "RECIPIENT'S/LENDER'S name street address city or town state or province country ZIP or foreign postal code and telephone no.",
        },
        "borrower_name": {
            "box": (0.0654, 0.4683, 0.4242, 0.5724),
            "desc": 'the "PAYER\'S/BORROWER\'S name" box, the borrower\'s name only',
            "label": "PAYER'S/BORROWER'S name",
        },
        "box1_mortgage_interest": {
            "box": (0.3948, 0.2470, 0.6536, 0.3700),
            "desc": 'box 1, "Mortgage interest received from payer(s)/borrower(s)"',
            "label": "Mortgage interest received from payer(s)/borrower(s)",
        },
    },
    "K-1": {
        "partnership_ein": {
            "box": (0.0657, 0.2022, 0.4039, 0.2462),
            "desc": 'Part I item A, "Partnership\'s employer identification number"',
            "label": "Partnership's employer identification number",
        },
        "partnership_name": {
            "box": (0.0657, 0.2437, 0.4275, 0.2900),
            "desc": 'Part I item B, "Partnership\'s name, address, city, state, and ZIP code", the name only',
            "label": "Partnership's name, address, city, state, and ZIP code",
        },
        "partner_name": {
            "box": (0.0657, 0.3737, 0.4275, 0.4247),
            "desc": 'Part II item F, the partner\'s name only',
            "label": "Name, address, city, state, and ZIP code for partner entered in E",
        },
        "ordinary_income": {
            "box": (0.4036, 0.0878, 0.6859, 0.1388),
            "desc": 'Part III box 1, "Ordinary business income (loss)"',
            "label": "Ordinary business income (loss)",
        },
    },
}

# Upscale a crop whose long side is below this, so small glyphs (K-1 body text,
# TINs) survive the vision encoder's fixed internal downsample.
_MIN_CROP_LONG_SIDE = 1024


def region_box(doc_type: str, field: str):
    entry = REGIONS.get(doc_type, {}).get(field)
    return entry["box"] if entry else None


def region_desc(doc_type: str, field: str):
    entry = REGIONS.get(doc_type, {}).get(field)
    return entry["desc"] if entry else None


def has_region(doc_type: str, field: str) -> bool:
    return field in REGIONS.get(doc_type, {})


# ---------------------------------------------------------------------------
# Label blacklist: normalized box-label strings the model must never return as
# a *value*. Built from the "label" of every region entry, plus the short
# role words that a confused model tends to echo.
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(s)).split()).casefold()


_LABEL_STRINGS = set()
for _dt in REGIONS.values():
    for _entry in _dt.values():
        _LABEL_STRINGS.add(_norm(_entry["label"]))
# Common role-word echoes (short labels a model may emit verbatim).
_LABEL_STRINGS.update(
    _norm(s)
    for s in (
        "payer's name", "recipient's name", "lender's name", "borrower's name",
        "employer's name", "employee's name", "partner's name", "partnership's name",
        "payer", "recipient", "lender", "borrower", "employer", "employee",
        "name", "street address", "ordinary business income", "wages tips other compensation",
        "interest income", "nonemployee compensation", "employer identification number",
    )
)


def looks_like_label(value) -> bool:
    """True if `value` echoes a known box-label string (deterministic).

    Match is: normalized value equals a blacklist entry, OR a short blacklist
    entry is fully contained in the normalized value (catches "the payer's name
    is ..." style echoes). Long values (a real name is short) that merely share
    a word are NOT matched.
    """
    v = _norm(value)
    if not v:
        return True
    if v in _LABEL_STRINGS:
        return True
    for lab in _LABEL_STRINGS:
        # containment only for multi-word labels, to avoid a real name that
        # happens to contain "name" tripping a 1-word entry.
        if " " in lab and (lab in v or v in lab):
            return True
    return False


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------
def crop_region_b64(image_b64: str, doc_type: str, field: str):
    """Crop the (doc_type, field) region from a base64 PNG/JPEG image.

    Returns the cropped region as base64 PNG, or None if there is no region for
    this field or anything fails (caller then skips the field). Never raises.
    """
    box = region_box(doc_type, field)
    if box is None or Image is None:
        return None
    try:
        raw = base64.b64decode(image_b64)
    except (binascii.Error, ValueError):
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        x0, y0, x1, y1 = box
        px = (
            max(0, int(x0 * w)),
            max(0, int(y0 * h)),
            min(w, int(x1 * w)),
            min(h, int(y1 * h)),
        )
        if px[2] - px[0] < 8 or px[3] - px[1] < 8:
            return None
        crop = img.crop(px)
        long_side = max(crop.size)
        if long_side < _MIN_CROP_LONG_SIDE:
            scale = _MIN_CROP_LONG_SIDE / float(long_side)
            crop = crop.resize(
                (int(round(crop.size[0] * scale)), int(round(crop.size[1] * scale))),
                Image.BICUBIC,
            )
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 - a bad crop must never break intake
        return None
