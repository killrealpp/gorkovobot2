param(
    [int]$Limit = 20,
    [string[]]$Only = @(),
    [int]$MaxRows = 1000,
    [switch]$Apply,
    [switch]$Yes,
    [switch]$Json,
    [switch]$IncludeFiles
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = Join-Path $projectRoot "venv\Scripts\python.exe"
}
if (-not (Test-Path $python)) {
    $python = "python"
}

$previous = @{}
function Set-ScopedEnv {
    param(
        [string]$Name,
        [string]$Value
    )
    $script:previous[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Restore-ScopedEnv {
    foreach ($name in $script:previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $script:previous[$name], "Process")
    }
}

try {
    Set-ScopedEnv "PYTHONIOENCODING" "utf-8"
    Set-ScopedEnv "DB_HOST" "aws-0-eu-north-1.pooler.supabase.com"
    Set-ScopedEnv "DB_PORT" "5432"
    Set-ScopedEnv "DB_NAME" "postgres"
    Set-ScopedEnv "DB_USER" "postgres.apchmukcofaggmamkdte"
    Set-ScopedEnv "DB_SSLMODE" "require"

    Write-Host "Retention cleanup target: test Supabase DB $env:DB_HOST/$env:DB_NAME as $env:DB_USER"
    if ($Apply -and -not $Yes) {
        Write-Host "Apply requested without -Yes; the Python guard will refuse mutation."
    }
    if (-not $Apply) {
        Write-Host "Mode: dry-run only. No rows/files will be changed."
    }

    $args = @("scripts\retention_cleanup.py", "--limit", $Limit.ToString(), "--max-rows", $MaxRows.ToString())
    if (-not $IncludeFiles) {
        $args += "--skip-files"
    }
    foreach ($item in $Only) {
        $args += @("--only", $item)
    }
    if ($Apply) {
        $args += "--apply"
    }
    if ($Yes) {
        $args += "--yes"
    }
    if ($Json) {
        $args += "--json"
    }

    & $python @args
    if ($LASTEXITCODE -ne 0) {
        throw "Retention cleanup failed with exit code $LASTEXITCODE"
    }
}
finally {
    Restore-ScopedEnv
}
