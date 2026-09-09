"""A deliberately small formula evaluator.

openpyxl never evaluates formulas, and a file that was authored by openpyxl
(rather than saved by Excel/LibreOffice) has no cached result to fall back
on either — `load_workbook(..., data_only=True)` returns None for every
formula cell in that case. So the adapter needs *some* way to produce a
resolved value and a real confidence signal.

Rather than pull in a full formula engine, this resolves a still-small but
now meaningfully wider set of patterns:

    =SUM(A1:A9)                          same-sheet range aggregate
    =AVERAGE(A1:A9) / COUNT / MAX / MIN   the same, four more aggregate functions
    ='Other Sheet'!A1                    cross-sheet single-cell reference
    =SUM(A1:A9)+'Other Sheet'!A1         a range aggregate plus a cross-sheet cell
    =SUM(A1:A5)+SUM(B1:B5)               two range aggregates
    =A1+B1+'Other Sheet'!C1              any mix of same-sheet cells, cross-
                                          sheet cells, and range aggregates,
                                          joined by any number of "+"
    =IF(A1>10, B1, C1)                   a single cell comparison, both
                                          branches a literal or same-sheet cell

Anything else comes back as UNSUPPORTED rather than a guess — an adapter
that silently mis-evaluates an unfamiliar formula shape is worse than one
that flags it for review. This is still a small, deliberately-scoped
evaluator, not a general formula engine: no nested IF, no string functions,
no arithmetic operators besides "+", no multi-condition logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

from openpyxl.utils import range_boundaries
from openpyxl.workbook.workbook import Workbook

SUM_RANGE_RE = re.compile(r"^SUM\(([A-Z]+\d+):([A-Z]+\d+)\)$")
AGG_RANGE_RE = re.compile(r"^(SUM|AVERAGE|COUNT|MAX|MIN)\(([A-Z]+\d+):([A-Z]+\d+)\)$")
CROSS_SHEET_CELL_RE = re.compile(r"^(?:'([^']+)'|([A-Za-z0-9_ ]+))!([A-Z]+\d+)$")
SAME_SHEET_CELL_RE = re.compile(r"^[A-Z]+\d+$")
IF_RE = re.compile(
    r"^IF\(\s*([A-Z]+\d+)\s*(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*,\s*([^,()]+)\s*,\s*([^,()]+)\s*\)$"
)

UNRESOLVED_SHEET_NOT_FOUND = "sheet not found"
UNRESOLVED_UNSUPPORTED_PATTERN = "unsupported formula pattern"
UNRESOLVED_NON_NUMERIC_CELL = "range contains a non-numeric cell"
UNRESOLVED_EMPTY_RANGE = "aggregate over an empty/non-numeric range"


@dataclass
class Resolved:
    """Result of evaluating one formula cell."""

    value: Union[float, int, None]
    ok: bool
    reason: str | None  # set when ok is False
    same_sheet_refs: list[str]  # cell coordinates referenced within the formula's own sheet
    cross_sheet_refs: list[tuple[str, str]]  # (sheet_name, cell_coordinate) pairs


def _literal_value(ws, coordinate: str) -> Union[float, int, str, None]:
    return ws[coordinate].value


def _is_formula(value) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _range_values(ws, start: str, end: str) -> tuple[list[Union[float, int]] | None, bool, list[str]]:
    """Walks a range, resolving any nested formula cells recursively.
    Returns (numeric values found, ok, cell coordinates referenced) — `ok`
    is False the moment a non-numeric or unresolvable cell is hit, the same
    "flag it, don't guess" behavior the original SUM-only version had.
    """
    min_col, min_row, max_col, max_row = range_boundaries(f"{start}:{end}")
    values: list[Union[float, int]] = []
    refs = []
    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            cell = ws.cell(row=row, column=col)
            refs.append(cell.coordinate)
            value = cell.value
            if _is_formula(value):
                nested = evaluate(ws.parent, ws.title, cell.coordinate)
                if not nested.ok:
                    return None, False, refs
                value = nested.value
            if value is None:
                continue
            if not isinstance(value, (int, float)):
                return None, False, refs
            values.append(value)
    return values, True, refs


def _sum_range(ws, start: str, end: str) -> tuple[Union[float, int, None], bool, list[str]]:
    """Kept as its own function (rather than folded into _range_values +
    _apply_aggregate) because adapters/spreadsheet/adapter.py's
    _check_totals_consistency imports SUM_RANGE_RE and re-derives an
    independent sum the same way this does, as a second, separate code
    path — that check's whole point is comparing two independently-computed
    sums, so this shouldn't become "the same function called twice."
    """
    values, ok, refs = _range_values(ws, start, end)
    if not ok:
        return None, False, refs
    return sum(values), True, refs


def _apply_aggregate(func: str, values: list[Union[float, int]]) -> tuple[Union[float, int, None], bool, str | None]:
    if func == "SUM":
        return sum(values), True, None
    if func == "COUNT":
        return len(values), True, None
    if not values:
        return None, False, UNRESOLVED_EMPTY_RANGE
    if func == "AVERAGE":
        return sum(values) / len(values), True, None
    if func == "MAX":
        return max(values), True, None
    if func == "MIN":
        return min(values), True, None
    return None, False, UNRESOLVED_UNSUPPORTED_PATTERN  # pragma: no cover — AGG_RANGE_RE only matches the above


def _resolve_term(workbook: Workbook, ws, body: str) -> tuple[Union[float, int, None], bool, str | None, list[str], list[tuple[str, str]]]:
    """Resolves one '+'-separated term of a formula body: a range aggregate,
    a cross-sheet single cell, a same-sheet single cell, or a bare numeric
    literal. Returns (value, ok, reason, same_sheet_refs, cross_sheet_refs).
    """
    m = AGG_RANGE_RE.match(body)
    if m:
        func, start, end = m.groups()
        values, ok, refs = _range_values(ws, start, end)
        if not ok:
            return None, False, UNRESOLVED_NON_NUMERIC_CELL, refs, []
        value, ok, reason = _apply_aggregate(func, values)
        return value, ok, reason, refs, []

    m = CROSS_SHEET_CELL_RE.match(body)
    if m:
        quoted_sheet, bare_sheet, cell = m.groups()
        target_sheet = quoted_sheet or bare_sheet
        if target_sheet not in workbook.sheetnames:
            return None, False, UNRESOLVED_SHEET_NOT_FOUND, [], [(target_sheet, cell)]
        nested = evaluate(workbook, target_sheet, cell)
        if not nested.ok:
            return None, False, nested.reason, [], [(target_sheet, cell)]
        return nested.value, True, None, [], [(target_sheet, cell)]

    m = SAME_SHEET_CELL_RE.match(body)
    if m:
        value = ws[body].value
        if _is_formula(value):
            nested = evaluate(workbook, ws.title, body)
            if not nested.ok:
                return None, False, nested.reason, [body], []
            return nested.value, True, None, [body], []
        if isinstance(value, (int, float)):
            return value, True, None, [body], []
        return None, False, UNRESOLVED_NON_NUMERIC_CELL, [body], []

    try:
        return float(body) if "." in body else int(body), True, None, [], []
    except ValueError:
        return None, False, UNRESOLVED_UNSUPPORTED_PATTERN, [], []


def _resolve_if(workbook: Workbook, ws, m: re.Match) -> Resolved:
    cell_ref, op, threshold_str, true_branch, false_branch = m.groups()
    cond_value = ws[cell_ref].value
    if _is_formula(cond_value):
        nested = evaluate(workbook, ws.title, cell_ref)
        if not nested.ok:
            return Resolved(None, False, nested.reason, [cell_ref], [])
        cond_value = nested.value
    if not isinstance(cond_value, (int, float)):
        return Resolved(None, False, UNRESOLVED_NON_NUMERIC_CELL, [cell_ref], [])

    threshold = float(threshold_str)
    ops = {">": lambda a, b: a > b, "<": lambda a, b: a < b, ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "=": lambda a, b: a == b}
    branch = true_branch if ops[op](cond_value, threshold) else false_branch

    value, ok, reason, same_refs, cross_refs = _resolve_term(workbook, ws, branch.strip())
    same_refs = [cell_ref] + same_refs
    if not ok:
        return Resolved(None, False, reason, same_refs, cross_refs)
    return Resolved(value, True, None, same_refs, cross_refs)


def evaluate(workbook: Workbook, sheet_name: str, coordinate: str) -> Resolved:
    """Evaluate the formula at `sheet_name!coordinate`. Resolves recursively
    when a referenced cell is itself a formula (e.g. a cross-sheet total that
    is itself a SUM).
    """
    ws = workbook[sheet_name]
    raw = ws[coordinate].value
    if not _is_formula(raw):
        val = raw if isinstance(raw, (int, float)) else None
        return Resolved(val, val is not None, None, [], [])

    body = raw[1:].strip()

    if body.startswith("IF("):
        m = IF_RE.match(body)
        if m:
            return _resolve_if(workbook, ws, m)
        return Resolved(None, False, UNRESOLVED_UNSUPPORTED_PATTERN, [], [])

    m = AGG_RANGE_RE.match(body)
    if m:
        func, start, end = m.groups()
        values, ok, refs = _range_values(ws, start, end)
        if not ok:
            return Resolved(None, False, UNRESOLVED_NON_NUMERIC_CELL, refs, [])
        value, ok, reason = _apply_aggregate(func, values)
        if not ok:
            return Resolved(None, False, reason, refs, [])
        return Resolved(value, True, None, refs, [])

    m = CROSS_SHEET_CELL_RE.match(body)
    if m:
        quoted_sheet, bare_sheet, cell = m.groups()
        target_sheet = quoted_sheet or bare_sheet
        if target_sheet not in workbook.sheetnames:
            return Resolved(None, False, UNRESOLVED_SHEET_NOT_FOUND, [], [(target_sheet, cell)])
        nested = evaluate(workbook, target_sheet, cell)
        if not nested.ok:
            return Resolved(None, False, nested.reason, [], [(target_sheet, cell)])
        return Resolved(nested.value, True, None, [], [(target_sheet, cell)])

    if "+" in body:
        terms = [t.strip() for t in body.split("+")]
        total = 0
        same_refs: list[str] = []
        cross_refs: list[tuple[str, str]] = []
        for term in terms:
            value, ok, reason, term_same_refs, term_cross_refs = _resolve_term(workbook, ws, term)
            same_refs += term_same_refs
            cross_refs += term_cross_refs
            if not ok:
                return Resolved(None, False, reason, same_refs, cross_refs)
            total += value
        return Resolved(total, True, None, same_refs, cross_refs)

    return Resolved(None, False, UNRESOLVED_UNSUPPORTED_PATTERN, [], [])
