#!/usr/bin/env python3
"""Write a reproducibility manifest for a completed raw-SVO stage."""

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


def git_head() -> str | None:
    result = subprocess.run(
        ["git", "-c", "safe.directory=D:/woori_graph", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--units", type=Path, required=True)
    parser.add_argument("--raw-svo", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--batch-size", type=int, required=True)
    parser.add_argument("--initial-failures", type=int, default=0)
    parser.add_argument("--recovered-failures", type=int, default=0)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    mode = "w" if args.overwrite else "x"
    manifest = {
        "stage": "extract-raw-svo",
        "status": "completed" if audit.get("passed") else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "program": {"git_parent_commit": git_head()},
        "execution": {
            "workers": args.workers,
            "batch_size": args.batch_size,
            "preserve_llm_output": True,
            "endpoint_scope": "user_approved_private_non_loopback",
            "endpoint_url_recorded": False,
            "initial_failures": args.initial_failures,
            "recovered_failures": args.recovered_failures,
            "recovery_note": "One long response was retried with max_tokens=4096",
        },
        "prompt": {"path": args.prompt.as_posix(), "sha256": sha256(args.prompt)},
        "config": {"path": args.config.as_posix(), "sha256": sha256(args.config)},
        "artifacts": {
            "semantic_units": {
                "path": args.units.as_posix(),
                "sha256": sha256(args.units),
                "record_count": audit["counts"]["semantic_units"],
            },
            "raw_svo": {
                "path": args.raw_svo.as_posix(),
                "sha256": sha256(args.raw_svo),
                "record_count": audit["counts"]["raw_svo_records"],
                "relation_count": audit["counts"]["raw_relations"],
                "empty_record_count": audit["counts"]["empty_relation_records"],
            },
            "audit": {
                "path": args.audit.as_posix(),
                "sha256": sha256(args.audit),
                "passed": audit["passed"],
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0 if audit.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
