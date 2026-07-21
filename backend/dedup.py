"""Perceptual duplicate detection for KeepBook intake — two-stage.

ROADMAP Phase 2, Tier A #1 (also closes IMPROVEMENTS #14: zero-byte/duplicate
uploads accepted silently). Model proposes, human confirms — a near-duplicate is
only ever FLAGGED, never auto-dropped.

Why two stages (calibrated in eval/dedup_calibration.py, 2026-07-20): on
template-heavy tax forms a dHash at ANY practical resolution (measured 64/256/576/
1024-bit) collapses two DIFFERENT same-type synthetic forms to Hamming distance 0 —
the shared blank template dominates and the downsample erases the typed values. A
re-encoded copy of the SAME form also sits at ~0. The hash therefore cannot tell
"different person's W-2" from "re-encoded same W-2"; no threshold separates them.
What does separate them is pixel difference at a calibrated working size: a
re-encode/downscale differs only by compression/resampling noise (0 strongly-
differing pixels at 384^2, delta 60), while a different person's form differs
hard in the text regions (14-411 pixels).

Stage 1 — 256-bit dHash prefilter (cheap, index-friendly):
  grayscale -> resize 17x16 -> adjacent-pixel compare -> 256 bits (64 hex chars).
  Candidates = existing docs within THRESHOLD Hamming distance.
Stage 2 — pixel-difference confirm (runs only on stage-1 candidates):
  both images grayscale -> DIFF_SIZE^2 -> count pixels with |diff| > DIFF_DELTA.
  Count <= DIFF_MATCH_MAX_PX  => confirmed duplicate; otherwise NOT a duplicate
  (e.g. a different client's same-type form).

sha256 is the exact-byte shortcut: a literal re-drop matches it and skips stage 2.

PIL only, no new dependencies.
"""

import hashlib
import io

from PIL import Image, ImageChops

try:  # Pillow >= 9.1 exposes the Resampling enum; keep a fallback for old wheels.
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - only on ancient Pillow
    _RESAMPLE = Image.LANCZOS

# Stage 1 — dHash grid: a 16x16 comparison grid needs a 17-wide (HASH_SIZE+1)
# resize so each of the 16 rows yields 16 left>right comparisons -> 256 bits.
HASH_SIZE = 16
PHASH_HEX_LEN = HASH_SIZE * HASH_SIZE // 4  # 64 hex chars

# Stage-1 prefilter threshold (eval/dedup_calibration.py): the re-encode/resize/
# JPEG true-dup band measures 0-10 at 256 bits and the nearest DIFFERENT-type pair
# is at 37, so 16 catches the band with margin while bounding stage-2 work. The
# prefilter is deliberately recall-oriented: same-type distinct docs DO pass it
# (distance 0, see module docstring) and are then rejected by stage 2.
THRESHOLD = 16

# Stage 2 — pixel-difference confirm (eval/dedup_calibration.py). Working size and
# delta were SWEPT (512/384 x delta 60/100/140): 512px keeps resampling-aliasing
# noise from downscaled copies (up to 25 px), and delta>=100 anti-aliases the real
# text-change signal away; 384px + delta 60 is the operating point where EVERY
# realistic true-dup variant (PNG re-encode, JPEG q>=30, 0.5x downscale, 0.5x+JPEG,
# resize round-trips) measures 0 strongly-differing pixels while the closest
# DIFFERENT same-type pair (two people's K-1s) measures 14 and W-2 pairs ~100+.
# Cutoff 6 sits >2x below the distinct floor with headroom above the (zero) dup
# band. Known false negative: extreme downscales (<=0.35x) alias the text and
# exceed the cutoff — an accepted miss (flag-only feature; false negatives are
# acceptable, false-positive flags in Review are not).
DIFF_SIZE = 384
DIFF_DELTA = 60
DIFF_MATCH_MAX_PX = 6


class UnreadableImage(ValueError):
    """Raised when upload bytes are empty or not a PIL-decodable image."""


def compute_hashes(data: bytes):
    """Return (sha256_hex, dhash_hex). Raise UnreadableImage on empty/undecodable.

    This is the intake gate for IMPROVEMENTS #14: a zero-byte or non-image upload
    raises here so the caller can return HTTP 400 without ever creating a document.
    """
    if not data:
        raise UnreadableImage("empty upload (zero bytes)")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:  # noqa: BLE001 - PIL raises many types; all mean "not an image"
        raise UnreadableImage(f"not a decodable image ({exc})") from exc
    sha = hashlib.sha256(data).hexdigest()
    return sha, dhash_hex(im)


def dhash_hex(im) -> str:
    """256-bit difference hash of a PIL image, as a 64-char hex string."""
    small = im.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), _RESAMPLE)
    # For mode "L", tobytes() is one byte per pixel, row-major, no padding.
    px = small.tobytes()
    width = HASH_SIZE + 1
    bits = 0
    for row in range(HASH_SIZE):
        base = row * width
        for col in range(HASH_SIZE):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:0{PHASH_HEX_LEN}x}"


def dhash_from_file(path: str):
    """dHash of an image file under the CURRENT scheme, or None on any failure.

    Migration helper: a state file written by the old 64-bit scheme carries 16-hex
    phash values; the caller recomputes from the stored upload when possible.
    Best-effort — a missing/corrupt file returns None, never raises.
    """
    try:
        with Image.open(path) as im:
            im.load()
            return dhash_hex(im)
    except Exception:  # noqa: BLE001 - any IO/decode failure means "can't recompute"
        return None


def hamming(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two equal-length dHash hex strings."""
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


def find_candidates(new_phash: str, candidates, threshold: int = THRESHOLD):
    """Stage 1: [(doc_id, distance), ...] within `threshold`, nearest first.

    candidates: iterable of (doc_id, phash_hex) for existing non-deleted documents.
    Ties keep the caller's order (insertion order; sort is stable). A candidate
    with a missing/None phash, a phash whose LENGTH differs from the current
    scheme (a legacy 64-bit entry the caller could not recompute), or an
    unparseable value is skipped — never a crash, never a false flag on a
    length mismatch.
    """
    if not new_phash:
        return []
    out = []
    for doc_id, phash in candidates:
        if not phash or len(phash) != len(new_phash):
            continue
        try:
            dist = hamming(new_phash, phash)
        except (ValueError, TypeError):
            continue
        if dist <= threshold:
            out.append((doc_id, dist))
    out.sort(key=lambda t: t[1])
    return out


def pixel_diff_count(im_a, im_b) -> int:
    """Stage-2 metric: pixels (of DIFF_SIZE^2) whose grayscale |diff| > DIFF_DELTA."""
    ga = im_a.convert("L").resize((DIFF_SIZE, DIFF_SIZE), _RESAMPLE)
    gb = im_b.convert("L").resize((DIFF_SIZE, DIFF_SIZE), _RESAMPLE)
    hist = ImageChops.difference(ga, gb).histogram()
    return sum(hist[DIFF_DELTA + 1:])


def is_pixel_duplicate(new_bytes: bytes, existing_path: str) -> bool:
    """Stage 2: confirm a stage-1 candidate by full-resolution pixel difference.

    True only when the two images match within DIFF_MATCH_MAX_PX strongly-
    differing pixels. Any IO/decode failure returns False — a candidate we cannot
    verify is not flagged (never crash, never false-flag).
    """
    try:
        with Image.open(io.BytesIO(new_bytes)) as im_new, Image.open(existing_path) as im_old:
            im_new.load()
            im_old.load()
            return pixel_diff_count(im_new, im_old) <= DIFF_MATCH_MAX_PX
    except Exception:  # noqa: BLE001 - a candidate we cannot verify is not flagged
        return False
