param(
    [switch]$WarnOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$wikiRoot = Join-Path $projectRoot "wiki"
$required = @(
    "index.md",
    "log.md",
    "project-overview.md",
    "integrations-and-storage.md",
    "dialog-flow.md",
    "quality-and-operations.md",
    "three-stage-roadmap.md",
    "tooling-memory-stack.md",
    "preparation-execplan-2026-07-27.md",
    "stage-01-retention-report-execplan-2026-07-27.md",
    "stage-02-yaml-universalization-execplan-2026-07-27.md"
)

$issues = New-Object System.Collections.Generic.List[string]
if (-not (Test-Path $wikiRoot)) {
    $issues.Add("Missing wiki directory: wiki")
} else {
    foreach ($file in $required) {
        $path = Join-Path $wikiRoot $file
        if (-not (Test-Path $path)) {
            $issues.Add("Missing wiki file: wiki/$file")
            continue
        }
        $content = Get-Content -Raw -Encoding UTF8 $path
        if ([string]::IsNullOrWhiteSpace($content)) {
            $issues.Add("Empty wiki file: wiki/$file")
        }
    }
}

if ($issues.Count -gt 0) {
    foreach ($issue in $issues) {
        if ($WarnOnly) {
            Write-Warning $issue
        } else {
            Write-Error $issue
        }
    }
    if (-not $WarnOnly) {
        exit 1
    }
}

Write-Output "Wiki check completed. Issues: $($issues.Count)"
