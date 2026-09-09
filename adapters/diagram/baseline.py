"""The naive baseline the structured diagram adapter is measured against.

A diagram has no natural "flattened text" the way a spreadsheet or a
document does. The honest naive-pipeline equivalent — and a direct
illustration of the exact claim CLAUDE.md makes about diagrams
("image-only VLM prompting is known to fail badly on connection topology
specifically") — is: OCR every visible string in the image and nothing
else. No shape detection, no line/arrow detection, no `connects-to`
relationships, no node/edge ids. Just a flat, unordered bag of labels.

This reuses the exact same OCR primitives the document adapter's scanned-
page path already uses (`adapters.common.ocr.get_reader()` +
`group_detections_into_lines`) rather than re-implementing OCR grouping —
this is genuinely shared infrastructure, not diagram-adapter logic, the
same reasoning that put those functions in `adapters/common/ocr.py` in the
first place.

No node/edge is ever attached to a label here, on purpose: a naive OCR-only
pipeline has no way to know a piece of text sat inside a rectangle versus
next to an arrow, so it doesn't get to keep that fact.
"""

from __future__ import annotations

from pathlib import Path

import cv2

from adapters.common.ocr import get_reader, group_detections_into_lines, ocr_engine_status


def flatten_labels(path: str | Path) -> str:
    """Every OCR'd line in the image, in top-to-bottom/left-to-right order,
    as a flat "# label: labelN" list — no node/edge ids, no topology.
    Returns an empty string (no labels at all) if the OCR engine isn't
    available, mirroring the real adapter's own honest degrade-gracefully
    behavior rather than crashing.
    """
    ok, _ = ocr_engine_status()
    if not ok:
        return ""

    image_bgr = cv2.imread(str(path))
    if image_bgr is None:
        raise ValueError(f"could not read '{path}' as an image (unsupported format or corrupt file)")

    reader = get_reader()
    detections = reader.readtext(image_bgr)
    lines = group_detections_into_lines(detections)

    out = []
    for i, line in enumerate(lines):
        text = " ".join(b[4] for b in line)
        if not text.strip():
            continue
        out.append(f"# label: label{i + 1}")
        out.append(text.strip())
        out.append("")
    return "\n".join(out)
