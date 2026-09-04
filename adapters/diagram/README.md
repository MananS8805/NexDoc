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

4. **Labels**: OCR only, via the exact same Tesseract path as the document
   adapter (`adapters/common/ocr.py`'s `tesseract_status()` — the two
   adapters share this one probe rather than duplicating it). Shape and
   topology detection use no OCR at all and are unaffected when Tesseract
   isn't available.

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
  adapter's check of the same name: whether Tesseract was reachable on this
  machine, and how many node/edge labels went unlabeled as a result. Shape
  and topology detection are explicitly called out as unaffected, since
  neither uses OCR.

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

## Known limitations (Phase 3 scope, not bugs)

- Only rectangle and diamond node shapes are recognized; any other shape
  (circle, parallelogram) would currently fall through to the "4 vertices
  but neither clean pattern" bucket at best, or be missed entirely if it
  doesn't approximate to 4 vertices.
- Curved or bent (non-straight) connectors are not detected — `HoughLinesP`
  finds straight segments only.
- Direction inference (arrowhead-density heuristic) is measurably weaker
  when a connector terminates at a diamond's vertex vs. a rectangle's flat
  edge — see the confidence table above. This is reported via confidence,
  not hidden.
- No handling for overlapping or touching shapes, curved layouts, multiple
  diagrams per image, or diagrams with a background pattern/texture.
- Label OCR shares every limitation documented in
  `adapters/document/README.md`'s "OCR engine — real limitations" section,
  including this adapter having been built and tested on a machine without
  a working Tesseract install (same environment-specific fact, not an
  adapter design limitation).

## Test fixture

`tests/fixtures/generate_diagram_fixture.py` builds
`tests/fixtures/flowchart_clean.png`: a vendor-onboarding flow matching the
UI mock's original sample — one start node (rectangle), one decision node
(diamond), two action nodes (rectangles), three arrows ("yes"/"no" branches
plus the initial edge), drawn cleanly with Pillow at high contrast.
