#!/usr/bin/env python3
"""Write the source-bearing entity/relation candidate stage manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("raw_svo", "entities", "relations", "audit", "output"):
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    git_result = subprocess.run(
        ["git", "-c", "safe.directory=D:/woori_graph", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    manifest = {
        "stage": "build-source-bearing-candidates",
        "status": "completed" if audit.get("passed") else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "program": {"git_parent_commit": git_result.stdout.strip() or None},
        "strategy": {
            "grouping": "exact_surface",
            "source_text": "resolved_text_or_unit_text",
            "semantic_clustering_applied": False,
        },
        "artifacts": {
            "raw_svo": {"path": args.raw_svo.as_posix(), "sha256": sha256(args.raw_svo)},
            "entity_candidates": {
                "path": args.entities.as_posix(),
                "sha256": sha256(args.entities),
                "record_count": audit["counts"]["entity_candidates"],
            },
            "relation_candidates": {
                "path": args.relations.as_posix(),
                "sha256": sha256(args.relations),
                "record_count": audit["counts"]["relation_candidates"],
            },
            "audit": {
                "path": args.audit.as_posix(),
                "sha256": sha256(args.audit),
                "passed": audit["passed"],
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if args.overwrite else "x"
    with args.output.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0 if audit.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
