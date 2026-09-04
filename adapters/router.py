"""The real "detect artifact type" + "route to adapter" stages from
CLAUDE.md's architecture (stages 1-2). Detection is by content sniffing —
the actual file signature, not the filename's extension — so a mislabeled
extension doesn't silently misroute a file to the wrong adapter; it's
either still identified correctly from its real bytes, or rejected with a
clear error rather than guessed at.

This module knows about all three adapters, by design — it's the one place
that's allowed to, since "route to the correct adapter" is its entire job.
Each adapter otherwise stays independent of the other two (CLAUDE.md's
guardrail): this router imports an adapter's `build_artifact` only inside
the branch that needs it, so a bug in the diagram adapter's import graph
can't affect a spreadsheet run.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

SUPPORTED_KINDS = ("spreadsheet", "document", "diagram")


class UnsupportedArtifactError(ValueError):
    """Raised when a file's real content doesn't match any known adapter,
    or a zip-based file isn't the specific Office format we support."""


def _confirm_xlsx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise UnsupportedArtifactError(f"'{path}' looks like a zip container but could not be opened: {exc}") from exc
    if "xl/workbook.xml" not in names:
        raise UnsupportedArtifactError(
            f"'{path}' is a zip-based file but not a recognized .xlsx workbook "
            "(no xl/workbook.xml inside it). Other Office formats (.docx, .pptx) "
            "aren't supported by any adapter yet."
        )
    return "spreadsheet"


def sniff_kind(path: str | Path) -> str:
    """Returns one of SUPPORTED_KINDS based on the file's real content, or
    raises UnsupportedArtifactError / FileNotFoundError with a clear reason.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    if path.is_dir():
        raise UnsupportedArtifactError(f"'{path}' is a directory, not a file")

    with open(path, "rb") as f:
        header = f.read(8)

    if header.startswith(b"PK\x03\x04"):
        return _confirm_xlsx(path)
    if header.startswith(b"%PDF"):
        return "document"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "diagram"
    if header[:3] == b"\xff\xd8\xff":
        return "diagram"  # JPEG

    raise UnsupportedArtifactError(
        f"Could not determine an artifact type for '{path}' from its content "
        f"(first bytes: {header!r}). Supported: .xlsx (spreadsheet), .pdf "
        "(document), .png/.jpg (diagram)."
    )


def build_artifact_for(path: str | Path) -> tuple[str, dict]:
    """Sniffs the file's real kind and runs the matching adapter's
    build_artifact against it. Returns (kind, artifact_dict).
    """
    kind = sniff_kind(path)
    if kind == "spreadsheet":
        from adapters.spreadsheet.adapter import build_artifact
    elif kind == "document":
        from adapters.document.adapter import build_artifact
    elif kind == "diagram":
        from adapters.diagram.adapter import build_artifact
    else:  # pragma: no cover — sniff_kind only ever returns SUPPORTED_KINDS
        raise UnsupportedArtifactError(f"no adapter registered for kind '{kind}'")
    return kind, build_artifact(path)
