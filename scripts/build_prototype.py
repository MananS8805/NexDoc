"""Builds design/preprocessing-pipeline-prototype.html from real adapter output.

Runs the actual spreadsheet adapter (adapters/spreadsheet/adapter.py) against
the real test fixture (tests/fixtures/budget_test.xlsx), reshapes that real
output through the four common output functions
(adapters/common/outputs.py), and bakes the result into a copy of the
existing UI design (design/preprocessing-pipeline-mock.html) as a static,
self-contained HTML file — no server, no network calls, no fetch at load
time. The original mock is left untouched as the design spec.

Run from the repo root:

    python scripts/build_prototype.py

Everything embedded in the output is real: region count, confidence values,
the genuinely-failed cross-sheet check, and the full common-schema JSON
(source + regions + checks). Nothing here is illustrative sample data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "budget_test.xlsx"
MOCK_HTML = ROOT / "design" / "preprocessing-pipeline-mock.html"
OUTPUT_HTML = ROOT / "design" / "preprocessing-pipeline-prototype.html"

FIXTURES_ALL_KINDS = {
    "spreadsheet": ROOT / "tests" / "fixtures" / "budget_test.xlsx",
    "document": ROOT / "tests" / "fixtures" / "invoice_born_digital.pdf",
    "diagram": ROOT / "tests" / "fixtures" / "flowchart_clean.png",
}


def _type_label_spreadsheet(region: dict) -> str:
    sheet, _, ref = region["id"].partition("!")
    t = region["type"]
    if t != "formula-cell":
        return f"{ref} · {t}"
    text = region["text"] or ""
    if "unresolved" in text:
        return f"{ref} · formula (unresolved)"
    if region["confidence"] == 0.80:
        return f"{ref} · formula (cross-sheet)"
    return f"{ref} · formula"


def _compute_boxes_spreadsheet(regions: list[dict]) -> dict[str, dict]:
    """Box placement derived purely from each region's real sheet/row/column
    — not fabricated. Sheets are stacked into vertical bands in the order
    they appear in the workbook; within a band, a cell's position is its
    (row, col) normalized against that sheet's real max row/col.
    """
    parsed = []
    sheets_order: list[str] = []
    for region in regions:
        sheet, _, ref = region["id"].partition("!")
        if sheet not in sheets_order:
            sheets_order.append(sheet)
        col_letter, row = coordinate_from_string(ref)
        col = column_index_from_string(col_letter)
        parsed.append((region["id"], sheet, row, col))

    max_dims = {}
    for _, sheet, row, col in parsed:
        mr, mc = max_dims.get(sheet, (0, 0))
        max_dims[sheet] = (max(mr, row), max(mc, col))

    num_sheets = len(sheets_order)
    band_height = 100 / num_sheets
    band_gap = 3  # % — visual separation between stacked sheets

    boxes = {}
    for region_id, sheet, row, col in parsed:
        band_index = sheets_order.index(sheet)
        max_row, max_col = max_dims[sheet]
        band_top = band_index * band_height
        usable = band_height - band_gap
        cell_h = usable / max_row
        cell_w = 100 / max_col
        top = band_top + band_gap / 2 + (row - 1) * cell_h
        left = (col - 1) * cell_w
        boxes[region_id] = {
            "top": round(top, 2),
            "left": round(left, 2),
            "width": round(cell_w * 0.92, 2),
            "height": round(cell_h * 0.82, 2),
        }
    return boxes


def _ordinal_stack_boxes(region_ids: list[str]) -> dict[str, dict]:
    """Fallback box placement when a real per-region position isn't
    available (e.g. an OCR'd line with no stored bbox): stack regions as
    full-width horizontal bands in reading order. This is honest about what
    it is — an ordering, not a spatial layout — unlike the spreadsheet and
    diagram box functions, which place regions at their real coordinates.
    """
    n = max(len(region_ids), 1)
    band = 100 / n
    return {
        rid: {"top": round(i * band + band * 0.06, 2), "left": 2.0, "width": 96.0, "height": round(band * 0.82, 2)}
        for i, rid in enumerate(region_ids)
    }


def _compute_boxes_document(path: Path, regions: list[dict]) -> dict[str, dict]:
    """Real per-block bounding boxes, re-extracted from the same PDF via
    PyMuPDF and matched back to each region by the block number encoded in
    its id ('pageN!blockK') — the adapter itself doesn't carry position in
    the common schema (position is a UI concern, not semantic content), so
    this re-derives it purely for the preview panel, from the real file.
    OCR'd lines and unprocessed-scan regions have no stored bbox to
    re-derive this way and fall back to ordinal stacking.
    """
    import fitz

    boxes: dict[str, dict] = {}
    doc = fitz.open(path)
    try:
        num_pages = doc.page_count
        page_band = 100 / max(num_pages, 1)
        fallback_ids = []
        for region in regions:
            m = re.match(r"^page(\d+)!block(\d+)$", region["id"])
            if not m:
                fallback_ids.append(region["id"])
                continue
            page_no, block_no = int(m.group(1)), int(m.group(2))
            page = doc[page_no - 1]
            pw, ph = page.rect.width, page.rect.height
            match = next((b for b in page.get_text("blocks") if int(b[5]) == block_no), None)
            if not match:
                fallback_ids.append(region["id"])
                continue
            x0, y0, x1, y1 = match[:4]
            band_top = (page_no - 1) * page_band
            boxes[region["id"]] = {
                "top": round(band_top + (y0 / ph) * page_band, 2),
                "left": round((x0 / pw) * 100, 2),
                "width": round(((x1 - x0) / pw) * 100, 2),
                "height": round(((y1 - y0) / ph) * page_band, 2),
            }
        boxes.update(_ordinal_stack_boxes(fallback_ids))
    finally:
        doc.close()
    return boxes


def _compute_boxes_diagram(path: Path, regions: list[dict]) -> dict[str, dict]:
    """Real pixel bounding boxes from the same OpenCV detection the adapter
    itself runs — re-run here purely to recover position for the preview
    panel, matched back to each region by the sequence number in its id
    ('node-K' / 'edge-K'), since position isn't part of the common schema.
    """
    from adapters.diagram.adapter import _load_binary, _detect_nodes, _mask_out_nodes, _detect_edges
    import cv2

    img = cv2.imread(str(path))
    ih, iw = img.shape[:2]
    binary = _load_binary(path)
    nodes = _detect_nodes(binary)
    masked = _mask_out_nodes(binary, nodes)
    edges = _detect_edges(binary, masked, nodes)

    boxes: dict[str, dict] = {}
    for region in regions:
        m = re.match(r"^(node|edge)-(\d+)$", region["id"])
        if not m:
            continue
        kind, idx = m.group(1), int(m.group(2)) - 1
        bbox = nodes[idx]["bbox"] if kind == "node" and idx < len(nodes) else None
        if kind == "edge" and idx < len(edges):
            bbox = edges[idx]["bbox"]
        if not bbox:
            continue
        x0, y0, x1, y1 = bbox
        pad = 6  # px of on-image padding so a thin edge's box stays visible/clickable
        boxes[region["id"]] = {
            "top": round(max(0, (y0 - pad) / ih) * 100, 2),
            "left": round(max(0, (x0 - pad) / iw) * 100, 2),
            "width": round(max(1, (x1 - x0 + 2 * pad)) / iw * 100, 2),
            "height": round(max(1, (y1 - y0 + 2 * pad)) / ih * 100, 2),
        }
    return boxes


def run_eval_result() -> dict:
    """Runs the real deterministic fact-lookup eval (eval/run_eval.py) and
    returns its result dict — never a hardcoded or estimated number.
    """
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "eval"))
    from run_eval import run as run_eval

    return run_eval()


def build_doc_for(kind: str, path: Path, artifact: dict) -> dict:
    """Builds one DOCS[] entry (UI-enriched view over a real artifact) for
    any of the three kinds. Reused by run_adapter.py's CLI for an arbitrary
    file, and by this script's own default (spreadsheet fixture) and
    --all-kinds paths.
    """
    from adapters.common.outputs import to_table

    if kind == "spreadsheet":
        boxes = _compute_boxes_spreadsheet(artifact["regions"])
    elif kind == "document":
        boxes = _compute_boxes_document(path, artifact["regions"])
    elif kind == "diagram":
        boxes = _compute_boxes_diagram(path, artifact["regions"])
    else:
        boxes = {}
    boxes.update(_ordinal_stack_boxes([r["id"] for r in artifact["regions"] if r["id"] not in boxes]))

    enriched_regions = []
    for region in artifact["regions"]:
        if kind == "spreadsheet":
            type_label = _type_label_spreadsheet(region)
        else:
            type_label = f"{region['id']} · {region['type']}" + (f" ({region['shape']})" if region.get("shape") else "")
        enriched_regions.append(
            {
                **region,
                "typeLabel": type_label,
                "lang": region.get("language"),
                "box": boxes.get(region["id"], {"top": 0, "left": 0, "width": 100, "height": 5}),
            }
        )

    table = to_table(artifact)
    failed_checks = [c for c in artifact["checks"] if not c["passed"]]
    check_summary = (
        f"{len(failed_checks)} check{'s' if len(failed_checks) != 1 else ''} flagged"
        if failed_checks
        else "all checks passed"
    )
    meta = f"{len(artifact['regions'])} regions · {check_summary}"

    return {
        "id": path.stem,
        "kind": artifact["kind"],
        "adapter": artifact["adapter"],
        "title": artifact["artifact"],
        "meta": meta,
        "pageLabel": f"{artifact['kind']} · {path.name}",
        "regions": enriched_regions,
        "tableColumns": table["columns"],
        "table": table["rows"],
        "tableEmptyReason": table.get("empty_reason"),
        "checks": artifact["checks"],
        "realArtifact": artifact,
    }


def build_doc() -> dict:
    """Backward-compatible default: the spreadsheet fixture, exactly as
    this script has always built by default with no arguments.
    """
    from adapters.spreadsheet.adapter import build_artifact

    artifact = build_artifact(FIXTURE)
    return build_doc_for("spreadsheet", FIXTURE, artifact)


def build_all_kinds_docs() -> list[dict]:
    """One real doc per kind (spreadsheet/document/diagram), for verifying
    the UI actually renders all three correctly — CLAUDE.md Phase 4's
    checkpoint, not just the single-spreadsheet default."""
    from adapters.router import build_artifact_for

    docs = []
    for kind, path in FIXTURES_ALL_KINDS.items():
        detected_kind, artifact = build_artifact_for(path)
        assert detected_kind == kind, f"expected router to detect '{kind}' for {path}, got '{detected_kind}'"
        docs.append(build_doc_for(kind, path, artifact))
    return docs


CHECKS_CSS = """
  .checks-strip{ margin-top:var(--space-4); display:flex; flex-direction:column; gap:6px; }
  .checks-strip-label{ font-family:var(--font-mono); font-size:11px; color:var(--ink-faint); }
  .check-badge{ display:flex; align-items:flex-start; gap:8px; font-size:12.5px; padding:8px 10px; border-radius:var(--radius-sm); border:1px solid var(--line-strong); }
  .check-badge.pass{ background:var(--accent-soft); border-color:var(--accent); color:var(--accent-ink); }
  .check-badge.fail{ background:var(--flag-soft); border-color:var(--flag); color:var(--flag-ink); }
  .check-badge .mark{ font-family:var(--font-mono); flex:none; }
  .check-badge .body strong{ display:block; font-family:var(--font-mono); font-size:11.5px; font-weight:500; }
  .check-badge .body span{ display:block; margin-top:2px; font-size:12px; opacity:0.9; }
"""

CHECKS_MARKUP = """
          <div class="checks-strip" id="checksStrip"></div>"""

CHECKS_JS = """
function renderChecks(){
  const doc = currentDoc();
  const strip = document.getElementById('checksStrip');
  if(!doc.checks || !doc.checks.length){ strip.innerHTML = ''; return; }
  strip.innerHTML = '<span class="checks-strip-label">Validation checks — artifact-level, independent of per-region confidence</span>' +
    doc.checks.map(c => {
      const cls = c.passed ? 'pass' : 'fail';
      const mark = c.passed ? '\\u2713' : '\\u2715';
      return '<div class="check-badge ' + cls + '"><span class="mark">' + mark + '</span>' +
        '<div class="body"><strong>' + escapeHtml(c.name) + '</strong><span>' + escapeHtml(c.detail) + '</span></div></div>';
    }).join('');
}
"""


def render_html(docs: list[dict], eval_result: dict | None) -> str:
    html = MOCK_HTML.read_text(encoding="utf-8")
    kinds_present = sorted({d["kind"] for d in docs})

    html = html.replace(
        '<span class="mock-tag">interactive mock — sample data</span>',
        f'<span class="mock-tag">real adapter output — {", ".join(kinds_present)}</span>',
    )
    html = html.replace(
        "<p>Sample content only — this page demonstrates the interaction pattern and evaluation methodology, not a live backend or measured results.</p>",
        f"<p>Real output from {len(docs)} real artifact(s) ({', '.join(d['title'] for d in docs)}) run through their actual adapters — every region, confidence value, and validation check on this page came from a real parse/detection/OCR pass, not sample data.</p>",
    )

    html = html.replace("  </style>", CHECKS_CSS + "  </style>")

    html = html.replace(
        '          </div>\n        </div>\n      </div>\n\n      <aside class="live-panel"',
        '          </div>' + CHECKS_MARKUP + '\n        </div>\n      </div>\n\n      <aside class="live-panel"',
    )

    # Replace the spreadsheet comparison row with the real, measured result
    # from eval/run_eval.py — a deterministic fact-lookup eval, not an LLM
    # benchmark (see that script's docstring and the adapter README for the
    # methodology and its limits) — or an honest "no eval" note when this
    # run has no ground truth to score against (an arbitrary user file via
    # run_adapter.py, not the known fixture).
    old_row = (
        '    <div class="compare-row">\n'
        '      <div class="compare-label">Spreadsheets — formula &amp; total questions</div>\n'
        '      <div class="compare-bars">\n'
        '        <div class="bar baseline" style="width:34%">34% · flattened text</div>\n'
        '        <div class="bar structured" style="width:92%">92% · structured</div>\n'
        '      </div>\n'
        '    </div>\n'
    )
    if eval_result is not None:
        baseline_pct = eval_result["baseline_pct"]
        structured_pct = eval_result["structured_pct"]
        n = eval_result["n"]
        new_row = (
            '    <div class="compare-row">\n'
            '      <div class="compare-label">Spreadsheets — formula &amp; total questions '
            f'({eval_result["baseline_correct"]}/{n} vs {eval_result["structured_correct"]}/{n}, deterministic fact-lookup eval)</div>\n'
            '      <div class="compare-bars">\n'
            f'        <div class="bar baseline" style="width:{max(baseline_pct, 6)}%">{baseline_pct}% · flattened text</div>\n'
            f'        <div class="bar structured" style="width:{max(structured_pct, 6)}%">{structured_pct}% · structured</div>\n'
            '      </div>\n'
            '    </div>\n'
        )
        proof_note = (
            'Documents and diagrams: illustrative targets — no eval harness exists for those kinds yet '
            '(see docs/eval-methodology.md). Spreadsheets: real, measured result from eval/run_eval.py — a '
            'deterministic fact-lookup eval (does the answer exist at all in each representation), not an LLM '
            'benchmark. The 0% baseline reflects this fixture\'s formulas having no cached value to flatten, not '
            'a rigged comparison — see adapters/spreadsheet/README.md.'
        )
    else:
        new_row = (
            '    <div class="compare-row">\n'
            '      <div class="compare-label">Spreadsheets — formula &amp; total questions</div>\n'
            '      <div class="compare-bars">\n'
            '        <div class="bar" style="width:100%;background:var(--line-strong);color:var(--ink-soft);">'
            'no eval run for this file — the deterministic eval needs hand-authored ground truth, which only '
            'exists for tests/fixtures/budget_test.xlsx (run eval/run_eval.py against that fixture, or supply '
            '--eval-questions)</div>\n'
            '      </div>\n'
            '    </div>\n'
        )
        proof_note = (
            'Illustrative targets only — no eval was run for this/these artifact(s). See '
            'docs/eval-methodology.md for what a real eval requires and adapters/spreadsheet/README.md for the '
            'one kind that has a real measured number today.'
        )
    assert old_row in html, "spreadsheet compare-row markup not found — mock file may have changed"
    html = html.replace(old_row, new_row)

    html = html.replace(
        '<p class="proof-note">Illustrative targets from the evaluation methodology, not measured results — the real numbers come from running this harness against a held-out sample once each adapter is built.</p>',
        f'<p class="proof-note">{proof_note}</p>',
    )

    # Swap the sample DOCS array for the real artifact(s).
    docs_pattern = re.compile(r"const DOCS = \[.*?\n\];\n", re.DOTALL)
    assert docs_pattern.search(html), "DOCS array not found in mock — mock file may have changed"
    docs_replacement = "const DOCS = " + json.dumps(docs, indent=2) + ";\n"
    html = docs_pattern.sub(lambda _: docs_replacement, html)

    # renderJson: emit the true common-schema artifact (source + regions +
    # checks) exactly as build_artifact/to_json produced it, instead of the
    # mock's hand-picked subset of fields.
    old_render_json = (
        "function renderJson(){\n"
        "  const doc = currentDoc();\n"
        "  const panel = document.getElementById('panel-json');\n"
        "  const obj = {\n"
        "    artifact: doc.title,\n"
        "    kind: doc.kind,\n"
        "    adapter: doc.adapter,\n"
        "    page: doc.pageLabel,\n"
        "    path_used: doc.path,\n"
        "    regions: doc.regions.map(r => ({\n"
        "      type: r.type,\n"
        "      shape: r.shape || null,\n"
        "      language: r.lang,\n"
        "      confidence: r.confidence,\n"
        "      text: r.text\n"
        "    }))\n"
        "  };\n"
        "  let text = JSON.stringify(obj, null, 2);\n"
    )
    new_render_json = (
        "function renderJson(){\n"
        "  const doc = currentDoc();\n"
        "  const panel = document.getElementById('panel-json');\n"
        "  let text = JSON.stringify(doc.realArtifact, null, 2);\n"
    )
    assert old_render_json in html, "renderJson() body not found — mock file may have changed"
    html = html.replace(old_render_json, new_render_json)

    # renderTable: drive columns from the real to_table() output instead of
    # three hand-picked, kind-specific column sets.
    old_render_table = (
        "function renderTable(){\n"
        "  const doc = currentDoc();\n"
        "  const panel = document.getElementById('panel-table');\n"
        "  if(!doc.table){\n"
        "    panel.innerHTML = '<div class=\"empty-state\">No tabular data in this artifact.<br>Table export stays empty rather than guessing.</div>';\n"
        "    return;\n"
        "  }\n"
        "  let headCols, rows;\n"
        "  if(doc.tableKind === 'lineitems'){\n"
        "    headCols = ['Description','Qty','Unit price','Total'];\n"
        "    rows = doc.table.map(row => '<tr><td>'+row.description+'</td><td>'+row.qty+'</td><td>'+row.unit_price_eur.toFixed(2)+' €</td><td>'+row.total_eur.toFixed(2)+' €</td></tr>').join('');\n"
        "  } else if(doc.tableKind === 'cells'){\n"
        "    headCols = ['Reference','Formula','Value'];\n"
        "    rows = doc.table.map(row => '<tr><td>'+row.ref+'</td><td>'+escapeHtml(row.formula)+'</td><td>'+row.value+'</td></tr>').join('');\n"
        "  } else if(doc.tableKind === 'edges'){\n"
        "    headCols = ['From','To','Label','Confidence'];\n"
        "    rows = doc.table.map(row => '<tr><td>'+row.from+'</td><td>'+row.to+'</td><td>'+row.label+'</td><td>'+Math.round(row.confidence*100)+'%</td></tr>').join('');\n"
        "  }\n"
        "  panel.innerHTML = '<table class=\"extract-table\"><thead><tr>'+headCols.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table>';\n"
        "}\n"
    )
    new_render_table = (
        "function renderTable(){\n"
        "  const doc = currentDoc();\n"
        "  const panel = document.getElementById('panel-table');\n"
        "  if(!doc.table || !doc.table.length){\n"
        "    panel.innerHTML = '<div class=\"empty-state\">' + (doc.tableEmptyReason || 'No tabular data in this artifact.') + '<br>Table export stays empty rather than guessing.</div>';\n"
        "    return;\n"
        "  }\n"
        "  const cols = doc.tableColumns;\n"
        "  const rows = doc.table.map(row => '<tr>' + cols.map(c => {\n"
        "    let v = row[c];\n"
        "    if(c === 'confidence') v = Math.round(v*100) + '%';\n"
        "    if(v === null || v === undefined) v = '—';\n"
        "    return '<td>' + escapeHtml(String(v)) + '</td>';\n"
        "  }).join('') + '</tr>').join('');\n"
        "  panel.innerHTML = '<table class=\"extract-table\"><thead><tr>'+cols.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+rows+'</tbody></table>';\n"
        "}\n"
    )
    assert old_render_table in html, "renderTable() body not found — mock file may have changed"
    html = html.replace(old_render_table, new_render_table)

    # Hook renderChecks() into the same places renderActiveTab() already
    # fires from, and append its definition to the script.
    html = html.replace(
        "function renderActiveTab(){\n  renderText();\n  renderStructured();\n  renderJson();\n  renderTable();\n}",
        "function renderActiveTab(){\n  renderText();\n  renderStructured();\n  renderJson();\n  renderTable();\n  renderChecks();\n}",
    )
    html = html.replace("</script>", CHECKS_JS + "</script>")

    return html


ALL_KINDS_OUTPUT_HTML = ROOT / "design" / "preprocessing-pipeline-all-kinds.html"


def main():
    import sys

    if "--all-kinds" in sys.argv:
        docs = build_all_kinds_docs()
        eval_result = run_eval_result()  # still real — one of the three docs is the spreadsheet fixture
        html = render_html(docs, eval_result)
        ALL_KINDS_OUTPUT_HTML.write_text(html, encoding="utf-8")
        print(f"Wrote {ALL_KINDS_OUTPUT_HTML} ({len(docs)} artifacts: {', '.join(d['kind'] for d in docs)})")
        for d in docs:
            print(f"  {d['kind']:12s} {d['title']:24s} {len(d['regions'])} regions, {len(d['checks'])} checks")
        return

    doc = build_doc()
    eval_result = run_eval_result()
    html = render_html([doc], eval_result)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(
        f"Wrote {OUTPUT_HTML} ({len(doc['regions'])} regions, {len(doc['checks'])} checks, "
        f"eval: baseline {eval_result['baseline_pct']}% vs structured {eval_result['structured_pct']}%)"
    )


if __name__ == "__main__":
    main()
