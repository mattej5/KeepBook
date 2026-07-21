"""Empirical THRESHOLD calibration for backend/dedup.py (ROADMAP Phase 2 Tier A #1).

Runs measurements over eval/testset/ (all genuinely DISTINCT documents) plus
synthesized true-duplicate pairs (PNG re-encode, resize round-trips, JPEG
recompression). It splits the distinct-doc distances into:

  * SAME-TYPE pairs   (two different people's W-2s, etc.)  and
  * CROSS-TYPE pairs  (a W-2 vs a 1099 — should never be confused).

FINDING (recorded 2026-07-20): a 64-bit dHash CANNOT separate two different
same-type synthetic forms — they share a pixel-identical blank template, so the
low-resolution hash collapses them to distance 0. The stated premise "none should
collide" is therefore false for same-type forms, and NO threshold >= 0 gives a
strictly zero false-positive testset. The largest CROSS-TYPE-safe threshold (below
the different-type floor) is what we ship; the same-type collision is an inherent
limit of the pinned 64-bit hash, reported as a known flag-only false-positive class.
(Separately: a real phone-photo of the same scan sits ~26-32 away — dHash does not
recognize it either. Reliable catches are exact re-drop (sha256) + light re-encode.)

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

    # (1) inter-doc distances, split same-type vs cross-type
    same, cross = [], []
    for i in range(len(names)):
        for k in range(i + 1, len(names)):
            a, b = names[i], names[k]
            d = dedup.hamming(hashes[a], hashes[b])
            (same if typ(a) == typ(b) else cross).append((d, a, b))
    same.sort()
    cross.sort()
    min_same = same[0][0]
    min_cross = cross[0][0]

    print(f"testset images: {len(names)}   same-type pairs: {len(same)}   cross-type pairs: {len(cross)}")
    print("\n-- closest SAME-type distinct pairs (shared blank template -> collide) --")
    for d, a, b in same[:6]:
        print(f"  dist {d:2d}   {a}  vs  {b}")
    print("\n-- closest CROSS-type distinct pairs (the meaningful FP floor) --")
    for d, a, b in cross[:6]:
        print(f"  dist {d:2d}   {a} ({typ(a)})  vs  {b} ({typ(b)})")

    # (2) true-duplicate pairs
    print("\n-- TRUE-DUP distances (must be caught) --")
    variants = [
        ("png-reencode", _reencode_png),
        ("resize x0.5 round-trip", lambda im: _resize_roundtrip(im, 0.5)),
        ("resize x0.75 round-trip", lambda im: _resize_roundtrip(im, 0.75)),
        ("jpeg q85", lambda im: _jpeg(im, 85)),
        ("jpeg q60", lambda im: _jpeg(im, 60)),
    ]
    sample = [n for n in ["w2_clean_01.png", "1099int_clean_01.png", "k1_clean_01.png",
                          "1098_clean_01.png", "w2_photo_01.png"] if n in imgs]
    true_dup_dists = []
    for n in sample:
        for label, fn in variants:
            dv = dedup.hamming(hashes[n], dedup.dhash_hex(fn(imgs[n])))
            true_dup_dists.append(dv)
            print(f"  dist {dv:2d}   {n}  ->  {label}")
    max_true = max(true_dup_dists)

    recommended = min_cross - 1  # largest threshold below the different-TYPE floor
    print("\n================ SUMMARY ================")
    print(f"min SAME-type distance (template collision) : {min_same}  <-- premise 'none collide' is FALSE")
    print(f"min CROSS-type distance (real FP floor)      : {min_cross}")
    print(f"max true-dup distance (must catch)           : {max_true}")
    print(f"largest cross-type-safe threshold            : {recommended}")
    print(f"dedup.THRESHOLD currently                    : {dedup.THRESHOLD}")
    print(f"margin above true-dup band (THRESH - max_dup) : {dedup.THRESHOLD - max_true}")
    print(f"cushion below cross-type floor (floor - THRESH): {min_cross - dedup.THRESHOLD}")
    print(f"THRESHOLD catches all true-dups?             : {dedup.THRESHOLD >= max_true}")
    print(f"THRESHOLD keeps DIFFERENT types distinct?    : {dedup.THRESHOLD < min_cross}")
    print(f"THRESHOLD strictly zero-FP on testset?       : {dedup.THRESHOLD < min_same}"
          f"  (impossible: same-type template floor = {min_same})")


if __name__ == "__main__":
    main()
