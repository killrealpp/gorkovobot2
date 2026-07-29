param(
    [switch]$Status,
    [switch]$AstOnly,
    [switch]$Semantic,
    [switch]$NoCluster,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$graphOut = Join-Path $projectRoot "graphify-out"
$graphJson = Join-Path $graphOut "graph.json"
$manifestJson = Join-Path $graphOut "manifest.json"

function Get-GraphifyCommand {
    return Get-Command graphify -ErrorAction SilentlyContinue
}

function Get-GraphStats {
    if (-not (Test-Path $graphJson)) {
        return [pscustomobject]@{
            Exists = $false
            Nodes = 0
            Links = 0
            LastWriteTime = $null
        }
    }
    $graph = Get-Content -Raw -Encoding UTF8 $graphJson | ConvertFrom-Json
    $file = Get-Item $graphJson
    return [pscustomobject]@{
        Exists = $true
        Nodes = @($graph.nodes).Count
        Links = @($graph.links).Count
        LastWriteTime = $file.LastWriteTime
    }
}

function Get-ManifestInfo {
    if (-not (Test-Path $manifestJson)) {
        return [pscustomobject]@{
            Exists = $false
            Entries = 0
            MissingOnDisk = @()
            CatalogMissingFromManifest = @()
            LastWriteTime = $null
        }
    }

    $manifest = Get-Content -Raw -Encoding UTF8 $manifestJson | ConvertFrom-Json
    $manifestFiles = @($manifest.PSObject.Properties.Name)
    $missingOnDisk = @(
        $manifestFiles | Where-Object {
            $repoPath = $_ -replace '/', [System.IO.Path]::DirectorySeparatorChar
            -not (Test-Path (Join-Path $projectRoot $repoPath))
        } | Sort-Object
    )

    $catalogFiles = @(
        "app/api/__init__.py",
        "app/api/catalog_admin.py",
        "app/api/public_catalog.py",
        "app/api/server.py",
        "app/catalog/__init__.py",
        "app/catalog/reader.py",
        "scripts/generate_public_catalog_fixture.py",
        "tests/public_catalog_smoke.py"
    )
    $catalogMissing = @(
        $catalogFiles | Where-Object {
            $repoPath = $_ -replace '/', [System.IO.Path]::DirectorySeparatorChar
            (Test-Path (Join-Path $projectRoot $repoPath)) -and ($manifestFiles -notcontains $_)
        } | Sort-Object
    )

    $file = Get-Item $manifestJson
    return [pscustomobject]@{
        Exists = $true
        Entries = $manifestFiles.Count
        MissingOnDisk = $missingOnDisk
        CatalogMissingFromManifest = $catalogMissing
        LastWriteTime = $file.LastWriteTime
    }
}

function Show-GraphifyStatus {
    $command = Get-GraphifyCommand
    $stats = Get-GraphStats
    $manifest = Get-ManifestInfo

    if ($command) {
        Write-Output "Graphify command: $($command.Source)"
    } else {
        Write-Output "Graphify command: NOT_FOUND"
    }
    if (Test-Path $graphOut) {
        Write-Output "graphify-out: present"
    } else {
        Write-Output "graphify-out: missing"
    }
    if ($stats.Exists) {
        Write-Output "graph.json: nodes=$($stats.Nodes) links=$($stats.Links) modified=$($stats.LastWriteTime)"
    } else {
        Write-Output "graph.json: missing"
    }
    if ($manifest.Exists) {
        Write-Output "manifest.json: entries=$($manifest.Entries) modified=$($manifest.LastWriteTime)"
        Write-Output "manifest missing-on-disk count: $(@($manifest.MissingOnDisk).Count)"
        if (@($manifest.MissingOnDisk).Count -gt 0) {
            Write-Output "manifest missing-on-disk sample: $((@($manifest.MissingOnDisk) | Select-Object -First 8) -join ', ')"
        }
        Write-Output "catalog files missing from manifest count: $(@($manifest.CatalogMissingFromManifest).Count)"
        if (@($manifest.CatalogMissingFromManifest).Count -gt 0) {
            Write-Output "catalog files missing from manifest: $((@($manifest.CatalogMissingFromManifest) | Select-Object -First 20) -join ', ')"
            Write-Output "freshness: stale for current public catalog work"
        } else {
            Write-Output "freshness: no catalog drift detected by wrapper"
        }
    } else {
        Write-Output "manifest.json: missing"
        Write-Output "freshness: unknown"
    }
}

if ($Status) {
    Show-GraphifyStatus
    return
}

$graphify = Get-GraphifyCommand
if (-not $graphify) {
    throw "Graphify CLI is not on PATH. Install or expose graphify before running a build."
}

if ($Semantic) {
    Write-Output "Running Graphify semantic extraction. This is explicit because it may use configured LLM credentials."
    & $graphify.Source extract $projectRoot --out $projectRoot
    if ($LASTEXITCODE -ne 0) {
        throw "graphify extract failed with exit code $LASTEXITCODE"
    }
    return
}

$previousForce = $env:GRAPHIFY_FORCE
try {
    if ($Force) {
        $env:GRAPHIFY_FORCE = "1"
    }
    $arguments = @("update", $projectRoot)
    if ($NoCluster) {
        $arguments += "--no-cluster"
    }
    if ($AstOnly -or -not $Semantic) {
        Write-Output "Running Graphify AST/local update."
        & $graphify.Source @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "graphify update failed with exit code $LASTEXITCODE"
        }
    }
}
finally {
    if ($null -eq $previousForce) {
        Remove-Item Env:\GRAPHIFY_FORCE -ErrorAction SilentlyContinue
    } else {
        $env:GRAPHIFY_FORCE = $previousForce
    }
}
