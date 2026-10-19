"""The semantic-graph module's pure logic (model building + Mermaid export),
exercised under Node against real adapter output. The SVG rendering itself is
verified in a browser; this covers the part that decides *what* the graph is:
which regions become nodes, how diagram edges collapse into links, and that
the Mermaid text is well-formed.

Skipped automatically if `node` isn't installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GRAPH_JS = ROOT / "webapp" / "static" / "js" / "graph.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = {}; ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const model = ctx.NX.graph.buildModel(input.regions, input.kind);
process.stdout.write(JSON.stringify({
  nodes: model.nodes.map(n => ({ id: n.id, label: n.label, group: n.group, deg: n.deg })),
  links: model.links,
  mermaid: ctx.NX.graph.toMermaid(model),
}));
"""


def run_graph(regions: list[dict], kind: str) -> dict:
    out = subprocess.run(
        ["node", "-e", HARNESS, str(GRAPH_JS)],
        input=json.dumps({"regions": regions, "kind": kind}),
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(out.stdout)


def _region(rid, rtype, shape=None, text=None, rels=(), conf=0.95):
    return {"id": rid, "type": rtype, "shape": shape, "text": text, "confidence": conf,
            "relationships": [{"type": t, "target": tgt} for t, tgt in rels]}


def test_diagram_edges_collapse_into_direct_node_to_node_links():
    regions = [
        _region("node-1", "node", "node-initial"),
        _region("node-2", "node", "node-activity", "Receive Order"),
        _region("node-3", "node", "node-decision", "OK?"),
        _region("swimlane-1", "swimlane", "swimlane-divider"),
        _region("edge-1", "edge", "edge", None, [("connects-to", "node-1"), ("connects-to", "node-2")]),
        _region("edge-2", "edge", "edge", "yes", [("connects-to", "node-2"), ("connects-to", "node-3")]),
    ]
    g = run_graph(regions, "diagram")
    assert [n["id"] for n in g["nodes"]] == ["node-1", "node-2", "node-3"], "edges and swimlanes are not graph nodes"
    assert [(l["source"], l["target"], l["label"]) for l in g["links"]] == [
        ("node-1", "node-2", ""), ("node-2", "node-3", "yes")]
    deg = {n["id"]: n["deg"] for n in g["nodes"]}
    assert deg == {"node-1": 1, "node-2": 2, "node-3": 1}
    m = g["mermaid"]
    assert m.splitlines()[0] == "flowchart TD"
    assert 'n3{"OK?"}' in m, "a decision node exports as a Mermaid diamond"
    assert 'n1(("start"))' in m, "an unlabeled initial node is named 'start'"
    assert 'n2 -->|"yes"| n3' in m and "n1 --> n2" in m


def test_real_spreadsheet_output_becomes_a_formula_dependency_graph():
    from adapters.spreadsheet.adapter import build_artifact

    artifact = build_artifact(ROOT / "tests" / "fixtures" / "budget_test.xlsx")
    g = run_graph(artifact["regions"], "spreadsheet")
    types = {l["type"] for l in g["links"]}
    assert "formula-ref" in types and "cross-sheet-ref" in types
    # nodes are real cell regions, labeled by cell ref (formula cells get the ƒ marker)
    assert any(n["label"].endswith("ƒ") for n in g["nodes"])
    ids = {n["id"] for n in g["nodes"]}
    assert all(l["source"] in ids and l["target"] in ids for l in g["links"])
    assert "-. \"formula-ref\" .->" in g["mermaid"]


def test_document_total_links_back_to_its_line_items():
    from adapters.document.adapter import build_artifact

    artifact = build_artifact(ROOT / "tests" / "fixtures" / "invoice_born_digital.pdf")
    g = run_graph(artifact["regions"], "document")
    sums = [l for l in g["links"] if l["type"] == "sums-line-items"]
    assert sums, "the total-field's sums-line-items relationships must appear as links"
    assert len({l["source"] for l in sums}) == 1, "all links originate at the single total"
