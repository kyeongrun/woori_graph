from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_local_compose_starts_age_and_opensearch_without_external_viewer() -> None:
    compose = (PROJECT_ROOT / "deploy" / "local-storage" / "compose.yaml").read_text(
        encoding="utf-8"
    )

    assert "  age:" in compose
    assert "  opensearch:" in compose
    assert "apache/age:release_PG16_1.6.0" in compose
    assert "opensearchproject/opensearch:3.8.0" in compose
    assert '"127.0.0.1:8080:8080"' not in compose
    assert "demtec/" not in compose


def test_local_loader_waits_until_age_and_opensearch_respond() -> None:
    loader = (PROJECT_ROOT / "deploy" / "local-storage" / "load.ps1").read_text(
        encoding="utf-8"
    )

    assert "$ageReady -and $openSearchReady" in loader
    assert "[switch]$ReplaceRdbOnly" in loader
    assert "graph.semantic_unit" in loader
    assert "RDB-only replacement selected" in loader
    assert "8080" not in loader


def test_local_start_script_runs_storage_and_search_web_together() -> None:
    launcher = (PROJECT_ROOT / "deploy" / "local-storage" / "start.ps1").read_text(
        encoding="utf-8"
    )

    assert "[int]$Port = 8765" in launcher
    assert '& docker compose -f $ComposeFile up -d' in launcher
    assert '"search-web"' in launcher
    assert '"--config", $SearchConfig' in launcher
    assert '"--port", $Port' in launcher
    assert '"--allow-remote-embedding"' in launcher
    assert "[switch]$AllowRemoteLlm" in launcher
    assert '"--allow-remote-llm"' in launcher
    assert '$env:PYTHONPATH = $SourceRoot' in launcher
    assert "8080" not in launcher
