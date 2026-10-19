"""Copies the EasyOCR model weights the app needs into ./docker-models/ so the
Docker build can COPY them in instead of downloading ~300MB *inside* the build.

Why: a download inside `docker build` has no resume — one dropped connection
fails the whole step (this happened: "retrieval incomplete: got only 34780267
out of 199886824 bytes"). Here the download, if one is even needed, runs on the
host with a normal retry loop, and the build itself needs no network for models.

    python scripts/stage_ocr_models.py          # then: docker build -t nexdoc .
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DEST = ROOT / "docker-models"
LANGS = ["en", "hi"]  # adapters.common.ocr.DEFAULT_LANGUAGES
FILES = ["craft_mlt_25k.pth", "devanagari.pth"]  # detector + the Devanagari/Latin recognizer en+hi resolves to


def main() -> int:
    cache = Path.home() / ".EasyOCR" / "model"
    for attempt in range(1, 6):
        if all((cache / f).exists() for f in FILES):
            break
        print(f"models missing from {cache}; downloading via EasyOCR (attempt {attempt}/5)…")
        try:
            import easyocr

            easyocr.Reader(LANGS, gpu=False, verbose=True)
        except Exception as exc:  # network errors are the expected failure; just retry
            print(f"  download failed: {type(exc).__name__}: {exc}")
            time.sleep(5)
    missing = [f for f in FILES if not (cache / f).exists()]
    if missing:
        print(f"could not obtain: {missing}")
        return 1
    DEST.mkdir(exist_ok=True)
    for f in FILES:
        shutil.copy2(cache / f, DEST / f)
        print(f"staged {f} ({(DEST / f).stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
