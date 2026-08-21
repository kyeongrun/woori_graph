"""Prompt loading that does not depend on the process working directory."""

from __future__ import annotations

from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_ROOT = _PROJECT_ROOT / "prompts"


def load_prompt_asset(filename: str, *, prompt_root: Path | None = None) -> str:
    root = (prompt_root or DEFAULT_PROMPT_ROOT).resolve()
    path = (root / filename).resolve()
    if path.parent != root:
        raise ValueError(f"Prompt filename must not escape prompt root: {filename}")
    return path.read_text(encoding="utf-8").strip()
