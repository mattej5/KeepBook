"""Empirical calibration for backend/dedup.py (ROADMAP Phase 2 Tier A #1).

Two-stage scheme (see backend/dedup.py docstring for the why):

  Stage 1 — 256-bit dHash prefilter (THRESHOLD, Hamming distance).
  Stage 2 — pixel-difference confirm (DIFF_SIZE/DIFF_DELTA/DIFF_MATCH_MAX_PX).

This script measures, over eval/testset/ (all genuinely DISTINCT documents) plus
synthesized true-duplicate variants (PNG re-encode, resize round-trips, JPEG
recompression, resize+JPEG combined):

  1. Stage-1 Hamming distances: same-type distinct pairs, cross-type distinct
     pairs, and the true-dup band. FINDING (holds at 64/256/576/1024-bit,
     measured 2026-07-20): same-type distinct synthetics collapse to distance 0 —
     the shared blank template dominates — so the hash ALONE cannot separate a
     re-encoded copy (also ~0) from a different person's same-type form. The
     prefilter is therefore calibrated for RECALL (catch the true-dup band, stay
     below the cross-type floor to bound stage-2 work), not for precision.
  2. Stage-2 strongly-differing-pixel counts: true-dup variants must land at/near
     zero; same-type DIFFERENT-people pairs must land clearly above the cutoff.
     This is the discriminator the hash cannot provide.

Prints both tables plus pass/fail checks against the shipped constants.
Committed reference run: eval/dedup_calibration_output.txt.

Run:  <venv>/bin/python eval/dedup_calibration.py
"""

import io
import json
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import dedup  # noqa: E402

TESTSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testset")
LABELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "labels.json")


def _reencode_png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return Image.open(io.BytesIO(buf.getvalue()))


def _jpeg(im, quality):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buf.getvalue()))


def _resize_roundtrip(im, factor):
    w, h = im.size
    small = im.resize((max(1, int(w * factor)), max(1, int(h * factor))))
    return small.resize((w, h))


def _downscale(im, factor):
    """SINGLE downscale — what a smaller-copy upload actually is (no round-trip)."""
    w, h = im.size
    return im.resize((max(1, int(w * factor)), max(1, int(h * factor))))


# True-dup variants: realistic re-captures of the SAME file (re-encodes, JPEG
# recompression, single downscales, and resize round-trips). Extreme downscales
# (<=0.35x) are measured separately as KNOWN MISSES (text aliasing floods stage 2).
VARIANTS = [
    ("png-reencode", _reencode_png),
    ("jpeg q85", lambda im: _jpeg(im, 85)),
    ("jpeg q60", lambda im: _jpeg(im, 60)),
    ("jpeg q30", lambda im: _jpeg(im, 30)),
    ("downscale x0.5", lambda im: _downscale(im, 0.5)),
    ("downscale x0.5 + jpeg q70", lambda im: _jpeg(_downscale(im, 0.5), 70)),
    ("resize x0.5 round-trip", lambda im: _resize_roundtrip(im, 0.5)),
    ("resize x0.75 round-trip", lambda im: _resize_roundtrip(im, 0.75)),
    ("resize x0.6 + jpeg q70", lambda im: _jpeg(_resize_roundtrip(im, 0.6), 70)),
]
KNOWN_MISS_VARIANTS = [
    ("downscale x0.35", lambda im: _downscale(im, 0.35)),
    ("downscale x0.25", lambda im: _downscale(im, 0.25)),
]

SAMPLE = ["w2_clean_01.png", "1099int_clean_01.png", "k1_clean_01.png",
          "1098_clean_01.png", "w2_photo_01.png"]

# Same-type DIFFERENT-people pairs (the demo scenario stage 2 must NOT flag).
DISTINCT_SAME_TYPE = [
    ("w2_clean_01.png", "w2_clean_02.png"), ("w2_clean_01.png", "w2_clean_03.png"),
    ("w2_clean_02.png", "w2_clean_03.png"),
    ("1098_clean_01.png", "1098_clean_02.png"), ("k1_clean_01.png", "k1_clean_02.png"),
    ("1099nec_clean_01.png", "1099nec_clean_02.png"),
    ("1099nec_clean_01.png", "1099nec_clean_03.png"),
    ("1099nec_clean_02.png", "1099nec_clean_03.png"),
    ("1099int_clean_01.png", "1099int_clean_02.png"),
]


def main():
    labels = json.load(open(LABELS))

    def typ(n):
        return labels.get(n, {}).get("doc_type", "OTHER")

    names = sorted(n for n in os.listdir(TESTSET) if n.lower().endswith((".png", ".jpg", ".jpeg")))
    imgs = {}
    hashes = {}
    for n in names:
        im = Image.open(os.path.join(TESTSET, n))
        im.load()
        imgs[n] = im
        hashes[n] = dedup.dhash_hex(im)

    bits = dedup.HASH_SIZE * dedup.HASH_SIZE
    print(f"scheme: {bits}-bit dHash ({dedup.HASH_SIZE + 1}x{dedup.HASH_SIZE} grid, "
          f"{dedup.PHASH_HEX_LEN} hex) + pixel confirm "
          f"({dedup.DIFF_SIZE}^2 gray, delta {dedup.DIFF_DELTA}, cutoff {dedup.DIFF_MATCH_MAX_PX})")
    print(f"testset images: {len(names)}")

    # ---------------- Stage 1: Hamming distances ----------------
    same, cross = [], []
    for i in range(len(names)):
        for k in range(i + 1, len(names)):
            a, b = names[i], names[k]
            d = dedup.hamming(hashes[a], hashes[b])
            (same if typ(a) == typ(b) else cross).append((d, a, b))
    same.sort()
    cross.sort()
    min_same, min_cross = same[0][0], cross[0][0]

    print("\n== STAGE 1 (dHash prefilter) ==")
    print("-- closest SAME-type distinct pairs (shared template -> collide; stage 2's job) --")
    for d, a, b in same[:6]:
        print(f"  dist {d:3d}   {a}  vs  {b}")
    print("-- closest CROSS-type distinct pairs (bound on prefilter width) --")
    for d, a, b in cross[:6]:
        print(f"  dist {d:3d}   {a} ({typ(a)})  vs  {b} ({typ(b)})")
    print("-- TRUE-DUP hash distances (prefilter must catch: dist <= THRESHOLD) --")
    dup_dists = []
    for n in SAMPLE:
        for label, fn in VARIANTS:
            dv = dedup.hamming(hashes[n], dedup.dhash_hex(fn(imgs[n])))
            dup_dists.append(dv)
            print(f"  dist {dv:3d}   {n}  ->  {label}")
    max_dup_dist = max(dup_dists)

    # ---------------- Stage 2: pixel-diff counts ----------------
    print("\n== STAGE 2 (pixel-difference confirm) ==")
    print(f"-- TRUE-DUP variants (must CONFIRM: count <= {dedup.DIFF_MATCH_MAX_PX}) --")
    dup_counts = []
    for n in SAMPLE:
        for label, fn in VARIANTS:
            c = dedup.pixel_diff_count(imgs[n], fn(imgs[n]))
            dup_counts.append(c)
            print(f"  count {c:5d}   {n}  ->  {label}")
    max_dup_count = max(dup_counts)
    print(f"-- SAME-type DIFFERENT-people pairs (must REJECT: count > {dedup.DIFF_MATCH_MAX_PX}) --")
    distinct_counts = []
    for a, b in DISTINCT_SAME_TYPE:
        c = dedup.pixel_diff_count(imgs[a], imgs[b])
        distinct_counts.append(c)
        print(f"  count {c:5d}   {a}  vs  {b}")
    min_distinct_count = min(distinct_counts)
    print("-- KNOWN-MISS variants (accepted false negatives; recorded, not required) --")
    for n in SAMPLE:
        for label, fn in KNOWN_MISS_VARIANTS:
            c = dedup.pixel_diff_count(imgs[n], fn(imgs[n]))
            print(f"  count {c:5d}   {n}  ->  {label}")

    # ---------------- Summary + checks ----------------
    print("\n================ SUMMARY ================")
    print(f"stage1 same-type distinct floor      : {min_same}  (hash CANNOT separate; stage 2 must)")
    print(f"stage1 cross-type distinct floor     : {min_cross}")
    print(f"stage1 true-dup band max             : {max_dup_dist}")
    print(f"stage1 THRESHOLD (shipped)           : {dedup.THRESHOLD}")
    print(f"stage2 true-dup count max            : {max_dup_count}")
    print(f"stage2 same-type distinct count min  : {min_distinct_count}")
    print(f"stage2 cutoff (shipped)              : {dedup.DIFF_MATCH_MAX_PX}")
    ok1 = dedup.THRESHOLD >= max_dup_dist
    ok2 = dedup.THRESHOLD < min_cross
    ok3 = max_dup_count <= dedup.DIFF_MATCH_MAX_PX
    ok4 = min_distinct_count > dedup.DIFF_MATCH_MAX_PX
    print(f"CHECK prefilter catches true-dup band          : {ok1}")
    print(f"CHECK prefilter below cross-type floor         : {ok2}")
    print(f"CHECK confirm accepts every true-dup variant   : {ok3}")
    print(f"CHECK confirm rejects every same-type distinct : {ok4}")
    print(f"ALL CHECKS PASS: {ok1 and ok2 and ok3 and ok4}")


if __name__ == "__main__":
    main()
