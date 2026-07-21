"""Server-side PDF rendering + on-device decryption for KeepBook intake.

ROADMAP Phase 2, Tier C #9. Real firms receive PDFs — bank statements arrive as
password-protected PDFs over email (field signal). KeepBook accepts only images
in the pipeline, so at intake every PDF page is rendered to a PNG here and each
page then flows through the existing classify/extract path as its own document.

Decryption stays on this device: the password is used ONLY to open the document
in memory. It is never written to state.json/events.jsonl/raws or any log, and
never appears in an error — the typed errors below carry the filename only.

Rendering uses **pypdfium2** (BSD-3-Clause / Apache-2.0 — verified from the
installed package metadata), NOT PyMuPDF (AGPL).
"""

import io
import os

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

# %PDF magic. A file is treated as a PDF when its bytes begin with this OR its
# filename ends in ".pdf". Magic bytes are authoritative: a file whose content
# starts with %PDF is a PDF regardless of extension. The ".pdf" extension is the
# secondary trigger — a ".pdf" file whose content is NOT %PDF still enters the
# PDF path, where it is rejected as an unreadable/corrupt PDF (400) rather than
# being silently guessed at as an image. (See is_pdf.)
PDF_MAGIC = b"%PDF"

# Target render resolution. pypdfium renders at 72 DPI * scale, so scale = DPI/72
# yields a true ~200 DPI page (verified legible on a testset-derived page). The
# scale is derived from the page's own point size, and clamped so a pathologically
# large page can't produce a multi-hundred-megapixel bitmap.
RENDER_DPI = 200
_BASE_SCALE = RENDER_DPI / 72.0
MAX_SIDE_PX = 4000

# Page cap: reject the whole file beyond this many pages (consistent with the
# existing one-bad-file-fails-the-batch intake behavior).
PDF_PAGE_CAP = 20


class PdfError(Exception):
    """Base for PDF intake failures. Carries the filename only — never password
    material — so a caller can build a 400 detail without leaking anything."""

    def __init__(self, message: str, filename: str):
        super().__init__(message)
        self.filename = filename


class PasswordRequired(PdfError):
    """Encrypted PDF, no (usable) password supplied."""


class PasswordIncorrect(PdfError):
    """Encrypted PDF, a password was supplied but rejected."""


class PdfTooManyPages(PdfError):
    """PDF exceeds PDF_PAGE_CAP pages."""


class PdfUnreadable(PdfError):
    """Zero-byte, corrupt, or otherwise undecodable PDF."""


def is_pdf(data: bytes, filename: str) -> bool:
    """True if these bytes should be routed to the PDF path.

    %PDF magic wins over extension: a non-.pdf file whose content is %PDF is
    rendered as a PDF; a .pdf file whose content is not %PDF is still routed here
    (and will fail as PdfUnreadable) rather than mis-parsed as an image.
    """
    if data[:4] == PDF_MAGIC:
        return True
    ext = os.path.splitext(filename or "")[1].lower()
    return ext == ".pdf"


def _render_scale(w_pt: float, h_pt: float) -> float:
    """~200 DPI scale, clamped by the page's own size so the longest rendered
    side never exceeds MAX_SIDE_PX (guards against a poster-size page)."""
    longest = max(w_pt or 1.0, h_pt or 1.0)
    scale = _BASE_SCALE
    if longest * scale > MAX_SIDE_PX:
        scale = MAX_SIDE_PX / longest
    return scale


def render_pdf_pages(data: bytes, filename: str, password=None) -> list:
    """Render every page of one PDF to PNG bytes (one entry per page, in order).

    Raises (before returning anything, so intake stays all-or-nothing):
      * PasswordRequired    — encrypted, no usable password supplied
      * PasswordIncorrect   — encrypted, wrong password supplied
      * PdfTooManyPages     — more than PDF_PAGE_CAP pages
      * PdfUnreadable       — zero-byte / corrupt / empty

    `password` is used only to open the document; it is not retained.
    """
    # Treat "", whitespace-only, and None all as "no password supplied" so the
    # required-vs-incorrect distinction keys on real intent, not on an empty box.
    pw = password if (password and str(password).strip()) else None

    try:
        pdf = pdfium.PdfDocument(data, password=pw)
    except pdfium.PdfiumError as exc:
        code = getattr(exc, "err_code", None)
        is_password = code == pdfium_c.FPDF_ERR_PASSWORD or "password" in str(exc).lower()
        if is_password:
            if pw is None:
                raise PasswordRequired("password_required", filename) from None
            raise PasswordIncorrect("password_incorrect", filename) from None
        raise PdfUnreadable(f"{filename}: unreadable or corrupt PDF", filename) from None

    try:
        n_pages = len(pdf)
        if n_pages == 0:
            raise PdfUnreadable(f"{filename}: PDF has no pages", filename)
        if n_pages > PDF_PAGE_CAP:
            raise PdfTooManyPages(
                f"{filename}: PDF has {n_pages} pages; the limit is "
                f"{PDF_PAGE_CAP} pages per file",
                filename,
            )
        pages_png = []
        for i in range(n_pages):
            page = pdf[i]
            try:
                w_pt, h_pt = page.get_size()
                bitmap = page.render(scale=_render_scale(w_pt, h_pt))
                try:
                    pil = bitmap.to_pil()
                    buf = io.BytesIO()
                    pil.convert("RGB").save(buf, format="PNG")
                    pages_png.append(buf.getvalue())
                finally:
                    bitmap.close()
            finally:
                page.close()
        return pages_png
    finally:
        pdf.close()
