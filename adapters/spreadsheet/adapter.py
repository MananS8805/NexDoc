"""Spreadsheet adapter: .xlsx -> common schema.

Public entry points:
    parse_spreadsheet(path)  -> generator yielding Region objects, one per
                                 non-empty cell, in sheet/row/column order.
                                 This is the streaming surface a live UI
                                 panel would consume (see CLAUDE.md's "Live
                                 processing pattern").
    build_artifact(path)     -> dict, the full common-schema artifact
                                 (regions + checks + source), ready to feed
                                 into adapters.common.outputs.

Region classification (per sheet, generic — not hardcoded to any one
workbook's row layout):
    - the anchor cell of a merged range spanning more than one cell -> "title"
    - the first non-empty, non-title row in a sheet                -> "header"
    - after that, a cell holding a formula                         -> "formula-cell"
    - after that, a cell holding a number                          -> "value"
    - after that, anything else (text)                             -> "label"

Confidence:
    - literal cells (title/header/value/label): 0.99 — a direct, unambiguous
      read of a value already stored as that type in the file. Nothing to
      infer, so nothing to be uncertain about.
    - formula cells: driven entirely by adapters.spreadsheet.formula.evaluate
      — 0.96 when a formula resolves fully within its own sheet, 0.80 when
      it resolves but reaches into another sheet, 0.4 when it can't be
      resolved at all (missing sheet, non-numeric range, unsupported
      pattern). See formula.py's docstring for why "resolved" isn't just
      "has a cached value."
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries
from openpyxl.workbook.workbook import Workbook

from adapters.common.schema import Artifact, Check, Region, Relationship, Source, sha256_of_file
from adapters.spreadsheet.formula import SUM_RANGE_RE, evaluate

ADAPTER_NAME = "Spreadsheet adapter"
ADAPTER_VERSION = "0.1.0"

CONFIDENCE_LITERAL = 0.99
CONFIDENCE_FORMULA_SAME_SHEET = 0.96
CONFIDENCE_FORMULA_CROSS_SHEET = 0.80
CONFIDENCE_FORMULA_UNRESOLVED = 0.40


def _is_formula(value) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _merge_anchors(ws) -> set[tuple[int, int]]:
    return {
        (r.min_row, r.min_col)
        for r in ws.merged_cells.ranges
        if (r.min_row, r.min_col) != (r.max_row, r.max_col)
    }


def _classify(is_title_row: bool, header_assigned: bool, value) -> str:
    if is_title_row:
        return "title"
    if not header_assigned:
        return "header"
    if _is_formula(value):
        return "formula-cell"
    if isinstance(value, (int, float)):
        return "value"
    return "label"


def _formula_confidence(resolved) -> float:
    if not resolved.ok:
        return CONFIDENCE_FORMULA_UNRESOLVED
    if resolved.cross_sheet_refs:
        return CONFIDENCE_FORMULA_CROSS_SHEET
    return CONFIDENCE_FORMULA_SAME_SHEET


def _format_formula_text(formula: str, resolved) -> str:
    if resolved.ok:
        return f"{formula} → {resolved.value}"
    return f"{formula} → unresolved ({resolved.reason})"


def _build_region(wb: Workbook, sheet_name: str, cell, region_type: str) -> Region:
    coord = cell.coordinate
    region_id = f"{sheet_name}!{coord}"

    if region_type == "formula-cell":
        resolved = evaluate(wb, sheet_name, coord)
        relationships = [Relationship("formula-ref", f"{sheet_name}!{ref}") for ref in resolved.same_sheet_refs]
        relationships += [
            Relationship("cross-sheet-ref", f"{target_sheet}!{target_cell}")
            for target_sheet, target_cell in resolved.cross_sheet_refs
        ]
        return Region(
            id=region_id,
            type=region_type,
            confidence=_formula_confidence(resolved),
            text=_format_formula_text(cell.value, resolved),
            relationships=relationships,
        )

    return Region(
        id=region_id,
        type=region_type,
        confidence=CONFIDENCE_LITERAL,
        text=str(cell.value),
    )


def _iter_regions(wb: Workbook) -> Iterator[Region]:
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        anchors = _merge_anchors(ws)
        header_assigned = False
        for row in ws.iter_rows():
            populated = [c for c in row if c.value is not None]
            if not populated:
                continue
            is_title_row = any((c.row, c.column) in anchors for c in populated)
            for cell in populated:
                yield _build_region(wb, sheet_name, cell, _classify(is_title_row, header_assigned, cell.value))
            if not is_title_row:
                header_assigned = True


def parse_spreadsheet(path: str | Path) -> Iterator[Region]:
    """Streaming entry point: opens the workbook itself and yields Region
    objects one at a time, in reading order.
    """
    wb = load_workbook(path, data_only=False)
    yield from _iter_regions(wb)


def _check_cross_sheet_refs(wb: Workbook, regions: list[Region]) -> Check:
    cross_refs = [
        (region.id, rel.target)
        for region in regions
        for rel in region.relationships
        if rel.type == "cross-sheet-ref"
    ]
    if not cross_refs:
        return Check("cross-sheet-references-resolve", True, "no cross-sheet references in this workbook")

    parts = []
    all_ok = True
    for region_id, target in cross_refs:
        target_sheet = target.split("!", 1)[0]
        exists = target_sheet in wb.sheetnames
        all_ok = all_ok and exists
        parts.append(f"{region_id} → {target} ({'resolved' if exists else 'sheet not found'})")
    return Check("cross-sheet-references-resolve", all_ok, "; ".join(parts))


def _check_totals_consistency(wb: Workbook, regions: list[Region]) -> Check:
    """For every formula cell that is a plain, same-sheet SUM(range) over
    cells that are themselves literal numbers, independently sum those
    literals (a bare Python sum(), not the recursive evaluator) and compare
    against the evaluator's resolved value. Formulas that reach into other
    formulas or other sheets are out of scope here — those are covered by
    confidence tiering and the cross-sheet-reference check instead.
    """
    checked = []
    for region in regions:
        if region.type != "formula-cell":
            continue
        sheet_name, _, coord = region.id.partition("!")
        raw = wb[sheet_name][coord].value
        if not _is_formula(raw):
            continue
        m = SUM_RANGE_RE.match(raw[1:].strip())
        if not m:
            continue

        start, end = m.groups()
        min_col, min_row, max_col, max_row = range_boundaries(f"{start}:{end}")
        literals = []
        all_literal = True
        for r in range(min_row, max_row + 1):
            for c in range(min_col, max_col + 1):
                v = wb[sheet_name].cell(row=r, column=c).value
                if _is_formula(v):
                    all_literal = False
                    break
                if isinstance(v, (int, float)):
                    literals.append(v)
            if not all_literal:
                break
        if not all_literal:
            continue

        independent_sum = sum(literals)
        resolved = evaluate(wb, sheet_name, coord)
        checked.append((region.id, independent_sum, resolved.value, resolved.ok and resolved.value == independent_sum))

    if not checked:
        return Check("totals-match-referenced-cells", True, "no plain same-sheet SUM formulas over literal cells to check")

    passed = all(ok for _, _, _, ok in checked)
    detail = "; ".join(f"{rid}: independent sum={isum}, formula value={fval}" for rid, isum, fval, ok in checked)
    return Check("totals-match-referenced-cells", passed, f"{len(checked)} totals formula(s) checked. {detail}")


def run_checks(wb: Workbook, regions: list[Region]) -> list[Check]:
    return [_check_cross_sheet_refs(wb, regions), _check_totals_consistency(wb, regions)]


def build_artifact(path: str | Path) -> dict:
    """Runs the full pipeline and returns the common-schema artifact as a dict."""
    path = Path(path)
    wb = load_workbook(path, data_only=False)
    regions = list(_iter_regions(wb))
    checks = run_checks(wb, regions)
    source = Source(path=str(path), hash=sha256_of_file(path), adapter_version=ADAPTER_VERSION)
    artifact = Artifact(
        artifact=path.stem,
        kind="spreadsheet",
        adapter=ADAPTER_NAME,
        source=source,
        regions=regions,
        checks=checks,
    )
    return artifact.to_dict()
