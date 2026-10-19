"""Docker build step: make sure the OCR weights are present, then prove they load.

Two build paths reach this:
  * local build  - `scripts/stage_ocr_models.py` already put the weights in
    ./docker-models/, the Dockerfile COPYed them in, nothing to download.
  * git-based host (Render, Railway, ...) - the repo has no weights (they're
    git-ignored), so this downloads them, retrying, since one dropped
    connection used to fail the whole build.
Finishes by loading the reader with downloads disabled, so a missing or
corrupt file fails the *build*, not the first user request.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

LANGS = ["en", "hi"]
FILES = ["craft_mlt_25k.pth", "devanagari.pth"]
CACHE = Path.home() / ".EasyOCR" / "model"


def main() -> int:
    for attempt in range(1, 7):
        if all((CACHE / f).exists() and (CACHE / f).stat().st_size > 1_000_000 for f in FILES):
            print("OCR weights present.")
            break
        print(f"OCR weights missing - downloading (attempt {attempt}/6)")
        try:
            import easyocr

            easyocr.Reader(LANGS, gpu=False, verbose=True)
        except Exception as exc:
            print(f"  download failed: {type(exc).__name__}: {exc}")
            for f in FILES:  # drop a truncated file so the next attempt re-fetches it
                p = CACHE / f
                if p.exists() and p.stat().st_size < 1_000_000:
                    p.unlink()
            time.sleep(5)
    else:
        print("could not obtain OCR weights")
        return 1
    import easyocr

    easyocr.Reader(LANGS, gpu=False, download_enabled=False, verbose=False)
    print("OCR models load offline: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
