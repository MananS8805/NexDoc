# ADR 0002: detection + OCR over a raw VLM prompt for diagrams

## Status

Accepted, implemented in [`adapters/diagram/adapter.py`](../../adapters/diagram/adapter.py).

## Context

The obvious-seeming approach to "extract structure from a diagram image" is
to hand the whole image to a vision-language model and ask it to describe
the nodes and edges. CLAUDE.md flags this directly as a known failure
mode: single-VLM-prompt topology extraction is measured (in prior
research, referenced in CLAUDE.md, not re-derived here) to fail badly
specifically on *connection topology* — a model can often describe what
shapes are present while getting which-connects-to-which wrong, because
topology requires precise geometric reasoning a single free-form
image-to-text pass doesn't reliably do.

## Decision

Use classical detection + reconstruction instead: OpenCV contour detection
and polygon approximation for nodes, Hough line detection for connectors,
and geometric matching (endpoint distance, dark-pixel-density asymmetry for
arrowhead direction) to reconstruct the node/edge graph — with OCR handling
only the text labels, as a separate concern from topology.

## Consequences

- Topology comes from measurable geometry, not a model's free-form
  description: a node is a real 4-vertex contour above an area threshold;
  an edge is a real Hough-detected line segment matched to two real node
  bounding boxes within a distance threshold. Every claim the adapter makes
  about structure traces back to a specific measurement, which is exactly
  what let a real bug get caught during development — see
  [`adapters/diagram/README.md`](../../adapters/diagram/README.md#confidence--a-real-finding-not-a-clean-first-draft)
  for the story of the first confidence formula scoring all three test
  edges identically, and why.
- The honest cost: this only reliably works on **clean, computer-generated
  diagrams** — solid shapes, straight connectors, high contrast. A
  hand-drawn or photographed diagram would defeat contour/Hough detection
  in ways a VLM might actually handle better (at the cost of the topology
  failure mode above). This adapter does not claim to handle those cases,
  and hasn't been tested against any — stated directly in that README's
  "Honest scope" section, not left implicit.
- A production system solving the general case would likely need a trained
  shape/arrow detector (not classical CV, not a single VLM prompt) — this
  adapter is a prototype demonstrating the *architecture* (detect, then
  reconstruct, then OCR labels separately), not a claim that classical CV
  is the final answer for messy real-world diagrams.
