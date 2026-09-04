# ADR 0003: static HTML output over a live server, for this stage

## Status

Accepted, implemented in [`scripts/build_prototype.py`](../../scripts/build_prototype.py) and [`run_adapter.py`](../../run_adapter.py).

## Context

CLAUDE.md's Phase 4 ("Wire the UI") describes eventually replacing the
mock's hardcoded sample data with "actual adapter output over a real API."
That implies a server at some point. This stage is not that point: three
adapters just went from "don't exist" to "real, tested against real
fixtures" in one build. Standing up an API, request/response handling, and
a hosting story on top of that would be scope creep against CLAUDE.md's own
explicit non-goals for this stage (no auth, no live server, no database).

## Decision

`run_adapter.py` writes a single self-contained `prototype.html` per run —
the real artifact's JSON baked directly into an embedded `<script>` block
at generation time, reusing the existing design mock's CSS/JS almost
entirely unchanged. No server process, no fetch, no network call; the file
works offline, opened directly in a browser.

## Consequences

- Every "live processing panel," tab, and check-strip interaction in the
  UI is real interaction with real data — verified directly in a browser
  for all three artifact kinds (spreadsheet, document, diagram) — but the
  data is frozen at generation time. Re-running the adapter against a
  changed file means re-running `run_adapter.py`, not refreshing a page
  that re-fetches.
- This is a legitimate, deliberate scope boundary for a prototype stage,
  not a technical limitation being worked around — the alternative (a live
  API) is explicitly a *later* phase in CLAUDE.md's own build order, gated
  on this stage existing first.
- The generation script (`scripts/build_prototype.py`) is structured so
  that swapping "bake JSON into HTML at generation time" for "fetch JSON
  from a real endpoint at page-load time" later is a change to how the
  `DOCS` array gets populated, not a rewrite of the interaction logic
  itself — the JS that renders tabs, the live panel, and the checks strip
  doesn't know or care whether its data arrived via a build step or a
  fetch.
