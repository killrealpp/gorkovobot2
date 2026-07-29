param(
    [switch]$SkipValidation
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

Write-Output "== Graphify =="
& (Join-Path $PSScriptRoot "graphify-build.ps1") -Status

Write-Output ""
Write-Output "== Headroom =="
$headroom = Get-Command headroom -ErrorAction SilentlyContinue
if ($headroom) {
    & $headroom.Source --version
    if ($LASTEXITCODE -ne 0) {
        throw "headroom --version failed with exit code $LASTEXITCODE"
    }
} else {
    Write-Warning "Headroom CLI is not on PATH. This is not a bot runtime failure."
}

if ($SkipValidation) {
    Write-Output ""
    Write-Output "Validation skipped by -SkipValidation."
    return
}

Write-Output ""
Write-Output "== Validation =="
& (Join-Path $PSScriptRoot "validate.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "validate.ps1 failed with exit code $LASTEXITCODE"
}
