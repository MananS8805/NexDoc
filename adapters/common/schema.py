"""The common schema every adapter converges on.

See CLAUDE.md ("Common schema") for the authoritative shape. This module is
intentionally thin: plain dataclasses with `to_dict`, no adapter-specific
logic. A new adapter (document, diagram, ...) imports these same types and
produces the same shape — that's the entire point of "reusable."
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Relationship:
    type: str
    target: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "target": self.target}


@dataclass
class Region:
    id: str
    type: str
    confidence: float
    text: str | None = None
    shape: str | None = None
    language: str | None = None
    relationships: list[Relationship] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "shape": self.shape,
            "language": self.language,
            "confidence": self.confidence,
            "text": self.text,
            "relationships": [r.to_dict() for r in self.relationships],
        }


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Source:
    path: str
    hash: str
    adapter_version: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "hash": self.hash, "adapter_version": self.adapter_version}


@dataclass
class Artifact:
    artifact: str
    kind: str
    adapter: str
    source: Source
    regions: list[Region]
    checks: list[Check]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "kind": self.kind,
            "adapter": self.adapter,
            "source": self.source.to_dict(),
            "regions": [r.to_dict() for r in self.regions],
            "checks": [c.to_dict() for c in self.checks],
        }


def sha256_of_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
