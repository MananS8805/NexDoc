"""Shared OCR-availability probe, used by any adapter that may need
Tesseract (currently document and diagram). This is genuinely shared
infrastructure — not spreadsheet-adapter logic — so it lives in `common`
rather than being duplicated per adapter or creating a dependency between
otherwise-independent adapters.
"""

from __future__ import annotations

import pytesseract

_STATUS: tuple[bool, str] | None = None  # lazily probed once per process


def tesseract_status() -> tuple[bool, str]:
    """Returns (available, detail). Probed once per process — cheap enough
    to not bother, but there's no reason to shell out to Tesseract more
    than once for a fact that doesn't change mid-run.
    """
    global _STATUS
    if _STATUS is None:
        try:
            version = pytesseract.get_tesseract_version()
            _STATUS = (True, str(version))
        except Exception as exc:  # pytesseract raises different types across platforms
            _STATUS = (False, f"{type(exc).__name__}: {exc}")
    return _STATUS
