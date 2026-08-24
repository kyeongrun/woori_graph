from pathlib import Path

from woori_graph.dictionary_build.config import load_dictionary_build_config


def test_context_complete_config_resolves_paths_and_fixed_storage_names() -> None:
    config = load_dictionary_build_config(
        Path("config/dictionary_build.context_complete.toml")
    )

    assert config.source.name == "raw"
    assert config.context_workers == 48
    assert config.svo_workers == 48
    assert config.relation_max_types == 100
    assert config.relation_polarity_strategy == "separate_canonical_types"
    assert config.postgres_schema == "graph_v2"
    assert config.age_graph == "svo_v2"
