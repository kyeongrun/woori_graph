param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseName,

    [switch]$Replace,

    [switch]$Resume,

    [switch]$ReplaceOpenSearch
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ComposeFile = Join-Path $PSScriptRoot "compose.yaml"
$LoadRoot = Join-Path $ProjectRoot "runtime\load"
$ReleaseDir = Join-Path $LoadRoot $ReleaseName
$ManifestPath = Join-Path $ReleaseDir "load_manifest.json"
$OpenSearchUrl = "http://127.0.0.1:19200"

if (@($Replace, $Resume, $ReplaceOpenSearch).Where({ $_ }).Count -gt 1) {
    throw "-Replace, -Resume, and -ReplaceOpenSearch cannot be used together"
}

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Load manifest not found: $ManifestPath"
}

$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $Manifest.passed) {
    throw "Load manifest audit did not pass: $ManifestPath"
}

function Assert-LastExitCode([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE"
    }
}

function Get-LastOutputLine([object[]]$Lines) {
    return [string]($Lines | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) } | Select-Object -Last 1)
}

function Wait-Services {
    $deadline = (Get-Date).AddMinutes(8)
    do {
        & docker compose -f $ComposeFile exec -T age pg_isready -U woori -d graphdb *> $null
        $ageReady = $LASTEXITCODE -eq 0
        try {
            $health = Invoke-RestMethod -Method Get -Uri "$OpenSearchUrl/_cluster/health" -TimeoutSec 5
            $openSearchReady = $health.status -in @("green", "yellow")
        }
        catch {
            $openSearchReady = $false
        }
        if ($ageReady -and $openSearchReady) {
            return
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    throw "PostgreSQL/AGE or OpenSearch did not become healthy within 8 minutes"
}

function Test-OpenSearchIndex([string]$IndexName) {
    $statusCode = & curl.exe --silent --output NUL --write-out "%{http_code}" `
        --head "$OpenSearchUrl/$IndexName"
    Assert-LastExitCode "OpenSearch index existence check"
    $statusCode = ([string]$statusCode).Trim()
    if ($statusCode -eq "200") {
        return $true
    }
    if ($statusCode -eq "404") {
        return $false
    }
    throw "Unexpected OpenSearch status for $IndexName`: HTTP $statusCode"
}

function Put-OpenSearchIndex([string]$IndexName, [string]$MappingPath) {
    $exists = Test-OpenSearchIndex $IndexName
    if ($exists -and $Resume) {
        return
    }
    if ($exists -and -not ($Replace -or $ReplaceOpenSearch)) {
        throw "OpenSearch index already exists: $IndexName. Use -ReplaceOpenSearch to rebuild only search indices."
    }
    if ($exists) {
        Invoke-RestMethod -Method Delete -Uri "$OpenSearchUrl/$IndexName" | Out-Null
    }
    $mapping = Get-Content -LiteralPath $MappingPath -Raw -Encoding UTF8
    Invoke-RestMethod -Method Put -Uri "$OpenSearchUrl/$IndexName" `
        -ContentType "application/json; charset=utf-8" -Body $mapping | Out-Null
}

function Send-BulkChunk([string]$ChunkPath, [int]$ChunkNumber, [int]$EstimatedChunks) {
    Write-Host "OpenSearch bulk chunk $ChunkNumber/$EstimatedChunks`: $ChunkPath"
    $responseText = & curl.exe --silent --show-error --fail-with-body `
        --request POST "$OpenSearchUrl/_bulk?refresh=true" `
        --header "Content-Type: application/x-ndjson" `
        --data-binary "@$ChunkPath"
    Assert-LastExitCode "OpenSearch bulk load"
    $result = $responseText | ConvertFrom-Json
    if ($result.errors) {
        $failures = @($result.items | Where-Object { $_.index.error } | Select-Object -First 10)
        throw "OpenSearch bulk load contains failures: $($failures | ConvertTo-Json -Depth 8 -Compress)"
    }
}

function Send-Bulk([string]$BulkPath) {
    # OpenSearch rejects HTTP request bodies larger than its configured limit.
    # Keep every action/document pair intact and send bounded temporary chunks.
    [int64]$maxChunkBytes = 50MB
    $bulkFile = Get-Item -LiteralPath $BulkPath
    $estimatedChunks = [Math]::Max(1, [int][Math]::Ceiling($bulkFile.Length / $maxChunkBytes))
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $reader = [System.IO.StreamReader]::new($BulkPath, [System.Text.Encoding]::UTF8, $true)
    $writer = $null
    $chunkPath = $null
    $chunkBytes = [int64]0
    $chunkNumber = 0

    try {
        $chunkPath = [System.IO.Path]::GetTempFileName()
        $writer = [System.IO.StreamWriter]::new($chunkPath, $false, $utf8NoBom)

        while (($actionLine = $reader.ReadLine()) -ne $null) {
            $documentLine = $reader.ReadLine()
            if ($null -eq $documentLine) {
                throw "OpenSearch bulk file has an action line without a document: $BulkPath"
            }
            $pair = "$actionLine`n$documentLine`n"
            $pairBytes = [System.Text.Encoding]::UTF8.GetByteCount($pair)

            if ($chunkBytes -gt 0 -and ($chunkBytes + $pairBytes) -gt $maxChunkBytes) {
                $writer.Dispose()
                $writer = $null
                $chunkNumber++
                Send-BulkChunk $chunkPath $chunkNumber $estimatedChunks
                [System.IO.File]::Delete($chunkPath)
                $chunkPath = [System.IO.Path]::GetTempFileName()
                $writer = [System.IO.StreamWriter]::new($chunkPath, $false, $utf8NoBom)
                $chunkBytes = 0
            }

            $writer.Write($pair)
            $chunkBytes += $pairBytes
        }

        if ($chunkBytes -gt 0) {
            $writer.Dispose()
            $writer = $null
            $chunkNumber++
            Send-BulkChunk $chunkPath $chunkNumber $estimatedChunks
        }
    }
    finally {
        if ($null -ne $writer) {
            $writer.Dispose()
        }
        $reader.Dispose()
        if ($chunkPath -and [System.IO.File]::Exists($chunkPath)) {
            [System.IO.File]::Delete($chunkPath)
        }
    }
}

function Set-CurrentAlias([string]$AliasName, [string]$IndexName) {
    $actions = New-Object System.Collections.ArrayList
    try {
        $current = Invoke-RestMethod -Method Get -Uri "$OpenSearchUrl/_alias/$AliasName"
        foreach ($property in $current.PSObject.Properties) {
            [void]$actions.Add(@{ remove = @{ index = $property.Name; alias = $AliasName } })
        }
    }
    catch {
        if (-not ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 404)) {
            throw
        }
    }
    [void]$actions.Add(@{ add = @{ index = $IndexName; alias = $AliasName } })
    $body = @{ actions = $actions } | ConvertTo-Json -Depth 8 -Compress
    Invoke-RestMethod -Method Post -Uri "$OpenSearchUrl/_aliases" `
        -ContentType "application/json" -Body $body | Out-Null
}

New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "runtime\postgres") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "runtime\opensearch") | Out-Null

& docker compose -f $ComposeFile up -d
Assert-LastExitCode "docker compose up"
Wait-Services

$ContainerReleaseDir = "/runtime/load/$ReleaseName"
$GraphName = [string]$Manifest.age_graph_name
if (-not ($Resume -or $ReplaceOpenSearch)) {
    & docker compose -f $ComposeFile exec -T age psql -v ON_ERROR_STOP=1 `
        -U woori -d graphdb -f "$ContainerReleaseDir/rdb/schema.sql"
    Assert-LastExitCode "RDB schema creation"

    if ($Replace) {
        $ClearRdbSql = "TRUNCATE graph.relation_evidence, graph.relation, graph.relation_type, graph.entity, graph.document;"
        & docker compose -f $ComposeFile exec -T age psql -v ON_ERROR_STOP=1 `
            -U woori -d graphdb -c $ClearRdbSql
        Assert-LastExitCode "RDB replacement"
    }

    & docker compose -f $ComposeFile exec -T age psql -v ON_ERROR_STOP=1 `
        -U woori -d graphdb -f "$ContainerReleaseDir/rdb/load.sql"
    Assert-LastExitCode "RDB load"

    $GraphCountSql = "LOAD 'age'; SET search_path = ag_catalog, public; SELECT count(*) FROM ag_graph WHERE name = '$GraphName';"
    $GraphCountOutput = & docker compose -f $ComposeFile exec -T age psql -At `
        -U woori -d graphdb -c $GraphCountSql
    Assert-LastExitCode "AGE graph existence check"
    $GraphCount = (Get-LastOutputLine $GraphCountOutput).Trim()
    if ([int]$GraphCount -gt 0) {
        if (-not $Replace) {
            throw "AGE graph already exists: $GraphName. Use -Replace or -Resume."
        }
        $DropSql = "LOAD 'age'; SET search_path = ag_catalog, public; SELECT drop_graph('$GraphName', true);"
        & docker compose -f $ComposeFile exec -T age psql -v ON_ERROR_STOP=1 `
            -U woori -d graphdb -c $DropSql
        Assert-LastExitCode "AGE graph replacement"
    }

    & docker compose -f $ComposeFile exec -T age psql -v ON_ERROR_STOP=1 `
        -U woori -d graphdb -f "$ContainerReleaseDir/age/load.sql"
    Assert-LastExitCode "AGE load"
}
else {
    $GraphCountSql = "LOAD 'age'; SET search_path = ag_catalog, public; SELECT count(*) FROM ag_graph WHERE name = '$GraphName';"
    $GraphCountOutput = & docker compose -f $ComposeFile exec -T age psql -At `
        -U woori -d graphdb -c $GraphCountSql
    Assert-LastExitCode "AGE resume check"
    $GraphCount = (Get-LastOutputLine $GraphCountOutput).Trim()
    if ([int]$GraphCount -eq 0) {
        throw "AGE graph does not exist while skipping graph load: $GraphName"
    }
}

$OpenSearchDir = Join-Path $ReleaseDir "opensearch"
$EntityIndex = [string]$Manifest.opensearch.entity_index
$RelationIndex = [string]$Manifest.opensearch.relation_index
Put-OpenSearchIndex $EntityIndex (Join-Path $OpenSearchDir "entities.mapping.json")
Put-OpenSearchIndex $RelationIndex (Join-Path $OpenSearchDir "relations.mapping.json")
Send-Bulk (Join-Path $OpenSearchDir "entities.bulk.ndjson")
Send-Bulk (Join-Path $OpenSearchDir "relations.bulk.ndjson")
Set-CurrentAlias ([string]$Manifest.opensearch.entity_alias) $EntityIndex
Set-CurrentAlias ([string]$Manifest.opensearch.relation_alias) $RelationIndex

$RdbCountSql = @"
SELECT json_build_object(
  'documents', (SELECT count(*) FROM graph.document),
  'entities', (SELECT count(*) FROM graph.entity),
  'relation_types', (SELECT count(*) FROM graph.relation_type),
  'relations', (SELECT count(*) FROM graph.relation),
  'evidence', (SELECT count(*) FROM graph.relation_evidence)
);
"@
$RdbCountsOutput = & docker compose -f $ComposeFile exec -T age psql -At `
    -U woori -d graphdb -c $RdbCountSql
Assert-LastExitCode "RDB reconciliation"
$RdbCountsText = (Get-LastOutputLine $RdbCountsOutput).Trim()
$RdbCounts = $RdbCountsText | ConvertFrom-Json

$AgeCountSql = "SELECT json_build_object('entities', (SELECT count(*) FROM `"$GraphName`".`"_ag_label_vertex`"), 'relations', (SELECT count(*) FROM `"$GraphName`".`"_ag_label_edge`"));"
$AgeCountsOutput = & docker compose -f $ComposeFile exec -T age psql -At `
    -U woori -d graphdb -c $AgeCountSql
Assert-LastExitCode "AGE count reconciliation"
$AgeCountsText = (Get-LastOutputLine $AgeCountsOutput).Trim()
$AgeCounts = $AgeCountsText | ConvertFrom-Json

$IdCheckSql = @"
LOAD 'age';
SET search_path = ag_catalog, public;
WITH age_entity_ids AS (
  SELECT jsonb_extract_path_text(properties::text::jsonb, 'id') AS id
  FROM "$GraphName"."_ag_label_vertex"
), age_relation_ids AS (
  SELECT jsonb_extract_path_text(properties::text::jsonb, 'id') AS id
  FROM "$GraphName"."_ag_label_edge"
)
SELECT json_build_object(
  'missing_entities', (SELECT count(*) FROM graph.entity e WHERE NOT EXISTS (SELECT 1 FROM age_entity_ids a WHERE a.id = e.entity_id::text)),
  'missing_relations', (SELECT count(*) FROM graph.relation r WHERE NOT EXISTS (SELECT 1 FROM age_relation_ids a WHERE a.id = r.relation_id::text))
);
"@
$IdChecksOutput = & docker compose -f $ComposeFile exec -T age psql -At `
    -U woori -d graphdb "--command=$IdCheckSql"
Assert-LastExitCode "RDB/AGE ID reconciliation"
$IdChecksText = (Get-LastOutputLine $IdChecksOutput).Trim()
$IdChecks = $IdChecksText | ConvertFrom-Json

$OpenSearchCounts = [ordered]@{
    entities = [int64](Invoke-RestMethod -Method Get -Uri "$OpenSearchUrl/$EntityIndex/_count").count
    relations = [int64](Invoke-RestMethod -Method Get -Uri "$OpenSearchUrl/$RelationIndex/_count").count
}
$OpenSearchVectorCounts = [ordered]@{
    entities = 0
    relations = 0
}
if ($Manifest.opensearch.embedding.enabled) {
    $existsBody = '{"query":{"exists":{"field":"embedding"}}}'
    $OpenSearchVectorCounts.entities = [int64](Invoke-RestMethod -Method Post `
        -Uri "$OpenSearchUrl/$EntityIndex/_count" -ContentType "application/json" `
        -Body $existsBody).count
    $OpenSearchVectorCounts.relations = [int64](Invoke-RestMethod -Method Post `
        -Uri "$OpenSearchUrl/$RelationIndex/_count" -ContentType "application/json" `
        -Body $existsBody).count
}

$Passed = (
    [int64]$RdbCounts.documents -eq [int64]$Manifest.counts.documents -and
    [int64]$RdbCounts.entities -eq [int64]$Manifest.counts.entities -and
    [int64]$RdbCounts.relation_types -eq [int64]$Manifest.counts.relation_types -and
    [int64]$RdbCounts.relations -eq [int64]$Manifest.counts.relations -and
    [int64]$RdbCounts.evidence -eq [int64]$Manifest.counts.evidence -and
    [int64]$AgeCounts.entities -eq [int64]$Manifest.counts.entities -and
    [int64]$AgeCounts.relations -eq [int64]$Manifest.counts.relations -and
    [int64]$OpenSearchCounts.entities -eq [int64]$Manifest.counts.entities -and
    [int64]$OpenSearchCounts.relations -eq [int64]$Manifest.counts.relations -and
    (
        -not $Manifest.opensearch.embedding.enabled -or
        (
            [int64]$OpenSearchVectorCounts.entities -eq [int64]$Manifest.counts.entities -and
            [int64]$OpenSearchVectorCounts.relations -eq [int64]$Manifest.counts.relations
        )
    ) -and
    [int64]$IdChecks.missing_entities -eq 0 -and
    [int64]$IdChecks.missing_relations -eq 0
)

$Report = [ordered]@{
    passed = $Passed
    dictionary_version = [string]$Manifest.dictionary_version
    age_graph_name = $GraphName
    expected = $Manifest.counts
    rdb = $RdbCounts
    age = $AgeCounts
    opensearch = $OpenSearchCounts
    opensearch_vectors = $OpenSearchVectorCounts
    cross_store_id_check = $IdChecks
    data_paths = [ordered]@{
        postgres = (Join-Path $ProjectRoot "runtime\postgres")
        opensearch = (Join-Path $ProjectRoot "runtime\opensearch")
        load_files = $ReleaseDir
    }
}
$ReportPath = Join-Path $ReleaseDir "load_reconcile.json"
$Report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
$Report | ConvertTo-Json -Depth 10

if (-not $Passed) {
    throw "Cross-store reconciliation failed. See $ReportPath"
}
