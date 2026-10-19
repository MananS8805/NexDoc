# OCR engine options

nexDoc uses **EasyOCR** (CPU) for scanned document pages and diagram labels
(`adapters/common/ocr.py`). It is accurate on the repo's fixtures — including
the Devanagari line — but it is the slowest part of the pipeline (about 15s
per scanned page and ~1s per label crop on the dev machine; everything else is
well under a second). This note records what was evaluated and what is worth
trying next. Only RapidOCR was actually run here; the rest is from general
knowledge and **has not been benchmarked on this repo** — treat it as a
shortlist, not a result.

## Tested here: RapidOCR (ONNX Runtime, default PP-OCRv4 models)

Same machine, same fixtures, vs. EasyOCR:

| | EasyOCR (en+hi) | RapidOCR (default) |
|---|---|---|
| Whole flowchart image | ~5.0s | ~3.4s |
| One label crop | ~0.4s | ~1.7s (slower) |
| Hindi line on `note_scanned.pdf` | read correctly | **dropped silently** |
| Needs PyTorch | yes | no |

Not a drop-in: the default models don't cover Devanagari and the per-crop cost
is higher. Its real appeal is the **absence of PyTorch** (this repo's most common
setup failure is a broken `torch` DLL in the wrong Python environment). It can
load other PP-OCR language recognizers; whether a Devanagari one matches EasyOCR
on `note_scanned.pdf` is untested.

## Shortlist to evaluate next

| Option | Why consider it | Watch out for |
|---|---|---|
| **PaddleOCR** (PP-OCRv4/v5) | Strong multilingual accuracy, has Devanagari recognizers, fast on CPU | Needs `paddlepaddle` (heavy; Windows installs can be fiddly) |
| **docTR** (Mindee, models on Hugging Face) | Document-focused detector + recognizer pipeline, PyTorch, easy to swap models | Latin-script-centric; Hindi coverage would need checking |
| **Surya** | Layout analysis *and* line OCR across ~90 languages incl. Hindi — would also help the "real table detection" gap in the document adapter | Wants a GPU for good speed; check its license terms before adopting |
| **TrOCR** (`microsoft/trocr-base-printed`, Hugging Face) | Very good recognizer for printed text lines | Recognition only (needs a separate detector), English only, slow on CPU |
| **VLM readers** (Qwen2.5-VL, Florence-2, GOT-OCR 2.0, …) | Best on messy layouts, tables, handwriting | GPU-class cost; they can *hallucinate* values, which collides with this project's guardrail against silently rewriting extracted numbers/names. If used at all: label reading only, never topology, with the value cross-checked against a conventional OCR pass |

## Recommended path

1. Add a small backend interface in `adapters/common/ocr.py` (today every call
   goes through `get_reader()` / `read_label()` / `group_detections_into_lines()`),
   so an engine is swappable without touching the adapters.
2. Evaluate candidates with the same harness used for the DPI change: alternate
   runs in one process, compare **median time** *and* require the **text to match**
   on `note_scanned.pdf` (English + Devanagari) and the diagram fixtures. A faster
   engine that changes the text is a regression, not a win.
3. Keep EasyOCR as the default until a candidate wins on both axes; ship the
   winner as an optional extra rather than a hard dependency.
