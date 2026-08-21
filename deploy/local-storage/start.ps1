[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [string]$SearchConfig,

    [bool]$AllowRemoteEmbedding = $true,

    [switch]$AllowRemoteLlm,

    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
$ComposeFile = Join-Path $PSScriptRoot "compose.yaml"

if ([string]::IsNullOrWhiteSpace($SearchConfig)) {
    $SearchConfig = Join-Path $ProjectRoot "config\\search.example.toml"
}
else {
    $SearchConfig = (Resolve-Path -LiteralPath $SearchConfig).Path
}

if (-not (Test-Path -LiteralPath $SearchConfig -PathType Leaf)) {
    throw "Search config not found: $SearchConfig"
}

function Assert-LastExitCode([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE"
    }
}

& docker compose -f $ComposeFile up -d
Assert-LastExitCode "docker compose up"

$searchArguments = @(
    "search-web",
    "--config", $SearchConfig,
    "--port", $Port
)
if ($AllowRemoteEmbedding) {
    $searchArguments += "--allow-remote-embedding"
}
if ($AllowRemoteLlm) {
    $searchArguments += "--allow-remote-llm"
}
if ($OpenBrowser) {
    $searchArguments += "--open-browser"
}

Write-Host "Search web: http://127.0.0.1:$Port/"
Write-Host "Press Ctrl+C to stop the search web. Docker services keep running."

$WooriGraphCommand = Get-Command woori-graph -ErrorAction SilentlyContinue
if ($null -ne $WooriGraphCommand) {
    & woori-graph @searchArguments
}
else {
    $SourceRoot = Join-Path $ProjectRoot "src"
    $HadPythonPath = Test-Path Env:PYTHONPATH
    $PreviousPythonPath = $env:PYTHONPATH
    try {
        if ($HadPythonPath) {
            $env:PYTHONPATH = "$SourceRoot;$PreviousPythonPath"
        }
        else {
            $env:PYTHONPATH = $SourceRoot
        }
        & py -3 -m woori_graph @searchArguments
    }
    finally {
        if ($HadPythonPath) {
            $env:PYTHONPATH = $PreviousPythonPath
        }
        else {
            Remove-Item Env:PYTHONPATH
        }
    }
}
Assert-LastExitCode "search web"
