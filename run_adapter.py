#!/usr/bin/env python
"""CLI entry point for the semantic preprocessing pipeline.

    python run_adapter.py --input <path-to-any-file> --output-dir <dir> [--eval-questions <path>]

Detects the artifact type from the file's real content (adapters/router.py
— magic bytes, not the extension), routes to the matching adapter
(spreadsheet/document/diagram), and writes:

    <output-dir>/schema.json      the full common-schema artifact (source + regions + checks)
    <output-dir>/text.txt         to_text() output
    <output-dir>/structured.json  to_structured() output
    <output-dir>/table.json       to_table() output
    <output-dir>/prototype.html   a self-contained static HTML report over this one real artifact

No server, no network calls, no auth — a single offline run over one file.

Eval: the deterministic fact-lookup eval (eval/run_eval.py) needs
hand-authored ground truth (which cell/region a question is about, and what
the expected answer is). That only exists today for
tests/fixtures/budget_test.xlsx. Running this CLI against any other file
does NOT auto-generate questions or fake a score — it says so plainly and
moves on, unless --eval-questions points at a real question file in the
same shape as eval/qa_dataset.json (spreadsheet inputs only; document and
diagram don't have an eval implementation yet).

Errors are reported clearly and the process exits non-zero — never a raw
traceback for an expected failure mode (missing file, wrong/corrupt format,
an adapter hitting something it can't handle).
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "eval"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def _fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_adapter.py",
        description="Route any supported artifact file through its adapter and write real, structured output.",
    )
    parser.add_argument("--input", required=True, help="Path to a .xlsx, .pdf, .png, or .jpg file.")
    parser.add_argument("--output-dir", required=True, help="Directory to write schema.json/text.txt/structured.json/table.json/prototype.html into.")
    parser.add_argument(
        "--eval-questions",
        default=None,
        help="Path to a question file in eval/qa_dataset.json's shape. Spreadsheet inputs only. "
        "Without this, no eval is run and the output says so explicitly.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)

    from adapters.router import UnsupportedArtifactError, sniff_kind

    try:
        kind = sniff_kind(input_path)
    except FileNotFoundError as exc:
        _fail(str(exc))
    except UnsupportedArtifactError as exc:
        _fail(str(exc))

    print(f"Detected: {kind} (from real file content, not the extension)")

    try:
        if kind == "spreadsheet":
            from adapters.spreadsheet.adapter import build_artifact
        elif kind == "document":
            from adapters.document.adapter import build_artifact
        elif kind == "diagram":
            from adapters.diagram.adapter import build_artifact
        artifact = build_artifact(input_path)
    except zipfile.BadZipFile as exc:
        _fail(f"'{input_path}' could not be opened as an .xlsx workbook: {exc}")
    except Exception as exc:  # adapter-boundary safety net — see module docstring
        _fail(
            f"the {kind} adapter failed while processing '{input_path}': {type(exc).__name__}: {exc}\n"
            "This is reported as a clean error rather than a raw traceback, but it is a real failure — "
            "not a recognized-but-unresolvable pattern (those degrade to a low-confidence region instead of raising)."
        )

    from adapters.common.outputs import to_json, to_structured, to_table, to_text

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "schema.json").write_text(json.dumps(to_json(artifact), indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "text.txt").write_text(to_text(artifact), encoding="utf-8")
    (output_dir / "structured.json").write_text(json.dumps(to_structured(artifact), indent=2, ensure_ascii=False), encoding="utf-8")
    (output_dir / "table.json").write_text(json.dumps(to_table(artifact), indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{artifact['adapter']} — {len(artifact['regions'])} region(s)")
    conf_by_tier: dict[float, int] = {}
    for r in artifact["regions"]:
        conf_by_tier[r["confidence"]] = conf_by_tier.get(r["confidence"], 0) + 1
    print("  confidence distribution:")
    for conf in sorted(conf_by_tier, reverse=True):
        print(f"    {conf:.2f}: {conf_by_tier[conf]} region(s)")
    print("  checks:")
    for c in artifact["checks"]:
        status = "PASS" if c["passed"] else "FAIL"
        print(f"    [{status}] {c['name']}: {c['detail']}")

    eval_result = None
    if args.eval_questions:
        if kind != "spreadsheet":
            print(
                f"\neval: --eval-questions was given, but the deterministic eval only understands "
                f"spreadsheet cell/region lookups today; '{input_path}' is a {kind}. Skipping eval "
                "rather than faking a score."
            )
        else:
            from run_eval import run as run_eval

            print(f"\nRunning eval against '{args.eval_questions}'...")
            try:
                eval_result = run_eval(fixture_path=input_path, qa_path=Path(args.eval_questions))
            except (KeyError, ValueError) as exc:
                _fail(f"--eval-questions file is malformed: {exc}")
    else:
        print(
            "\neval: not run. The deterministic eval requires hand-authored ground truth "
            "(which cell/region each question targets, and the expected answer) — that only exists "
            "today for tests/fixtures/budget_test.xlsx. Run `python eval/run_eval.py` against that "
            "fixture for a real scored comparison, or supply --eval-questions <path> (spreadsheet "
            "inputs only, same shape as eval/qa_dataset.json) to score this file."
        )

    from build_prototype import build_doc_for, render_html

    doc = build_doc_for(kind, input_path, artifact)
    html = render_html([doc], eval_result)
    (output_dir / "prototype.html").write_text(html, encoding="utf-8")

    print(f"\nWrote: {output_dir}/schema.json, text.txt, structured.json, table.json, prototype.html")


if __name__ == "__main__":
    main()
