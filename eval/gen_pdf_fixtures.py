"""Build the PDF-intake test fixtures from the committed eval/testset images.

Rebuilt at test time (backend/tests/test_pdf_intake.py imports this) so the repo
stays free of committed PDF binaries and the fixtures always track the current
testset. Four fixtures:

  * single_page.pdf   — one testset image wrapped as a 1-page PDF
  * three_page.pdf    — three DIFFERENT testset images as a 3-page PDF
  * encrypted.pdf     — the single-page PDF, encrypted with TEST_PASSWORD
  * oversized_21.pdf  — 21 pages, to exercise the 20-page cap (rejected 400)

Image → PDF uses Pillow. Encryption uses **pypdf** (pure-python, TEST-only — see
eval/requirements.txt), never a backend runtime dependency. The backend renders
these back to PNGs at intake with pypdfium2.

CLI:  python eval/gen_pdf_fixtures.py [out_dir]
"""

import io
import os

from PIL import Image

# TEST-ONLY password. Deliberately distinctive so a diff grep can prove it lives
# ONLY in test/fixture code and never in backend runtime, logs, or state.
TEST_PASSWORD = "keepbook-test-pw"

TESTSET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testset")

# Three distinct testset forms → three distinct dHashes, so the 3-page PDF's
# pages are not flagged as duplicates of each other.
THREE_PAGE_SOURCES = ["w2_clean_01.png", "1099int_clean_01.png", "1098_clean_01.png"]
SINGLE_PAGE_SOURCE = "w2_clean_01.png"


def _img(name: str) -> Image.Image:
    return Image.open(os.path.join(TESTSET, name)).convert("RGB")


def _images_to_pdf_bytes(images, resolution: float = 150.0) -> bytes:
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="PDF",
        save_all=True,
        append_images=list(images[1:]),
        resolution=resolution,
    )
    return buf.getvalue()


def single_page_pdf_bytes() -> bytes:
    return _images_to_pdf_bytes([_img(SINGLE_PAGE_SOURCE)])


def three_page_pdf_bytes() -> bytes:
    return _images_to_pdf_bytes([_img(n) for n in THREE_PAGE_SOURCES])


def encrypted_pdf_bytes(password: str = TEST_PASSWORD) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(single_page_pdf_bytes()))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def oversized_pdf_bytes(n_pages: int = 21) -> bytes:
    # Downscale a real page so 21 copies stay small and fast; content is
    # irrelevant — the cap keys on page count, checked before any page renders.
    base = _img(SINGLE_PAGE_SOURCE)
    small = base.resize((max(1, base.width // 3), max(1, base.height // 3)))
    return _images_to_pdf_bytes([small.copy() for _ in range(n_pages)])


def build_all(out_dir: str) -> dict:
    """Write all four fixtures into out_dir; return {name: path}."""
    os.makedirs(out_dir, exist_ok=True)
    specs = {
        "single_page.pdf": single_page_pdf_bytes(),
        "three_page.pdf": three_page_pdf_bytes(),
        "encrypted.pdf": encrypted_pdf_bytes(),
        "oversized_21.pdf": oversized_pdf_bytes(21),
    }
    paths = {}
    for name, data in specs.items():
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        paths[name] = path
    return paths


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pdf_fixtures"
    )
    for name, path in build_all(out).items():
        print(f"{name}: {path} ({os.path.getsize(path)} bytes)")
