"""The four output shapes, implemented as pure functions over the common schema.

Every function takes the same `Artifact.to_dict()` result and reshapes it —
none of them re-read the source file or re-run extraction. Per CLAUDE.md,
this is what makes them reusable across adapters: a diagram's artifact dict
and a spreadsheet's artifact dict both flow through the exact same four
functions.
"""

from __future__ import annotations

from typing import Any


def to_text(artifact: dict[str, Any]) -> str:
    """Regions flattened in reading order, for search indexing."""
    lines = []
    for region in artifact["regions"]:
        if region["text"]:
            lines.append(region["text"])
        else:
            lines.append(f"[{region['type']} — excluded, non-text]")
    return "\n".join(lines)


def to_structured(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    """The region list itself, typed and confidence-scored, for data entry automation."""
    return [dict(region) for region in artifact["regions"]]


def to_json(artifact: dict[str, Any]) -> dict[str, Any]:
    """The full schema, including source and checks, for compliance/audit review."""
    return dict(artifact)


def to_table(artifact: dict[str, Any]) -> dict[str, Any]:
    """Reshapes per `kind` into tabular rows. Empty state when there's nothing
    tabular — never fabricate rows to fill the view.
    """
    kind = artifact["kind"]
    if kind == "spreadsheet":
        return _table_spreadsheet(artifact)
    if kind == "document":
        return _table_document(artifact)
    if kind == "diagram":
        return _table_diagram(artifact)
    return {"columns": [], "rows": [], "empty_reason": f"no table mapping defined for kind '{kind}'"}


def _table_spreadsheet(artifact: dict[str, Any]) -> dict[str, Any]:
    """One row per cell: sheet, cell reference, formula (or '—' for a literal),
    resolved value, and confidence. Formula/value are split back out of the
    region's `text` field, which the spreadsheet adapter always writes as
    either a plain literal or "<formula> → <value>" — no re-parsing of the
    source, just re-presenting what the adapter already extracted.
    """
    rows = []
    for region in artifact["regions"]:
        sheet, _, ref = region["id"].partition("!")
        if region["type"] == "formula-cell" and region["text"] and " → " in region["text"]:
            formula, value = region["text"].split(" → ", 1)
        else:
            formula, value = "—", region["text"]
        rows.append(
            {
                "sheet": sheet,
                "ref": ref or region["id"],
                "formula": formula,
                "value": value,
                "confidence": region["confidence"],
            }
        )
    if not rows:
        return {"columns": [], "rows": [], "empty_reason": "no cells extracted"}
    return {"columns": ["sheet", "ref", "formula", "value", "confidence"], "rows": rows}


def _table_document(artifact: dict[str, Any]) -> dict[str, Any]:
    line_item_regions = [r for r in artifact["regions"] if r["type"] == "line-item"]
    if not line_item_regions:
        return {"columns": [], "rows": [], "empty_reason": "no tabular data in this artifact"}
    rows = [{"text": r["text"], "confidence": r["confidence"]} for r in line_item_regions]
    return {"columns": ["text", "confidence"], "rows": rows}


def _table_diagram(artifact: dict[str, Any]) -> dict[str, Any]:
    edge_regions = [r for r in artifact["regions"] if r["type"] == "edge"]
    if not edge_regions:
        return {"columns": [], "rows": [], "empty_reason": "no edges extracted"}
    rows = []
    for edge in edge_regions:
        targets = [rel["target"] for rel in edge["relationships"] if rel["type"] == "connects-to"]
        rows.append(
            {
                "from": targets[0] if len(targets) > 0 else None,
                "to": targets[1] if len(targets) > 1 else None,
                "label": edge["text"],
                "confidence": edge["confidence"],
            }
        )
    return {"columns": ["from", "to", "label", "confidence"], "rows": rows}
