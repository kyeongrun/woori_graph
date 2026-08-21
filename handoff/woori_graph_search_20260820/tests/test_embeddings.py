import pytest

from woori_graph.embeddings import (
    EmbeddingConfig,
    OpenAICompatEmbeddingClient,
    entity_embedding_text,
    normalize_embedding,
    relation_embedding_text,
)


def test_embedding_text_builders_are_bounded_and_directional() -> None:
    entity_text = entity_embedding_text(
        {
            "canonical_name": "금융감독원",
            "aliases": [
                {"name": "금융감독원"},
                {"name": "금감원"},
                {"name": "감독원"},
            ],
        },
        alias_limit=1,
    )
    relation_text = relation_embedding_text(
        {"source_name": "금융감독원", "target_name": "벌금"},
        relation_type_name="부과하다",
    )

    assert entity_text == "금융감독원\naliases: 금감원"
    assert relation_text == "금융감독원 부과하다 벌금"


def test_normalize_embedding_validates_dimension_and_zero_vector() -> None:
    assert normalize_embedding([3.0, 4.0], dimension=2) == [0.6, 0.8]
    with pytest.raises(ValueError, match="dimension mismatch"):
        normalize_embedding([1.0], dimension=2)
    with pytest.raises(ValueError, match="zero vector"):
        normalize_embedding([0.0, 0.0], dimension=2)


def test_embedding_client_rejects_remote_endpoint_by_default() -> None:
    config = EmbeddingConfig(
        base_url="https://embedding.example.test/v1",
        api_key="local",
        model="test-model",
        dimension=3,
    )

    with pytest.raises(ValueError, match="loopback"):
        OpenAICompatEmbeddingClient(config)
