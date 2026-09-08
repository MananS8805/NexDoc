# Diagram adapter

Phase 3 of the semantic preprocessing pipeline (see `/CLAUDE.md`). Converts a
`.png`/`.jpg` diagram into the common node/edge schema via detection +
reconstruction — classical computer vision (OpenCV contours + Hough line
detection), not a raw vision-language-model prompt against the whole image.
CLAUDE.md is explicit about why: a single VLM prompt for topology is a
known failure mode, not a guess.

## Input

A path to a `.png` or `.jpg` image. No PDF-embedded diagrams, no multi-page
diagrams, no vector formats (`.svg`) — out of scope for Phase 3.

## Honest scope — read this before trusting the output on anything else

This adapter reliably handles **clean, computer-generated diagrams**: solid
rectangle/diamond outlines, solid straight-line connectors, no visual
noise, high contrast. That's what the test fixture is, and that's the only
claim being made.

It is **not** claimed to handle hand-drawn diagrams, photographs of
whiteboards, low-contrast scans, curved/bent connectors, or overlapping
shapes. Those would need a fundamentally more robust detector — the kind of
trained shape/arrow detection model the "measured research" CLAUDE.md
refers to, not classical contour + Hough transform. Nothing downstream
(the prototype UI, the schema) should read this adapter's output as
evidence it works on real-world messy diagrams; it hasn't been tested
against any.

## Output

### `parse_diagram(path) -> Iterator[Region]`

Streaming entry point, matching the other adapters' shape: yields one
`Region` per detected node, then one per detected edge.

### `build_artifact(path) -> dict`

Runs the full pipeline once and returns the common-schema artifact.

## How detection actually works

1. **Nodes**: `cv2.findContours` + `cv2.approxPolyDP`. A closed contour with
   exactly 4 vertices and area above `MIN_NODE_AREA` is a node candidate.
   Rectangle vs. decision-diamond is decided by two independent, real
   geometric measurements — not a label guess:
   - **edge angle**: are all 4 polygon edges within
     `AXIS_ALIGNED_TOLERANCE_DEG` of horizontal/vertical? A rectangle's are;
     a diamond's (drawn corner-to-corner) are not.
   - **fill ratio**: `contour area ÷ bounding-box area`. A rectangle's
     outline nearly fills its own bounding box (>0.85); a diamond's
     bounding box has empty triangular corners outside the shape, so its
     fill ratio lands around 0.35–0.65.
   A 4-vertex contour that's axis-aligned with high fill ratio → `node`
   (confidence 0.90–0.97, higher when the fill ratio is closer to a perfect
   rectangle). Diagonal with a fill ratio in the expected diamond band →
   `node-decision` (confidence 0.85–0.93). Anything with 4 vertices but
   neither clean pattern is still emitted, honestly, at confidence 0.60,
   rather than forced into one bucket.

   A contour that *doesn't* approximate to 4 vertices is now checked for
   circle/ellipse before being given up on: a true ellipse's contour-area-
   to-bounding-box-area ratio is `pi/4` (~0.785) regardless of aspect ratio
   (area = `pi*a*b`, bbox area = `4*a*b`) — one constant that catches both a
   perfect circle and a stretched ellipse, unlike the rectangle/diamond
   bands above. That ratio is only a cheap pre-filter, though (a rounded-
   corner rectangle could land in the same band by coincidence): a contour
   passing it gets a real `cv2.fitEllipse` fit, and the *actual* confidence
   signal is how tightly the real contour points hug that fitted ellipse's
   boundary (mean normalized radial error) — → `node-circle` (confidence
   0.75–0.95, higher for a tighter fit), or discarded if the fit is poor.
   One more real-world wrinkle specific to round/ring shapes: a drawn
   outline is a thin ring, and `cv2.findContours` returns both the ring's
   outer edge and its inner edge as separate closed contours — both can
   independently pass the ellipse checks above and register the same
   physical shape twice at nearly the same bounding box. (This never
   surfaced for rectangles/diamonds on the original fixture — there, only
   one of the two per-shape contours ever survived the 4-vertex test, so it
   was a latent gap the circle path exposes, not one it introduces.) A
   dedup pass keeps only the larger of any two heavily-overlapping node
   candidates.

2. **Edges**: node bounding boxes (padded) are masked out of the binary
   image first, so `cv2.HoughLinesP` only ever sees connector strokes, not
   a node's own straight border — otherwise every rectangle edge would get
   picked up as a spurious "connector." Near-duplicate/overlapping segments
   Hough tends to return for one real line are clustered down to one
   representative each. Each retained segment is matched to its two
   nearest nodes by endpoint distance (must be within
   `ENDPOINT_TO_NODE_MAX_DIST`), which is how the `connects-to`
   relationships are built.

3. **Direction** (which node is "from," which is "to"): the arrowhead end
   of a line has extra converging strokes beyond the shaft, so a small
   neighborhood around it contains measurably more dark pixels than the
   plain tail end. Whichever endpoint has the denser neighborhood is
   labeled "to." Verified correct on all 3 edges in the test fixture,
   including telling apart a top-down arrow from two diagonal ones.

4. **Labels**: OCR only, via the exact same EasyOCR path as the document
   adapter (`adapters/common/ocr.py`'s `ocr_engine_status()`/`read_label()`
   — the two adapters share this one probe and label-reading helper rather
   than duplicating them). Shape and topology detection use no OCR at all
   and are unaffected when the OCR engine isn't available.

   A real, now-historical finding from when this adapter used Tesseract:
   cropping a `node-circle`'s raw bounding box for OCR (the same approach
   used for rectangles) reliably fed the curved boundary into Tesseract's
   layout analysis alongside the label text and made it return nothing at
   all — measured directly on this adapter's own fixture, not a
   hypothetical (`tests/fixtures/flowchart_with_circle.png`'s "Start"/"End"
   labels came back empty). A rectangle's flat edge sits clear of its own
   label at any reasonable padding; an ellipse's boundary curves in close
   to the label near the top/bottom of the shape. Fixed by insetting the
   OCR crop further in for `node-circle` specifically (`_ellipse_label_bbox`)
   before the shared label-reading path runs.

   **Re-verified directly after the EasyOCR switch, and the inset is no
   longer load-bearing**: EasyOCR reads both `node-circle` labels
   correctly (`Start`, confidence 1.0; `End`, confidence 0.999) from the
   *raw*, un-inset bounding box, not just the inset one — its text
   detector is evidently more robust to the curved boundary than
   Tesseract's layout analysis was. The inset was left in place anyway
   (it's harmless — same correct result either way on this fixture — and
   removing a fix that isn't provably wrong just because the engine
   changed underneath it is a change worth making deliberately later, not
   a side effect of an unrelated OCR-engine swap) rather than generalized
   to `node-decision` (the diamond), which was never affected by this
   issue under either engine.

## Confidence — a real finding, not a clean first draft

The first version of this adapter scored edge confidence from endpoint-to-
node distance alone. On the test fixture, all three edges came back at
**exactly 0.76** — every arrow happened to be drawn with a similarly small
gap to its target node, so that signal carried almost no real per-edge
variation. That's precisely the failure mode worth catching: confidence
that's technically computed per-region but numerically indistinguishable
isn't actually verifying anything independently.

The fix was a different, genuinely-varying real signal: the *ratio* between
the two endpoints' dark-pixel density (how clearly the arrowhead end
stands out from the tail end), measured directly on this fixture:

| edge | target node's boundary | density ratio | confidence |
|------|------------------------|---------------:|-----------:|
| start → decision | diamond's pointed vertex | **1.18×** | 0.653 |
| decision → approve | rectangle's flat edge | 2.52× | 0.854 |
| decision → request | rectangle's flat edge | 2.64× | 0.872 |

This is honest, not tuned to look better: an arrow terminating at a
diamond's sharp vertex is *genuinely* harder to distinguish from the
shape's own converging corner lines than one terminating at a flat
rectangle edge, and the confidence score now says so. Endpoint-to-node
distance is kept as a secondary penalty (a poor geometric match still pulls
confidence down), but it no longer dominates. Node confidence (0.85–0.97)
stays above all edge confidence (capped at 0.90) by construction, per
CLAUDE.md's expectation that "there is a shape here" is a more certain
claim than "this shape connects to that one in this direction."

## Checks

- **`edges-reference-existing-nodes`** — for every `edge` region, do both
  of its `connects-to` relationship targets exist as real `node` region ids
  in this artifact? Passes trivially (with an explicit reason) when no
  edges were detected. On the test fixture this passes by construction —
  edges are only ever built by matching real detected nodes — which is the
  expected, correct outcome, not evidence the check is vacuous; it would
  catch a future bug that let a dangling id through.
- **`ocr-engine-available`** — identical semantics to the document
  adapter's check of the same name: whether EasyOCR was usable on this
  machine, and how many node/edge labels went unlabeled as a result. Shape
  and topology detection are explicitly called out as unaffected, since
  neither uses OCR. Like the document adapter, this passes with
  `"not checked"` (rather than forcing an EasyOCR model load) for an image
  with no nodes or edges at all to label.

## Fixed bug: OpenCV version-dependent `HoughLinesP` output shape

`_detect_edges` used to assume `cv2.HoughLinesP` always returns lines shaped
`(N, 1, 4)` (the historical OpenCV behavior — each line a `[1, 4]`
sub-array, so `line[0]` is the 4-tuple `(x1, y1, x2, y2)`). OpenCV 5.0.0
(what `pip install opencv-python` gives you as of this writing, with no
upper bound in `requirements.txt`) returns `(N, 4)` instead — `line` itself
*is* the 4-length array, so `line[0]` is a bare `numpy.int32` scalar and
`tuple(line[0])` raised `TypeError: 'numpy.int32' object is not iterable`.
This was a real, reproducing crash on the repo's own bundled fixture
(`tests/fixtures/flowchart_clean.png`), not a hypothetical — a plain
`pip install -r requirements.txt` today gets it out of the box, failing 3
of 4 `tests/test_schema_conformance.py` cases.

Fixed by flattening each line with `np.asarray(line).reshape(-1)` instead
of indexing `line[0]` — this yields the same 4 ints regardless of whether
OpenCV wraps them in an extra singleton dimension or not, so it isn't tied
to either version's shape. `_edge_angles_deg` (used by node/contour
detection) made the same `(N, 1, 2)` assumption about `approxPolyDP`
output; that shape is still what OpenCV 5.0.0 returns as of this fix, but
it was hardened with the same `reshape(-1, 2)` pattern defensively, so a
future OpenCV release flattening that output too won't silently
reintroduce this class of bug. `requirements.txt`'s `opencv-python>=4.9`
was deliberately left with no upper bound — see the commit that introduced
this fix for the reasoning (the parsing is shape-agnostic now, not tied to
a version range).

If you hit this exact `TypeError` again on some future OpenCV release, it
means a line's *encoding* changed (not just an extra wrapping dimension) —
that's a genuinely new incompatibility, not a regression of this fix.

## Fixed bugs: found by testing against a UML sequence diagram

The adapter had only ever been tested against flowchart-style diagrams
(boxes, diamonds, circles). Pointed at a real, unseen diagram type — a UML
sequence diagram, with actor header boxes, vertical dashed lifelines, thin
tall activation bars, and horizontal call/return arrows — two real bugs
surfaced immediately, diagnosed with actual contour numbers rather than
guessed at:

1. **Ordinary rectangles were coming back as `node-circle`.** A shape's
   drawn stroke produces two contours (an outer and inner ring — the same
   duplicate-ring fact already known for circles, see above). On this
   fixture's actor boxes, anti-aliasing nudged the *outer* ring's
   `approxPolyDP` result to 5 vertices instead of 4, so it fell through to
   the ellipse check — and its fill ratio (0.841) happened to land inside
   the ellipse pre-filter band (0.70–0.86) and its fit error cleared
   `ELLIPSE_FIT_ERROR_MAX`, so it registered as a real, if marginal,
   ellipse match. The *inner* ring resolved to a clean 4-vertex rectangle,
   correctly. `_dedup_nodes` then discarded the correct one: it broke ties
   between overlapping candidates by area alone, and a stroke's outer ring
   is always the larger of the two. **Fix**: dedup now sorts non-circle
   classifications before `node-circle` ones, falling back to area only
   within the same shape category (the genuine same-shape ring-duplicate
   case this logic was originally written for, still handled the same
   way). All three actor boxes on `sequence_diagram_clean.png` now
   correctly come back as `node`, confidence 0.97.

2. **Thin, elongated shapes (activation bars) were silently missed
   entirely.** `0.02 * perimeter` is dominated by an activation bar's long
   dimension (~15px wide × ~300px tall), so the resulting epsilon exceeded
   the bar's own width — Douglas-Peucker collapsed both long parallel sides
   into what looked like one line, leaving only 2 vertices instead of 4,
   and it didn't qualify for the ellipse fallback either (its fill ratio,
   ~0.92, sits above the ellipse band). **Fix**: epsilon is now capped at
   `min(0.02 * perimeter, 0.4 * min(w, h))` — this only changes behavior
   for shapes whose short dimension is small relative to their perimeter
   (exactly the elongated case); verified directly that
   `flowchart_clean.png` and `flowchart_with_circle.png` produce byte-for-
   byte the same shape classifications as before this change
   (`tests/test_diagram_sequence_shape.py::test_existing_fixtures_are_unaffected_...`).

With both fixed, `sequence_diagram_clean.png` goes from **3 nodes, 0
edges, all misclassified** to **5 nodes (3 correctly-labeled actor
rectangles + 2 unlabeled activation-bar rectangles), 3 real message edges**
with correct topology and OCR'd labels ("request", "4. response") on the
edges that carried one. Full before/after numbers, and the remaining honest
gap (the `Database` activation bar, deliberately drawn short in the test
fixture, still doesn't clear `MIN_NODE_AREA` — see "Known limitations"
below), are in `tests/test_diagram_sequence_shape.py`'s module docstring.

## Known limitations (Phase 3 scope, not bugs)

- Rectangle, diamond, and now circle/ellipse node shapes are recognized
  (see "How detection actually works" above for the ellipse-fit approach
  and its confidence signal, added and proven against
  `tests/fixtures/flowchart_with_circle.png`). Any *other* shape
  (parallelogram, rounded rectangle, hexagon, ...) still falls through to
  the "4 vertices but neither clean pattern" bucket at best, or is missed
  entirely if it doesn't approximate to 4 vertices and doesn't pass the
  ellipse fit either. Only tested against clean, computer-generated
  ellipses/circles at roughly the size and stroke width of the two test
  fixtures — an untested edge case worth calling out explicitly: a very
  small or very thin (near-degenerate, almost line-like) ellipse would
  likely have too few usable contour points, or too poor a fit relative to
  its size, to clear `ELLIPSE_MIN_CONTOUR_POINTS`/`ELLIPSE_FIT_ERROR_MAX` —
  this hasn't been measured, so it's flagged here rather than assumed to
  work.
- Curved or bent (non-straight) connectors are not detected — `HoughLinesP`
  finds straight segments only.
- Direction inference (arrowhead-density heuristic) is measurably weaker
  when a connector terminates at a diamond's vertex vs. a rectangle's flat
  edge — see the confidence table above. This is reported via confidence,
  not hidden. Untested against a connector terminating at a `node-circle`
  specifically (the new fixture's arrows all still land on a flat or
  near-flat approach angle) — likely closer to the diamond case than the
  rectangle case, since a circle's boundary is curved everywhere, but this
  is a guess, not a measurement, and is called out as such rather than
  quietly assumed.
- No handling for overlapping or touching shapes, curved layouts, multiple
  diagrams per image, or diagrams with a background pattern/texture. The
  inner/outer-contour duplicate-detection issue solved for `node-circle`
  (see above) is a related but distinct problem from *overlapping distinct
  shapes* — the dedup pass keys on bounding-box overlap, so two genuinely
  separate but touching/overlapping shapes would likely also get
  incorrectly merged down to one node by the same logic. Not tested either
  way; flagging it since the dedup pass is new.
- Label OCR shares every limitation documented in
  `adapters/document/README.md`'s "OCR engine — EasyOCR" section (CPU
  inference speed, the one-time model download, the fixed `["en", "hi"]`
  default language list, no real layout understanding). The curved-node-
  boundary issue that used to drop a `node-circle` label's text entirely
  under Tesseract (see "Labels" above) was re-tested directly after the
  EasyOCR switch and no longer reproduces — EasyOCR reads both round-node
  labels correctly straight from the raw bounding box — so this is no
  longer a live limitation, though the defensive inset (`_ellipse_label_bbox`)
  is still applied for `node-circle`.
- **UML sequence diagrams are only partially handled, honestly measured on
  `sequence_diagram_clean.png`**: actor boxes and normally-sized activation
  bars now detect correctly (see "Fixed bugs" above), but a *very* short
  activation bar (the fixture's `Database` bar, 16px × 100px = 1600px²)
  still falls below `MIN_NODE_AREA` (3000px²) and is missed — this wasn't
  raised because activation-bar detection in general is unreliable, it's
  the same area-threshold limitation any small shape has, just easier to
  hit with a short bar than a normal flowchart box. One real, measured
  consequence: the message edge that should have connected the missing bar
  to the Server activation bar instead snapped onto the *Server actor
  header box* — the nearest node that does exist — producing a
  topologically wrong edge rather than no edge at all. This adapter does
  not currently detect "this edge's best match is implausibly far away,
  treat it as unmatched instead of forcing a connection" — every edge
  candidate that clears `ENDPOINT_TO_NODE_MAX_DIST` gets attached to
  *some* node, even a wrong one, if the right one was never detected.
  Lowering `MIN_NODE_AREA` was deliberately not done to patch this one
  fixture — that risks new noise false-positives elsewhere and is exactly
  the "tune it until the one test case passes" anti-pattern this project
  avoids; a real fix would need either a smarter area threshold (relative
  to the image/shapes already found, not a fixed pixel count) or a
  maximum-plausible-edge-length sanity check independent of node
  detection.
- **Dashed connectors work, but only within Hough's gap tolerance.**
  `sequence_diagram_clean.png`'s dashed return arrows (10px dash, 8px gap)
  were correctly detected as single edges — `HOUGH_MAX_LINE_GAP` (8)
  happens to bridge that exact gap. A dash pattern with a larger gap would
  likely fragment into multiple short segments, some possibly below
  `HOUGH_MIN_LINE_LENGTH` and dropped. This is a real, calibration-
  dependent limitation, not a claim that dashed lines are unsupported in
  general — measured to work at this specific dash/gap size, not proven to
  work at every size.
- **Long vertical lifelines were not observed to produce false-positive
  edges** on this fixture — likely because both ends of a lifeline's
  Hough-detected segments tend to match the same actor node (rejected by
  the `idx_a == idx_b` guard) or fall outside `ENDPOINT_TO_NODE_MAX_DIST`
  of any node at all, not because of any lifeline-specific handling. There
  is no code that recognizes "this is a lifeline, not a message" — the
  absence of false positives here is an emergent result of the existing
  distance/matching logic on this one fixture's proportions, not a
  guarantee for a lifeline drawn at a different length or dash spacing.

## Test fixtures

`tests/fixtures/generate_diagram_fixture.py` builds
`tests/fixtures/flowchart_clean.png`: a vendor-onboarding flow matching the
UI mock's original sample — one start node (rectangle), one decision node
(diamond), two action nodes (rectangles), three arrows ("yes"/"no" branches
plus the initial edge), drawn cleanly with Pillow at high contrast. (Note:
this script hardcodes a Windows font path, `C:/Windows/Fonts/arial.ttf`, and
does not run as-is on this repo's Linux dev environment — `flowchart_clean.png`
itself is still checked in and works fine, this only affects regenerating it.)

`tests/fixtures/generate_diagram_fixture_shapes.py` builds
`tests/fixtures/flowchart_with_circle.png` specifically to exercise
circle/ellipse node detection: an ellipse start node ("Start", non-1:1
aspect ratio), a diamond decision node ("Valid?"), a rectangle action node
("Approve"), and a circle end node ("End", 1:1 aspect ratio) — deliberately
including both a stretched ellipse and a true circle so the same code path
is proven on both, plus the pre-existing rectangle/diamond shapes so the
new detection doesn't accidentally displace them. Looks up a real installed
TTF (DejaVu Sans) with a fallback to Pillow's bitmap font, so it runs on
this Linux environment unlike the sibling script above.

`tests/fixtures/generate_diagram_fixture_sequence.py` builds
`tests/fixtures/sequence_diagram_clean.png`: a UML sequence diagram (3
actors — Client, Server, Database — dashed lifelines, activation bars, 2
solid call arrows, 2 dashed return arrows), specifically to test a
structurally different diagram grammar than any prior fixture. This is
what surfaced both bugs described in "Fixed bugs" above; see
`tests/test_diagram_sequence_shape.py` for the regression tests built from
it.
