import json

from woori_graph.graph_build.pipeline import GraphBuildPipeline
from woori_graph.ids import stable_id


class FakeClient:
    def __init__(self, response: dict):
        self.response = response

    def complete(self, prompt: str) -> str:
        return json.dumps(self.response, ensure_ascii=False)


def test_graph_pipeline_force_maps_unknown_relation_into_dictionary() -> None:
    relation_type_id = stable_id("relation_type", "부과하다")
    raw = [
        {
            "semantic_unit_id": "unit-1",
            "document_id": "doc-1",
            "document_title": "테스트법",
            "source_path": "test.md",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "relations": [
                {
                    "relation_mention_id": "mention-1",
                    "subject": "위원회",
                    "predicate": "벌과금을 물린다",
                    "object": "벌금",
                }
            ],
        }
    ]
    relation_dictionary = [
        {
            "relation_type_id": relation_type_id,
            "canonical_name": "부과하다",
            "polarity": "POSITIVE",
            "aliases": [{"name": "부과한다"}],
        }
    ]
    client = FakeClient(
        {
            "items": [
                {
                    "raw_predicate": "벌과금을 물린다",
                    "relation_type_id": relation_type_id,
                }
            ]
        }
    )

    bundle = GraphBuildPipeline().map_for_load(
        raw,
        [],
        relation_dictionary,
        dictionary_version="v1",
        client=client,
    )

    assert bundle.relations[0]["relation_type_id"] == relation_type_id
    assert bundle.relation_mapping_results[0]["mapping_status"] == (
        "forced_dictionary_mapping"
    )
