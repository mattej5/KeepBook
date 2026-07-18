"""Regression tests for the synthetic tax-form generator.

These tests intentionally describe the desired output rather than the current
full-page/misplaced-field behavior.  They should remain red until gen_forms.py
is corrected.
"""

from pathlib import Path

import fitz
import numpy as np
import pytest
from PIL import Image

from eval import gen_forms


@pytest.fixture(scope="module")
def generated_testset(tmp_path_factory):
    """Run the deterministic generator once without touching eval/testset."""
    root = tmp_path_factory.mktemp("generated-forms")
    output_dir = root / "testset"
    output_dir.mkdir()

    original_output_dir = gen_forms.OUT_DIR
    original_labels_path = gen_forms.LABELS_PATH
    original_labels = gen_forms.labels
    try:
        gen_forms.OUT_DIR = str(output_dir)
        gen_forms.LABELS_PATH = str(root / "labels.json")
        gen_forms.labels = {}
        gen_forms.rng.seed(gen_forms.SEED)
        gen_forms.main()
        yield output_dir
    finally:
        gen_forms.OUT_DIR = original_output_dir
        gen_forms.LABELS_PATH = original_labels_path
        gen_forms.labels = original_labels
        gen_forms.rng.seed(gen_forms.SEED)


def _nonblank_bbox(image_path, near_white=245):
    pixels = np.asarray(Image.open(image_path).convert("RGB"))
    nonblank = np.any(pixels < near_white, axis=2)
    ys, xs = np.nonzero(nonblank)
    assert len(xs), f"{image_path.name} contains no visible content"
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1, pixels.shape[:2]


def test_clean_forms_are_cropped_to_content(generated_testset):
    """Clean form images must not retain a mostly blank PDF-page tail."""
    failures = []
    images = sorted(generated_testset.glob("*_clean_*.png"))
    assert images, "generator produced no clean form images"

    for image_path in images:
        _, _, _, content_bottom, (height, _) = _nonblank_bbox(image_path)
        blank_bottom_fraction = (height - content_bottom) / height
        if blank_bottom_fraction >= 0.15:
            failures.append(
                f"{image_path.name}: bottom {blank_bottom_fraction:.1%} is blank "
                f"(content ends at row {content_bottom} of {height})"
            )

    assert not failures, "clean forms must be content-cropped:\n" + "\n".join(failures)


def _employee_ssn_entry_region(pdf_path):
    """Derive box a's data region from the official W-2's printed labels."""
    with fitz.open(pdf_path) as document:
        page = document[2]
        words = page.get_text("words")

        for index, word in enumerate(words[:-2]):
            phrase = [item[4].lower().replace("’", "'") for item in words[index : index + 3]]
            if phrase == ["employee's", "social", "security"]:
                employee_label = word
                break
        else:
            raise AssertionError("official W-2 employee SSN label was not found")

        # Box a's value starts beneath its label and ends at the next row.  Its
        # right edge is the vertical divider just beyond the left half-page.
        x0 = employee_label[0] - 3
        y0 = employee_label[3]
        x1 = page.rect.width * 0.55
        y1 = y0 + 25
        return x0, y0, x1, y1


def test_w2_ssn_is_rendered_inside_employee_ssn_box(generated_testset):
    """The blue SSN overlay must appear in official W-2 box a."""
    image_path = generated_testset / "w2_clean_01.png"
    pixels = np.asarray(Image.open(image_path).convert("RGB"))
    x0, y0, x1, y1 = _employee_ssn_entry_region(
        Path(gen_forms.BLANK_DIR) / "fw2.pdf"
    )
    scale = gen_forms.ZOOM
    region = pixels[
        round(y0 * scale) : round(y1 * scale),
        round(x0 * scale) : round(x1 * scale),
    ]

    red = region[:, :, 0].astype(int)
    green = region[:, :, 1].astype(int)
    blue = region[:, :, 2].astype(int)
    blue_overlay_pixels = np.count_nonzero(
        (blue - red > 40) & (blue - green > 40)
    )

    # Also inspect the current erroneous area so a failure distinguishes field
    # misplacement from an unrelated failure to render any overlay at all.
    misplaced_region = pixels[
        round(y0 * scale) : round(y1 * scale),
        round(40 * scale) : round((x0 - 5) * scale),
    ]
    misplaced_red = misplaced_region[:, :, 0].astype(int)
    misplaced_green = misplaced_region[:, :, 1].astype(int)
    misplaced_blue = misplaced_region[:, :, 2].astype(int)
    misplaced_blue_pixels = np.count_nonzero(
        (misplaced_blue - misplaced_red > 40)
        & (misplaced_blue - misplaced_green > 40)
    )

    assert blue_overlay_pixels >= 20 and misplaced_blue_pixels < 20, (
        "W-2 SSN overlay is not inside box a: "
        f"official box has {blue_overlay_pixels} blue pixels, while the "
        f"top-left/control-number area has {misplaced_blue_pixels}"
    )
