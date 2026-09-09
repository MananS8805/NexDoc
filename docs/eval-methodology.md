# Eval methodology

## What exists today

**All three adapters now have a real, deterministic fact-lookup eval** —
closing the gap this document used to describe as the biggest one against
CLAUDE.md's "measurable downstream improvement" criterion:

- [`eval/run_eval.py`](../eval/run_eval.py) — spreadsheet, against
  `tests/fixtures/budget_test.xlsx`. Result: baseline 0/6, structured 6/6.
- [`eval/run_document_eval.py`](../eval/run_document_eval.py) — document,
  against `tests/fixtures/invoice_born_digital.pdf`. Result: baseline 3/6
  (50%), structured 6/6 (100%).
- [`eval/run_diagram_eval.py`](../eval/run_diagram_eval.py) — diagram,
  against `tests/fixtures/flowchart_clean.png`. Result: baseline 0/6,
  structured 6/6.

No API key, no model call for any of the three — each runs in a few
seconds, offline, and stays the default eval for its kind. See "The
document eval" and "The diagram eval" below for what each one measures and
why the numbers differ so much from each other (they are not tuned to
match — see each section for the real, honest reason).

Spreadsheet additionally has a real Groq-backed task-accuracy eval
(`eval/run_real_eval.py`, opt-in, requires `GROQ_API_KEY`) — a genuinely
different, stronger claim than the deterministic evals above. See "The
real task-accuracy eval" below. Document and diagram do not have a
task-accuracy eval yet — a natural next extension once a key is available
and authorized for that purpose, reusing the same
`qa_document_dataset.json`/`qa_diagram_dataset.json` datasets, but not
built now (out of scope of this pass).

## The one distinction that matters: what is actually being measured

There are two genuinely different things a "spreadsheet eval" could mean,
and it matters which one you're looking at:

1. **Fact-recoverability** — is the specific fact a question asks about
   even *present*, in some form, in this representation? This is what
   `run_eval.py` measures. It looks up the exact cell or region a question
   names (e.g. "Q1 Plan!E6") in each representation and checks whether the
   expected value is there. No model is involved. It answers "did
   flattening destroy this information," not "can something read this
   representation and reason correctly about it."

2. **Task accuracy** — given this representation, can an LLM (or a human)
   actually answer the question correctly? This requires a real model call,
   scoring free-text answers, and dealing with everything that comes with
   that (prompt sensitivity, partial credit, the model's own reasoning
   failures independent of the data). **This is now built**, in
   [`eval/run_real_eval.py`](../eval/run_real_eval.py) and
   [`qa/groq_qa.py`](../qa/groq_qa.py) — see "The real task-accuracy eval"
   below. An earlier version of `run_eval.py` was written to do this via
   the Anthropic API, but no API key was available in the environment this
   was built in, and CLAUDE.md's guardrail is explicit: never present an
   estimated number as measured. Rather than leave the eval blocked on a
   credential, it was rewritten to measure (1) instead — a real, weaker,
   but honestly-labeled claim. Distinction (1) still stands on its own and
   `run_eval.py` still exists and still runs by default: (2) doesn't
   replace it, it answers the different, stronger question (1) was never
   trying to answer.

**Read every number produced by this repo's eval against distinction (1),
not (2).** "Structured scores 100%, baseline scores 0%" means "every fact
these 6 questions ask about is recoverable from the structured JSON and
none of them are recoverable from the flattened CSV" — a true and
meaningful statement about information loss, and a much narrower one than
"an LLM answers these questions correctly 100% of the time given the
structured JSON."

## Why the spreadsheet baseline is exactly 0%, not "low"

This is a property of the specific test fixture, not a general truth about
flattening spreadsheets. `tests/fixtures/generate_test_workbook.py` builds
its workbook with `openpyxl`, which never computes formulas — so the file
has no cached formula results anywhere. `flatten_to_csv`'s
`data_only=True` read finds nothing to fall back on and every formula cell
comes through blank. All six eval questions happen to target
formula-dependent cells, so the baseline can't get any of them right. A
real Excel-authored file usually *does* have cached values, and a fixture
with more literal (non-formula) data would show the baseline scoring above
0% on the questions that don't depend on a formula. Verified directly (see
[`adapters/spreadsheet/README.md`](../adapters/spreadsheet/README.md#eval))
by opening the same file with `data_only=True` vs `False` and comparing —
not assumed.

## The real task-accuracy eval (`eval/run_real_eval.py`)

This closes the gap the previous section of this document used to
describe as unbuilt. It reuses `eval/qa_dataset.json` exactly as the old
"How to extend this later" section proposed — the `check` field
(`cell_value`, `cross_sheet_ref`, `unresolved_reason`) already named what
each question tests structurally, so no new dataset was needed.

**What it measures.** For each question in `qa_dataset.json`, a real Groq-
hosted LLM (via [`qa/groq_qa.py`](../qa/groq_qa.py)) is asked the question
twice: once given the flattened CSV baseline as context, once given the
spreadsheet's structured common-schema JSON (`to_structured()`'s region
list — see `qa/groq_qa.py`'s module docstring for why that representation
specifically, not `to_text()` or the full `to_json()`). Each free-text
answer is scored against `expect_substring` with the same case-insensitive
substring match `run_eval.py` already uses. This is genuinely distinction
(2) from the section above: a model actually reasoning over the
representation, not a direct code lookup.

**Requirements.** A real `GROQ_API_KEY` (see
[`.env.example`](../.env.example) at the repo root — copy it to `.env` and
fill in a key from https://console.groq.com/keys). Without one,
`run_real_eval.py` prints a clear "SKIPPED" message and exits 0 — it never
crashes and never fabricates a number for a mode that didn't actually run,
per CLAUDE.md's guardrail.

**How to run it:**

```bash
pip install -r requirements.txt   # adds groq + python-dotenv
cp .env.example .env              # then edit .env and set GROQ_API_KEY
python eval/run_real_eval.py
```

It is a separate, opt-in script — `eval/run_eval.py` is unchanged and
still runs instantly, offline, for free, as the default eval.

**Honesty about verification.** The code path was built and is covered by
mocked unit tests
([`tests/test_groq_qa.py`](../tests/test_groq_qa.py),
[`tests/test_run_real_eval.py`](../tests/test_run_real_eval.py)) that
inject a fake Groq client to prove the prompt is built correctly, the
response is parsed correctly, and scoring/tallying is correct — independent
of what a real model says.

**A real key was supplied and this has now actually been run once**, on
`openai/gpt-oss-120b` via Groq. Real, measured result:

```
baseline (flattened CSV) task accuracy:        1/6 = 17%
structured (common schema JSON) task accuracy: 6/6 = 100%
```

Structured hit the same ceiling the deterministic eval (distinction (1))
already found — every fact is recoverable, and the model reliably recovers
it. The baseline scoring *1/6 instead of 0/6* is the interesting part, and
worth understanding rather than treating as noise: q2 asks for
`Actuals!B6` (`=SUM(B3:B5)`), a formula cell that — like every formula cell
in this fixture — comes through blank in the flattened CSV. The model
answered "4980" correctly anyway. It didn't recover a hidden value; the
three literal inputs the formula sums (`B3=1150`, `B4=2950`, `B5=880`) are
still individually visible as plain numbers elsewhere in the same CSV, and
the model added them itself. That's a real capability the deterministic
fact-lookup eval cannot see, by construction — it only checks the exact
cell a question names, never reasons across other cells the way a model
can. It's also exactly the "different, stronger claim" distinction (2)
exists to measure: a model given the baseline can sometimes *partially*
route around missing structure by re-deriving it, something a direct code
lookup never attempts. Notably, this reconstruction wasn't reliable even
within this one run: q1, q3, and q6 ask for the exact same kind of thing
(another row's SUM total, individually visible inputs still present in the
CSV) and the model did *not* reconstruct any of them correctly this time —
so "sometimes" is doing real work in that sentence, not "always when
possible." q4 (a cross-sheet relationship) and q5 (why a formula fails to
resolve) ask for something no amount of re-summation could recover
regardless. All five of those failed, giving the observed 1/6.

## The document eval (`eval/run_document_eval.py`)

Verified directly before designing this: `page.get_text("blocks")`
(PyMuPDF, no adapter logic at all) on `tests/fixtures/invoice_born_digital.pdf`
is **line-for-line identical** to what the real adapter's `to_text()`
produces. This is the key, honest difference from the spreadsheet eval —
a born-digital PDF does **not** lose facts by being flattened to plain
text the way a spreadsheet's formula cells do. So
[`adapters/document/baseline.py`](../adapters/document/baseline.py)'s
naive baseline (block-level text extraction, no classification, no
relationship inference, no OCR fallback for a scanned page) genuinely
keeps most of what a question could ask about.

**What it measures**, via `eval/qa_document_dataset.json`'s three check
types: `field_value` (a direct text lookup — expected to pass on **both**
sides, and it does), `relationship_target` (does this representation know
which line items a total sums? — the baseline is restricted to only the
named region's own text, never the whole blob, so it structurally cannot
answer), and `region_type` (does this representation know a line's
semantic role, e.g. "total-field" vs. just another line of prose? — raw
text never contains a literal type tag).

**Real, measured result**: baseline 3/6 = 50%, structured 6/6 = 100%. Not
a stark 0% — that would have been a fabricated-looking number forced by
picking only-favorable questions, and this project doesn't do that. The
honest claim here is narrower and more precise than the spreadsheet
eval's: a document's *content* mostly survives flattening; what never
survives is *semantic role and structural relationships* — exactly
CLAUDE.md's actual claim about generic pipelines, stated exactly, not
inflated.

## The diagram eval (`eval/run_diagram_eval.py`)

A diagram has no natural "flattened text" the way a spreadsheet or
document does. The honest naive-pipeline equivalent —
[`adapters/diagram/baseline.py`](../adapters/diagram/baseline.py)'s
`flatten_labels()` — runs OCR over the *whole image at once* with no
shape or connector detection at all: just every visible string, in an
unordered bag, with zero node/edge ids and zero topology. This is a
direct, real illustration of CLAUDE.md's claim that image-only processing
fails specifically at connection topology.

**What it measures**, via `eval/qa_diagram_dataset.json`'s three check
types: `label_value` (is this specific shape's label recoverable at all?),
`topology_target` (what does this connector lead to? — the baseline
**always** fails here by construction, not by a substring search missing:
a flat bag of labels has zero connectivity data for any label, ever — a
cleaner, more absolute illustration than the spreadsheet eval's
cross-sheet-ref check, whose baseline failure is a coincidence of one
unevaluated formula rather than a structural impossibility), and
`check_result` (baseline has no artifact-level validation concept at all).

**Real, measured result**: baseline 0/6, structured 6/6 — starker than
the document eval, and for a real, specific, measured reason worth
stating plainly rather than assuming: whole-image OCR (unlike the real
adapter's per-shape *cropped* OCR) groups text purely by vertical
position, so it measurably **merges labels from adjacent shapes onto the
same line** and **splits a single multi-word label across two lines**.
Both effects are visible directly in a real run of
`flatten_labels()` — e.g. `"Start: new vendor request"` comes back as two
separate lines, `"Start: new vendor"` and `"request"`, so even a simple
`label_value` question that would trivially pass with per-shape OCR fails
here. This was not designed in — the eval dataset originally assumed
label lookups would pass on both sides (as they do for documents), and
was corrected to reflect what a real run actually showed, per the "never
fabricate, never force a target number" rule.

## Deliberately scoped out of both new evals, and why

- `tests/fixtures/note_scanned.pdf` (document) — would need a second,
  OCR-specific naive-baseline path (this one only handles born-digital
  pages), and its Hindi content deserves its own script-correctness
  ground truth rather than being folded into this dataset.
- `tests/fixtures/flowchart_with_circle.png` (diagram) — a second clean
  fixture proving the same claim again; not needed for this eval's
  purpose.
- `tests/fixtures/sequence_diagram_clean.png` (diagram) — **not** a
  scope-narrowing choice, an ordering one: this fixture had an
  already-diagnosed adapter bug (see
  [`adapters/diagram/README.md`](../adapters/diagram/README.md)'s "Fixed
  bugs") at the time these evals were designed. Building eval ground truth
  against a fixture with a known-wrong extraction would have conflated
  "eval gap" with "adapter bug" in the same dataset — fix the bug first,
  eval it second, not both in one pass.

## Extending eval to a real task-accuracy measure for document/diagram

Both new datasets already name what each question tests structurally via
their `check` field, the same design choice `qa_dataset.json` made
originally — so a Groq-backed task-accuracy eval for document/diagram
(mirroring `eval/run_real_eval.py`) is a small, mechanical extension
whenever a key is available and its use is authorized for that purpose:
ask the model the question given each representation as context, score
its free-text answer the same way. Not built now — out of scope of this
pass, named here so it isn't lost.
