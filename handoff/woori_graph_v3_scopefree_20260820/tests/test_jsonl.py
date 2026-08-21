import json
from pathlib import Path

from woori_graph.jsonl import write_jsonl


def test_write_jsonl_puts_requested_keys_first():
    output = Path(__file__).with_name(".dictionary-order-test.jsonl")
    try:
        write_jsonl(
            output,
            [{"entity_id": "id-1", "aliases": ["별칭"], "canonical_name": "대표명"}],
            overwrite=True,
            leading_keys=("canonical_name", "aliases"),
        )

        line = output.read_text(encoding="utf-8").strip()
        assert list(json.loads(line)) == ["canonical_name", "aliases", "entity_id"]
    finally:
        output.unlink(missing_ok=True)
