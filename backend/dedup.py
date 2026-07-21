"""Perceptual-hash duplicate detection for KeepBook intake.

ROADMAP Phase 2, Tier A #1 (also closes IMPROVEMENTS #14: zero-byte/duplicate
uploads accepted silently). Model proposes, human confirms — a near-duplicate is
only ever FLAGGED, never auto-dropped.

At intake every accepted image gets:
  * sha256   — exact-byte identity (literal double-drop of the same file).
  * dHash    — a 64-bit difference hash, robust to re-encode / resize / JPEG
               recompression (emailed scan vs. phone photo of the same page).

A new upload that exactly matches OR lands within THRESHOLD Hamming distance of any
existing non-deleted document's dHash is flagged `duplicate_of: <nearest id>` and
still runs the normal classify/extract pipeline. Byte-identical uploads necessarily
produce an identical dHash (distance 0 <= THRESHOLD), so the dHash comparison
subsumes the exact-sha256 clause; sha256 is used by the caller as a fast O(1)
exact-match index (backend/main.py `_SHA_TO_DOC`).

dHash: grayscale, resize to 9x8, compare each pixel to its right neighbour -> 64
bits. PIL only, no new dependencies.
"""

import hashlib
import io

from PIL import Image

try:  # Pillow >= 9.1 exposes the Resampling enum; keep a fallback for old wheels.
    _RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - only on ancient Pillow
    _RESAMPLE = Image.LANCZOS

# dHash grid: an 8x8 comparison grid needs a 9-wide (HASH_SIZE+1) resize so each of
# the 8 rows yields 8 left>right comparisons -> 64 bits.
HASH_SIZE = 8

# Empirically calibrated in eval/dedup_calibration.py against the 32-image
# eval/testset plus re-encoded/resized/JPEG true-dup pairs. KEY FINDING: a 64-bit
# dHash cannot separate two DIFFERENT same-type synthetic forms — they share a
# pixel-identical blank template, so they collapse to distance 0. There is thus NO
# strictly zero-false-positive threshold (same-type floor = 0). We ship the largest
# CROSS-type-safe value: the nearest DIFFERENT-type pair is at distance 6, so
# THRESHOLD = 5 keeps different document types distinct while catching the true-dup
# band (re-encode/resize/JPEG at 0-2, cushion 3). The residual same-type template
# collision is an inherent limit of the pinned 64-bit hash; it is safe because the
# feature only FLAGS (human confirms via side-by-side compare — never auto-drops).
# NOTE: a real phone-photo of the same scan sits ~26-32 away and is NOT caught by
# dHash; reliable catches are exact re-drop (sha256) + light re-encode.
THRESHOLD = 5


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
    """64-bit difference hash of a PIL image, as a 16-char hex string."""
    small = im.convert("L").resize((HASH_SIZE + 1, HASH_SIZE), _RESAMPLE)
    # For mode "L", tobytes() is one byte per pixel, row-major, no padding.
    px = small.tobytes()
    width = HASH_SIZE + 1
    bits = 0
    for row in range(HASH_SIZE):
        base = row * width
        for col in range(HASH_SIZE):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:016x}"


def hamming(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two 64-bit dHash hex strings."""
    return bin(int(a_hex, 16) ^ int(b_hex, 16)).count("1")


def find_duplicate(new_phash: str, candidates, threshold: int = THRESHOLD):
    """Return (nearest_doc_id, distance) or (None, None).

    candidates: iterable of (doc_id, phash_hex) for existing non-deleted documents.
    Picks the strictly-nearest candidate within `threshold`; ties resolve to the
    first candidate seen (caller passes them in insertion order). Candidates with a
    missing/None phash (e.g. old state files, or documents that pre-date this
    feature) are skipped — they simply never match.
    """
    if not new_phash:
        return None, None
    best_id, best_dist = None, None
    for doc_id, phash in candidates:
        if not phash:
            continue
        try:
            dist = hamming(new_phash, phash)
        except (ValueError, TypeError):
            continue
        if dist <= threshold and (best_dist is None or dist < best_dist):
            best_id, best_dist = doc_id, dist
    return best_id, best_dist
