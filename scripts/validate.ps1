param(
    [switch]$IncludeSmoke,
    [switch]$IncludeLegacySmoke,
    [switch]$StrictWiki
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

function Invoke-Checked {
    param(
        [string]$Command,
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

Write-Host "Using Python: $python"
Invoke-Checked $python @("-m", "compileall", "app", "main.py", "scripts", "tests")

if ($IncludeSmoke) {
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("maxbot4-smoke-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    $envFile = Join-Path $tempRoot ".env"
    $dbPath = Join-Path $tempRoot "bot.sqlite3"
    Set-Content -Path $envFile -Encoding UTF8 -Value @"
SQLITE_PATH=$dbPath
DB_HOST=
DB_NAME=
DB_USER=
DB_PASSWORD=
CLIENT_CHANNELS=max
PAYMENT_STATUS_LOOP_ENABLED=false
YCLIENTS_SYNC_ENABLED=false
STARTUP_AVAILABILITY_REFRESH_ENABLED=false
WATCHLIST_LOOP_ENABLED=false
"@
    $previousAppEnvFile = $env:APP_ENV_FILE
    $previousSqlitePath = $env:SQLITE_PATH
    try {
        $env:APP_ENV_FILE = $envFile
        $env:SQLITE_PATH = $dbPath
        Invoke-Checked $python @("tests\smoke.py")
        Invoke-Checked $python @("tests\admin_profile_smoke.py")
        Invoke-Checked $python @("tests\stage2_behavior_parity_smoke.py")
        Invoke-Checked $python @("tests\retention_report_smoke.py")
        Invoke-Checked $python @("tests\retention_cleanup_smoke.py")
        Invoke-Checked $python @("tests\retention_scheduler_smoke.py")
        if ($IncludeLegacySmoke) {
            Invoke-Checked $python @("tests\validation_smoke.py")
            Invoke-Checked $python @("tests\booking_flow_smoke.py")
        } else {
            Write-Output "Legacy internal smoke scripts skipped. Run with -IncludeLegacySmoke after updating them to current dialog engine internals."
        }
    }
    finally {
        if ($null -eq $previousAppEnvFile) {
            Remove-Item Env:\APP_ENV_FILE -ErrorAction SilentlyContinue
        } else {
            $env:APP_ENV_FILE = $previousAppEnvFile
        }
        if ($null -eq $previousSqlitePath) {
            Remove-Item Env:\SQLITE_PATH -ErrorAction SilentlyContinue
        } else {
            $env:SQLITE_PATH = $previousSqlitePath
        }
    }
} else {
    Write-Host "Smoke scripts are available. Run with -IncludeSmoke to use a temporary APP_ENV_FILE and SQLite database."
}

if ($StrictWiki) {
    & (Join-Path $PSScriptRoot "wiki-check.ps1")
} else {
    & (Join-Path $PSScriptRoot "wiki-check.ps1") -WarnOnly
}

Write-Host "Validation baseline completed."
