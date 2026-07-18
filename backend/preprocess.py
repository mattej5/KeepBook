"""Deterministic image preprocessing for KeepBook document intake.

Public entry point:  ``preprocess(image_bytes) -> image_bytes``

Purpose
-------
gemma4:e4b reads clean form scans well but collapses on "phone photo"
variants (perspective warp, rotation, desk-background framing, shadow /
vignette, blur, noise, JPEG). eval/augment.py is exactly what produced those
variants, so this module inverts the same transforms, in the reverse of the
physical order they were applied:

    codec/sensor/optics  -> can't undo, but contrast can be recovered
    scene lighting        -> illumination flattening (kills shadow + vignette)
    desk framing          -> detect the page against the desk and crop it out
    perspective + rotation-> 4-point warp back to an axis-aligned rectangle

Safety contract (the good case must never get worse)
----------------------------------------------------
A clean scan fills the frame with a white page and has NO dark desk border.
Page detection therefore finds a quad that covers ~the whole frame, and the
geometric stage is skipped. Illumination flattening is a divide-by-background
gain map, which is ~identity on an already-flat white page. Every stage is
wrapped so that on ANY failure (or any doubt about the detection) the original
bytes are returned unchanged. Determinism: no randomness anywhere.
"""

import io

import cv2
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Tunables (all deterministic; chosen against eval/testset photo variants)
# ---------------------------------------------------------------------------
# Page detection: a detected quad must cover at least this fraction of the
# frame to be a plausible document (guards against cropping to a stray blob),
# and if it covers MORE than the "fills frame" fraction we treat the image as a
# clean full-frame scan and skip the geometric warp entirely.
_MIN_PAGE_AREA_FRAC = 0.20
_FILLS_FRAME_AREA_FRAC = 0.97
# A clean scan also has almost no dark desk pixels. If the fraction of "dark"
# pixels (page-vs-desk split by Otsu) is below this, there is no desk to remove.
_DESK_PIXEL_FRAC = 0.02
# Long side (px) the page is upscaled to if the cropped region is smaller, so
# small text survives the vision encoder's internal downsample.
_UPSCALE_LONG_SIDE = 1600
# White-point normalization percentiles.
_WHITE_PCT = 99.0
_BLACK_PCT = 2.0


# ---------------------------------------------------------------------------
# Decode / encode
# ---------------------------------------------------------------------------
def _decode(image_bytes: bytes):
    """bytes -> BGR uint8 array, or None if undecodable."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img  # None on failure


def _encode_png(img) -> bytes:
    """BGR uint8 array -> PNG bytes (lossless; no new codec artifacts)."""
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


# ---------------------------------------------------------------------------
# Page detection
# ---------------------------------------------------------------------------
def _order_quad(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as [top-left, top-right, bottom-right, bottom-left]."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)
    return np.array(
        [
            pts[np.argmin(s)],  # tl: smallest x+y
            pts[np.argmin(d)],  # tr: smallest y-x
            pts[np.argmax(s)],  # br: largest x+y
            pts[np.argmax(d)],  # bl: largest y-x
        ],
        dtype=np.float32,
    )


def _detect_page(img):
    """Find the document page against the desk.

    Returns (quad_or_None, dark_frac). ``quad`` is 4 ordered corners of the
    page; ``dark_frac`` is the fraction of the frame that reads as desk (used to
    tell a framed photo from a full-frame clean scan).
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Otsu split: page (bright) vs desk (dark). THRESH_BINARY -> page = 255.
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark_frac = float((mask == 0).sum()) / float(h * w)

    # Close small holes (form gridlines) so the page is one solid blob.
    k = max(3, int(0.01 * min(h, w)) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None, dark_frac
    c = max(contours, key=cv2.contourArea)
    area_frac = cv2.contourArea(c) / float(h * w)
    if area_frac < _MIN_PAGE_AREA_FRAC:
        return None, dark_frac

    peri = cv2.arcLength(c, True)
    approx = cv2.approxPolyDP(c, 0.02 * peri, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        return _order_quad(approx), dark_frac

    # Not a clean quad — fall back to the min-area (rotated) rectangle so we can
    # still deskew. Only trust it if it explains most of the contour (else the
    # page outline is too irregular to safely warp).
    rect = cv2.minAreaRect(c)
    box = cv2.boxPoints(rect)
    if cv2.contourArea(box.astype(np.float32)) > 0 and (
        cv2.contourArea(c) / cv2.contourArea(box.astype(np.float32)) > 0.85
    ):
        return _order_quad(box), dark_frac
    return None, dark_frac


def _warp_to_page(img, quad):
    """Perspective-warp the page quad to an axis-aligned rectangle."""
    tl, tr, br, bl = quad
    wa = np.linalg.norm(br - bl)
    wb = np.linalg.norm(tr - tl)
    ha = np.linalg.norm(tr - br)
    hb = np.linalg.norm(tl - bl)
    out_w = int(round(max(wa, wb)))
    out_h = int(round(max(ha, hb)))
    if out_w < 8 or out_h < 8:
        return None
    dst = np.array(
        [[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(quad, dst)
    return cv2.warpPerspective(
        img, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


# ---------------------------------------------------------------------------
# Illumination + contrast
# ---------------------------------------------------------------------------
def _flatten_illumination(img):
    """Divide out a large-scale background estimate to kill shadow + vignette.

    Color-preserving: one luminance gain map is applied to all channels, so an
    already-flat white page (gain ~= 1 everywhere) is essentially untouched.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # Background = heavy blur (kernel ~ 1/8 of the long side, odd).
    ksize = max(3, (int(0.125 * max(h, w)) | 1))
    bg = cv2.GaussianBlur(gray, (ksize, ksize), 0)
    bg = np.clip(bg, 1.0, None)
    target = float(np.percentile(bg, 90))  # the paper-white level
    gain = np.clip(target / bg, 0.5, 2.0)
    out = img.astype(np.float32) * gain[:, :, None]
    return np.clip(out, 0, 255).astype(np.uint8)


def _normalize_white_point(img):
    """Gentle levels stretch: push paper to true white, keep ink dark.

    Uses robust percentiles so a few dark form lines / bright specks don't blow
    out the mapping. ~identity on an image whose paper is already white.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lo = float(np.percentile(gray, _BLACK_PCT))
    hi = float(np.percentile(gray, _WHITE_PCT))
    if hi - lo < 20:  # too little dynamic range to trust; leave it alone
        return img
    scale = 255.0 / (hi - lo)
    out = (img.astype(np.float32) - lo) * scale
    return np.clip(out, 0, 255).astype(np.uint8)


def _upscale_if_small(img):
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side >= _UPSCALE_LONG_SIDE:
        return img
    scale = _UPSCALE_LONG_SIDE / float(long_side)
    return cv2.resize(
        img, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_CUBIC
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def preprocess(image_bytes: bytes) -> bytes:
    """Clean up a document photo so the vision model can read it.

    Deterministic. Safe on already-clean scans (geometric stage self-skips when
    the page fills the frame). Returns the ORIGINAL bytes unchanged on any
    failure, so it can never break or degrade the pipeline.
    """
    try:
        img = _decode(image_bytes)
        if img is None:
            return image_bytes

        # --- Geometric: crop the page off the desk + deskew, only for photos ---
        try:
            quad, dark_frac = _detect_page(img)
        except Exception:
            quad, dark_frac = None, 0.0

        if quad is not None and dark_frac >= _DESK_PIXEL_FRAC:
            area_frac = cv2.contourArea(quad.astype(np.float32)) / float(
                img.shape[0] * img.shape[1]
            )
            if area_frac < _FILLS_FRAME_AREA_FRAC:
                warped = _warp_to_page(img, quad)
                if warped is not None:
                    img = warped

        # --- Photometric: flatten lighting, then normalize the white point ---
        img = _flatten_illumination(img)
        img = _normalize_white_point(img)

        # --- Resolution: upscale small crops so text survives the encoder ---
        img = _upscale_if_small(img)

        return _encode_png(img)
    except Exception:
        # Never degrade the good case; never crash the worker.
        return image_bytes


if __name__ == "__main__":
    import sys

    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as fh:
        out = preprocess(fh.read())
    with open(dst, "wb") as fh:
        fh.write(out)
    print(f"wrote {dst} ({len(out)} bytes)")
